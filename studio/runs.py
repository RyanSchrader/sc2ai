from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from sc2.paths import Paths

from .models import MatchCreate, RegressionCreate, utc_now
from .repository import PROJECT_ROOT, ConflictError, StudioRepository

ENEMY_RACES = {"terran", "protoss", "zerg", "random"}
DIFFICULTIES = {
    "very_easy",
    "easy",
    "medium",
    "medium_hard",
    "hard",
    "harder",
    "very_hard",
}
RESULT_PREFIX = "SC2_STUDIO_RESULT:"
PROGRESS_PREFIX = "SC2_STUDIO_PROGRESS:"
ACTIVE_STATUSES = {"starting", "running", "stopping"}
TERMINAL_BATCH_STATUSES = {
    "completed",
    "completed_with_failures",
    "cancelled",
    "failed",
}
MAX_RETAINED_LOGS = 5000
MAX_LOG_LINE_LENGTH = 16_384
MAX_RETAINED_COMPLETED_RUNS = 25


def discover_maps() -> list[str]:
    candidates: list[Path] = []
    try:
        candidates.append(Path(Paths.MAPS))
    except Exception:
        pass
    candidates.append(Path("/Applications/StarCraft II/maps"))
    maps: set[str] = set()
    for root in candidates:
        if not root.exists():
            continue
        for path in root.rglob("*.SC2Map"):
            maps.add(path.stem)
        for path in root.rglob("*.sc2map"):
            maps.add(path.stem)
    return sorted(maps, key=str.lower)


@dataclass
class RunState:
    id: str
    bot_id: str
    bot_name: str
    bot_revision: int
    map_name: str
    opponent_type: str
    enemy_race: str
    difficulty: str | None
    opponent_bot_id: str | None = None
    opponent_bot_name: str | None = None
    opponent_revision: int | None = None
    regression_batch_id: str | None = None
    regression_game_id: str | None = None
    status: str = "starting"
    created_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    return_code: int | None = None
    logs: list[str] = field(default_factory=list)
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    result_payload: dict[str, object] | None = field(default=None, repr=False)
    game_time_seconds: float | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    next_log_sequence: int = 1
    stop_requested: bool = field(default=False, repr=False)
    log_persistence_failed: bool = field(default=False, repr=False)
    launch_task: asyncio.Task[asyncio.subprocess.Process] | None = field(
        default=None, repr=False
    )

    @property
    def first_log_sequence(self) -> int:
        return self.next_log_sequence - len(self.logs)

    def append_log(self, line: str) -> int:
        sequence = self.next_log_sequence
        self.next_log_sequence += 1
        if len(line) > MAX_LOG_LINE_LENGTH:
            line = (
                line[: MAX_LOG_LINE_LENGTH - 20]
                + " … [line truncated]"
            )
        self.logs.append(line)
        if len(self.logs) > MAX_RETAINED_LOGS:
            del self.logs[: len(self.logs) - MAX_RETAINED_LOGS]
        return sequence

    def public(self, repository: StudioRepository) -> dict[str, object]:
        match = repository.get_match(self.id)
        primary = match["participants"][0]
        opponent = match["participants"][1]
        return {
            "id": self.id,
            "botId": self.bot_id,
            "botName": self.bot_name,
            "botRevision": self.bot_revision,
            "botRevisionId": primary["botRevisionId"],
            "botRevisionDigest": primary["botRevisionDigest"],
            "mapName": self.map_name,
            "opponentType": self.opponent_type,
            "opponentBotId": self.opponent_bot_id,
            "opponentBotName": self.opponent_bot_name,
            "opponentRevision": self.opponent_revision,
            "opponentRevisionId": opponent["botRevisionId"],
            "opponentRevisionDigest": opponent["botRevisionDigest"],
            "enemyRace": opponent["resolvedRace"] or opponent["requestedRace"],
            "difficulty": self.difficulty,
            "status": match["status"],
            "result": primary["result"],
            "opponentResult": opponent["result"],
            "gameTimeSeconds": match["gameTimeSeconds"],
            "createdAt": match["createdAt"],
            "finishedAt": match["finishedAt"],
            "returnCode": match["returnCode"],
            "failureReason": match["failureReason"],
            "logCount": self.next_log_sequence - 1,
            "firstLogSequence": self.first_log_sequence,
            "regressionBatchId": self.regression_batch_id,
        }


class RunManager:
    def __init__(self, repository: StudioRepository):
        self.repository = repository
        self.runs: dict[str, RunState] = {}
        self.active_ids: set[str] = set()
        self._batch_token: str | None = None
        self._cancelled_batch_tokens: set[str] = set()
        self._admission_lock = asyncio.Lock()
        self._drain_tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def active_id(self) -> str | None:
        return next(iter(self.active_ids), None)

    @property
    def batch_active(self) -> bool:
        return self._batch_token is not None

    async def reserve_batch(self, token: str) -> None:
        async with self._admission_lock:
            if self.active_ids or self._batch_token is not None:
                raise ConflictError("A match or regression batch is already running.")
            self._cancelled_batch_tokens.discard(token)
            self._batch_token = token

    def cancel_batch(self, token: str) -> None:
        self._cancelled_batch_tokens.add(token)

    async def release_batch(self, token: str) -> None:
        async with self._admission_lock:
            if self._batch_token == token:
                self._batch_token = None
            self._cancelled_batch_tokens.discard(token)

    async def start(self, request: MatchCreate) -> dict[str, object]:
        async with self._admission_lock:
            if self.active_ids or self._batch_token is not None:
                raise ConflictError("A match or regression batch is already running.")
            self._prune_completed_runs()
            state = await self._start(
                bot_id=str(request.bot_id),
                bot_revision=None,
                map_name=request.map_name,
                opponent_type=request.opponent_type,
                enemy_race=request.enemy_race,
                difficulty=request.difficulty,
                opponent_bot_id=(
                    str(request.opponent_bot_id) if request.opponent_bot_id else None
                ),
                opponent_revision=None,
                source="single",
            )
            return state.public(self.repository)

    async def start_regression_game(
        self, bot_id: str, game: dict[str, object], *, reservation_token: str
    ) -> RunState:
        async with self._admission_lock:
            if self._batch_token != reservation_token:
                raise ConflictError("Regression batch no longer owns the match scheduler.")
            if reservation_token in self._cancelled_batch_tokens:
                raise ConflictError("Regression batch launch was cancelled.")
            self._prune_completed_runs()
            return await self._start(
                bot_id=bot_id,
                bot_revision=int(game["testedRevision"]),
                map_name=str(game["mapName"]),
                opponent_type=str(game["opponentType"]),
                enemy_race=str(game["enemyRace"] or "zerg"),
                difficulty=(
                    str(game["difficulty"]) if game["difficulty"] is not None else None
                ),
                opponent_bot_id=(
                    str(game["opponentBotId"]) if game["opponentBotId"] else None
                ),
                opponent_revision=(
                    int(game["opponentRevision"])
                    if game["opponentRevision"] is not None
                    else None
                ),
                source="regression",
                random_seed=int(game["randomSeed"]),
                regression_batch_id=str(game["batchId"]),
                regression_game_id=str(game["id"]),
            )

    def _append_log(self, state: RunState, line: str) -> int:
        sequence = state.append_log(line)
        stored_line = state.logs[-1]
        try:
            self.repository.append_match_log(
                state.id,
                sequence,
                stored_line,
                retain=MAX_RETAINED_LOGS,
            )
        except Exception as exc:
            if not state.log_persistence_failed:
                state.log_persistence_failed = True
                warning = (
                    "Could not persist match log: "
                    f"{type(exc).__name__}: {exc}"
                )
                state.append_log(warning)
        return sequence

    @staticmethod
    def _signal_process(
        process: asyncio.subprocess.Process, signal_number: signal.Signals
    ) -> None:
        pid = getattr(process, "pid", None)
        if pid is not None:
            try:
                os.killpg(pid, signal_number)
                return
            except (ProcessLookupError, PermissionError):
                pass
        method_name = "kill" if signal_number == signal.SIGKILL else "terminate"
        method = getattr(process, method_name, None)
        if method is not None:
            try:
                method()
            except ProcessLookupError:
                pass

    async def _terminate_process(
        self, state: RunState, *, timeout: float = 5
    ) -> None:
        process = state.process
        if process is None:
            return
        if getattr(process, "returncode", None) is not None:
            state.return_code = process.returncode
            return
        self._signal_process(process, signal.SIGTERM)
        try:
            state.return_code = await asyncio.wait_for(
                process.wait(), timeout=timeout
            )
        except asyncio.TimeoutError:
            self._signal_process(process, signal.SIGKILL)
            state.return_code = await process.wait()

    async def _start(
        self,
        *,
        bot_id: str,
        bot_revision: int | None,
        map_name: str,
        opponent_type: str,
        enemy_race: str,
        difficulty: str | None,
        opponent_bot_id: str | None,
        opponent_revision: int | None,
        source: str,
        random_seed: int | None = None,
        regression_batch_id: str | None = None,
        regression_game_id: str | None = None,
    ) -> RunState:
        if map_name not in discover_maps():
            raise ValueError(f"Map is not installed: {map_name}")
        if opponent_type == "computer":
            if enemy_race not in ENEMY_RACES:
                raise ValueError("Unsupported enemy race.")
            if difficulty not in DIFFICULTIES:
                raise ValueError("Unsupported difficulty.")
        elif opponent_type != "bot":
            raise ValueError("Unsupported opponent type.")

        bot = self.repository.get_bot_revision(bot_id, bot_revision)
        opponent = None
        if opponent_type == "bot":
            if not opponent_bot_id:
                raise ValueError("Studio bot matches require an opponent bot.")
            opponent = self.repository.get_bot_revision(
                opponent_bot_id, opponent_revision
            )
            if opponent["id"] == bot["id"] and source != "regression":
                raise ValueError("Choose a different Studio bot as the opponent.")

        participants = [
            {
                "participant_type": "bot",
                "bot_id": bot["id"],
                "bot_revision": bot["selectedRevision"],
                "name": bot["name"],
                "requested_race": bot["race"],
                "resolved_race": bot["race"],
                "difficulty": None,
            }
        ]
        if opponent:
            participants.append(
                {
                    "participant_type": "bot",
                    "bot_id": opponent["id"],
                    "bot_revision": opponent["selectedRevision"],
                    "name": opponent["name"],
                    "requested_race": opponent["race"],
                    "resolved_race": opponent["race"],
                    "difficulty": None,
                }
            )
        else:
            participants.append(
                {
                    "participant_type": "computer",
                    "bot_id": None,
                    "bot_revision": None,
                    "name": f"{enemy_race.title()} Computer",
                    "requested_race": enemy_race,
                    "resolved_race": None,
                    "difficulty": difficulty,
                }
            )

        match = self.repository.create_match(
            map_name=map_name,
            source=source,
            participants=participants,
            regression_batch_id=regression_batch_id,
            regression_game_id=regression_game_id,
        )
        state = RunState(
            id=match["id"],
            bot_id=bot["id"],
            bot_name=bot["name"],
            bot_revision=bot["selectedRevision"],
            map_name=map_name,
            opponent_type=opponent_type,
            enemy_race=enemy_race if not opponent else opponent["race"],
            difficulty=difficulty if not opponent else None,
            opponent_bot_id=opponent["id"] if opponent else None,
            opponent_bot_name=opponent["name"] if opponent else None,
            opponent_revision=opponent["selectedRevision"] if opponent else None,
            regression_batch_id=regression_batch_id,
            regression_game_id=regression_game_id,
        )
        environment = os.environ.copy()
        environment["SC2_STUDIO_DB"] = str(self.repository.database_path)
        command = [
            sys.executable,
            str(PROJECT_ROOT / "run_bot.py"),
            "--bot",
            bot["id"],
            "--bot-revision",
            str(bot["selectedRevision"]),
            "--bot-revision-id",
            str(bot["selectedRevisionId"]),
            "--bot-revision-digest",
            str(bot["selectedRevisionDigest"]),
            "--map",
            map_name,
            "--match-id",
            state.id,
        ]
        if opponent:
            command.extend(
                [
                    "--opponent-bot",
                    opponent["id"],
                    "--opponent-revision",
                    str(opponent["selectedRevision"]),
                    "--opponent-revision-id",
                    str(opponent["selectedRevisionId"]),
                    "--opponent-revision-digest",
                    str(opponent["selectedRevisionDigest"]),
                ]
            )
        else:
            command.extend(
                [
                    "--enemy-race",
                    enemy_race,
                    "--difficulty",
                    str(difficulty),
                ]
            )
        if random_seed is not None:
            command.extend(["--random-seed", str(random_seed)])

        self.runs[state.id] = state
        self.active_ids.add(state.id)
        self._append_log(state, f"$ {' '.join(command)}")
        self._append_log(
            state,
            "Primary revision provenance: "
            f"{bot['selectedRevisionId']} sha256:{bot['selectedRevisionDigest']}",
        )
        if opponent:
            self._append_log(
                state,
                "Opponent revision provenance: "
                f"{opponent['selectedRevisionId']} "
                f"sha256:{opponent['selectedRevisionDigest']}",
            )
        try:
            state.launch_task = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *command,
                    cwd=PROJECT_ROOT,
                    env=environment,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                ),
                name=f"sc2-match-launch-{state.id}",
            )
            state.process = await state.launch_task
        except asyncio.CancelledError:
            state.status = "stopped" if state.stop_requested else "failed"
            state.finished_at = utc_now()
            failure_reason = (
                "Match launch was stopped before process creation."
                if state.stop_requested
                else "Match launch was cancelled before process creation."
            )
            self._append_log(state, failure_reason)
            try:
                self.repository.finalize_match(
                    state.id,
                    status=state.status,
                    failure_reason=failure_reason,
                )
            except Exception as finalize_exc:
                self._append_log(
                    state,
                    "Could not persist cancelled launch: "
                    f"{type(finalize_exc).__name__}: {finalize_exc}",
                )
            finally:
                state.launch_task = None
                self.active_ids.discard(state.id)
                state.done.set()
                self._prune_completed_runs()
            raise
        except Exception as exc:
            state.status = "failed"
            state.finished_at = utc_now()
            self._append_log(state, f"Could not launch match process: {exc}")
            try:
                self.repository.finalize_match(
                    state.id,
                    status="failed",
                    failure_reason=f"Could not launch match process: {exc}",
                )
            except Exception as finalize_exc:
                self._append_log(
                    state,
                    "Could not persist launch failure: "
                    f"{type(finalize_exc).__name__}: {finalize_exc}",
                )
            finally:
                state.launch_task = None
                self.active_ids.discard(state.id)
                state.done.set()
                self._prune_completed_runs()
            raise
        finally:
            state.launch_task = None

        try:
            self.repository.set_match_running(state.id)
        except Exception as exc:
            state.status = "failed"
            state.finished_at = utc_now()
            self._append_log(
                state, f"Could not persist running match state: {exc}"
            )
            try:
                await self._terminate_process(state)
            except Exception as terminate_exc:
                self._append_log(
                    state,
                    "Could not terminate match after launch-state failure: "
                    f"{type(terminate_exc).__name__}: {terminate_exc}",
                )
            try:
                self.repository.finalize_match(
                    state.id,
                    status="failed",
                    return_code=state.return_code,
                    failure_reason=f"Could not persist running match state: {exc}",
                )
            except Exception as finalize_exc:
                self._append_log(
                    state,
                    "Could not persist launch state failure: "
                    f"{type(finalize_exc).__name__}: {finalize_exc}",
                )
            finally:
                self.active_ids.discard(state.id)
                state.done.set()
                self._prune_completed_runs()
            raise
        state.status = "stopping" if state.stop_requested else "running"
        drain_task = asyncio.create_task(
            self._drain(state), name=f"sc2-match-drain-{state.id}"
        )
        self._drain_tasks[state.id] = drain_task
        drain_task.add_done_callback(
            lambda completed, run_id=state.id: self._drain_task_finished(
                run_id, completed
            )
        )
        if state.stop_requested and state.process is not None:
            self._signal_process(state.process, signal.SIGTERM)
        return state

    async def _drain(self, state: RunState) -> None:
        try:
            if state.process is None or state.process.stdout is None:
                raise RuntimeError("Match process output stream is unavailable.")
            while True:
                line = await state.process.stdout.readline()
                if not line:
                    break
                decoded = line.decode(errors="replace").rstrip()
                if decoded.startswith(RESULT_PREFIX):
                    try:
                        state.result_payload = json.loads(
                            decoded[len(RESULT_PREFIX) :]
                        )
                    except json.JSONDecodeError:
                        self._append_log(
                            state,
                            "Invalid structured result received from runner."
                        )
                elif decoded.startswith(PROGRESS_PREFIX):
                    try:
                        progress = json.loads(decoded[len(PROGRESS_PREFIX) :])
                        state.game_time_seconds = float(progress["gameTimeSeconds"])
                        self.repository.update_match_game_time(
                            state.id, state.game_time_seconds
                        )
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        self._append_log(
                            state,
                            "Invalid structured progress received from runner."
                        )
                else:
                    self._append_log(state, decoded)

            return_code = await state.process.wait()
            state.return_code = return_code
            if state.status == "stopping":
                state.status = "stopped"
                self.repository.finalize_match(
                    state.id,
                    status="stopped",
                    return_code=return_code,
                    game_time_seconds=state.game_time_seconds,
                    failure_reason="Stopped by user",
                )
            elif return_code != 0:
                state.status = "failed"
                self.repository.finalize_match(
                    state.id,
                    status="failed",
                    return_code=return_code,
                    game_time_seconds=state.game_time_seconds,
                    failure_reason="Match process exited unsuccessfully.",
                )
            elif state.result_payload is None:
                state.status = "failed"
                self.repository.finalize_match(
                    state.id,
                    status="failed",
                    return_code=return_code,
                    game_time_seconds=state.game_time_seconds,
                    failure_reason="Match finished without a structured SC2 result.",
                )
            else:
                state.status = "completed"
                self.repository.finalize_match(
                    state.id,
                    status="completed",
                    return_code=return_code,
                    game_time_seconds=state.result_payload.get("gameTimeSeconds"),
                    participant_results=state.result_payload.get("participants"),
                )
        except asyncio.CancelledError:
            state.status = "failed"
            failure_reason = "Match monitor was cancelled before a terminal result."
            self._append_log(state, failure_reason)
            try:
                await self._terminate_process(state)
            except Exception as terminate_exc:
                self._append_log(
                    state,
                    "Could not terminate cancelled match process: "
                    f"{type(terminate_exc).__name__}: {terminate_exc}",
                )
            try:
                self.repository.finalize_match(
                    state.id,
                    status="failed",
                    return_code=state.return_code,
                    game_time_seconds=state.game_time_seconds,
                    failure_reason=failure_reason,
                )
            except Exception as finalize_exc:
                self._append_log(
                    state,
                    "Could not persist cancelled monitor state: "
                    f"{type(finalize_exc).__name__}: {finalize_exc}",
                )
            raise
        except Exception as exc:
            state.status = "failed"
            failure_reason = (
                f"Match monitor failed: {type(exc).__name__}: {exc}"
            )
            self._append_log(state, failure_reason)
            try:
                await self._terminate_process(state)
            except Exception as terminate_exc:
                self._append_log(
                    state,
                    "Could not terminate failed match process: "
                    f"{type(terminate_exc).__name__}: {terminate_exc}",
                )
            try:
                self.repository.finalize_match(
                    state.id,
                    status="failed",
                    return_code=state.return_code,
                    game_time_seconds=state.game_time_seconds,
                    failure_reason=failure_reason,
                )
            except Exception as finalize_exc:
                self._append_log(
                    state,
                    "Could not persist terminal match status: "
                    f"{type(finalize_exc).__name__}: {finalize_exc}",
                )
        finally:
            state.finished_at = utc_now()
            self.active_ids.discard(state.id)
            state.done.set()

    def _drain_task_finished(
        self, run_id: str, completed: asyncio.Task[None]
    ) -> None:
        if self._drain_tasks.get(run_id) is completed:
            self._drain_tasks.pop(run_id, None)
        self._prune_completed_runs()
        if completed.cancelled():
            return
        # Retrieve unexpected task exceptions so the event loop never reports an
        # unobserved background failure. _drain normally converts them to a
        # terminal match state itself.
        completed.exception()

    def _prune_completed_runs(self) -> None:
        completed_ids = [
            run_id
            for run_id, state in self.runs.items()
            if state.status not in ACTIVE_STATUSES and state.done.is_set()
        ]
        for run_id in completed_ids[:-MAX_RETAINED_COMPLETED_RUNS]:
            self.runs.pop(run_id, None)

    def get(self, run_id: str) -> RunState:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise KeyError(f"Live run not found: {run_id}") from exc

    def public(self, run_id: str) -> dict[str, object]:
        if run_id in self.runs:
            return self.runs[run_id].public(self.repository)
        match = self.repository.get_match(run_id)
        log_bounds = self.repository.get_match_log_bounds(run_id)
        primary, opponent = match["participants"]
        return {
            "id": match["id"],
            "botId": primary["botId"],
            "botName": primary["name"],
            "botRevision": primary["botRevision"],
            "botRevisionId": primary["botRevisionId"],
            "botRevisionDigest": primary["botRevisionDigest"],
            "mapName": match["mapName"],
            "opponentType": opponent["participantType"],
            "opponentBotId": opponent["botId"],
            "opponentBotName": opponent["name"] if opponent["botId"] else None,
            "opponentRevision": opponent["botRevision"],
            "opponentRevisionId": opponent["botRevisionId"],
            "opponentRevisionDigest": opponent["botRevisionDigest"],
            "enemyRace": opponent["resolvedRace"] or opponent["requestedRace"],
            "difficulty": opponent["difficulty"],
            "status": match["status"],
            "result": primary["result"],
            "opponentResult": opponent["result"],
            "gameTimeSeconds": match["gameTimeSeconds"],
            "createdAt": match["createdAt"],
            "finishedAt": match["finishedAt"],
            "returnCode": match["returnCode"],
            "failureReason": match["failureReason"],
            "logCount": log_bounds["lastSequence"] or 0,
            "firstLogSequence": log_bounds["firstSequence"] or 1,
            "regressionBatchId": match["regressionBatchId"],
        }

    async def stop(self, run_id: str) -> dict[str, object]:
        state = self.runs.get(run_id)
        if state is None:
            return self.public(run_id)
        if state.status not in ACTIVE_STATUSES:
            return state.public(self.repository)
        state.stop_requested = True
        state.status = "stopping"
        if state.process is None and state.launch_task is not None:
            state.launch_task.cancel()
        elif state.process is not None:
            self._signal_process(state.process, signal.SIGTERM)
        try:
            await asyncio.wait_for(state.done.wait(), timeout=5)
        except asyncio.TimeoutError:
            if state.launch_task is not None:
                state.launch_task.cancel()
            if state.process is not None:
                await self._terminate_process(state, timeout=1)
            await asyncio.wait_for(state.done.wait(), timeout=5)
        return state.public(self.repository)

    async def stop_all(self) -> None:
        await asyncio.gather(
            *(self.stop(run_id) for run_id in list(self.active_ids)),
            return_exceptions=True,
        )

    async def events(self, run_id: str, after: int = 0):
        state = self.runs.get(run_id)
        if state is None:
            persisted = self.repository.list_match_logs(run_id, after=after)
            first_available = persisted["firstSequence"]
            if (
                first_available is not None
                and after < int(first_available) - 1
            ):
                yield {
                    "type": "log_gap",
                    "after": max(0, after),
                    "firstAvailable": first_available,
                    "lastDropped": int(first_available) - 1,
                }
            for item in persisted["items"]:
                yield {
                    "type": "log",
                    "sequence": item["sequence"],
                    "index": item["sequence"],
                    "line": item["line"],
                }
            yield {"type": "status", **self.public(run_id)}
            return
        cursor = max(0, after)
        while True:
            first_available = state.first_log_sequence
            if cursor < first_available - 1:
                yield {
                    "type": "log_gap",
                    "after": cursor,
                    "firstAvailable": first_available,
                    "lastDropped": first_available - 1,
                }
                cursor = first_available - 1
            while cursor < state.next_log_sequence - 1:
                sequence = cursor + 1
                offset = sequence - state.first_log_sequence
                if offset < 0:
                    break
                try:
                    line = state.logs[offset]
                except IndexError:
                    break
                cursor = sequence
                yield {
                    "type": "log",
                    "sequence": sequence,
                    "index": sequence,
                    "line": line,
                }
            if state.status not in ACTIVE_STATUSES:
                yield {"type": "status", **state.public(self.repository)}
                return
            await asyncio.sleep(0.25)


class RegressionManager:
    def __init__(self, repository: StudioRepository, run_manager: RunManager):
        self.repository = repository
        self.run_manager = run_manager
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.reservations: dict[str, str] = {}
        self.cancelled: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def active_ids(self) -> set[str]:
        return {
            batch_id
            for batch_id, task in self.tasks.items()
            if not task.done()
        }

    def _prune_completed_tasks(self) -> None:
        completed_ids = [
            batch_id for batch_id, task in self.tasks.items() if task.done()
        ]
        for batch_id in completed_ids[:-MAX_RETAINED_COMPLETED_RUNS]:
            self.tasks.pop(batch_id, None)

    @staticmethod
    def _task_finished(completed: asyncio.Task[None]) -> None:
        if not completed.cancelled():
            completed.exception()

    async def start(self, request: RegressionCreate) -> dict[str, object]:
        async with self._lock:
            self._prune_completed_tasks()
            if self.active_ids:
                raise ConflictError("A match or regression batch is already running.")
            suite = self.repository.get_benchmark_suite(str(request.suite_id))
            installed = set(discover_maps())
            missing = sorted(
                {
                    scenario["mapName"]
                    for scenario in suite["scenarios"]
                    if scenario["mapName"] not in installed
                }
            )
            if missing:
                raise ValueError(f"Benchmark maps are not installed: {', '.join(missing)}")
            reservation_token = str(uuid4())
            await self.run_manager.reserve_batch(reservation_token)
            try:
                batch = self.repository.create_regression_batch(
                    bot_id=str(request.bot_id),
                    baseline_revision=request.baseline_revision,
                    suite_id=str(request.suite_id),
                    games_per_scenario=request.games_per_scenario,
                    concurrency=request.concurrency,
                )
                batch_id = str(batch["id"])
                self.reservations[batch_id] = reservation_token
                self.tasks[batch_id] = asyncio.create_task(
                    self._execute(batch_id), name=f"sc2-regression-{batch_id}"
                )
                self.tasks[batch_id].add_done_callback(self._task_finished)
                return batch
            except Exception:
                await self.run_manager.release_batch(reservation_token)
                raise

    async def _execute(self, batch_id: str) -> None:
        reservation_token = self.reservations.get(batch_id)
        try:
            if reservation_token is None:
                raise RuntimeError("Regression scheduler reservation is missing.")
            batch = self.repository.get_regression_batch(batch_id)
            self.repository.set_regression_batch_status(batch_id, "running")
            queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            for game in self.repository.list_queued_regression_games(batch_id):
                queue.put_nowait(game)

            async def worker() -> None:
                while not queue.empty():
                    if batch_id in self.cancelled:
                        return
                    try:
                        game = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    state: RunState | None = None
                    try:
                        if batch_id in self.cancelled:
                            return
                        state = await self.run_manager.start_regression_game(
                            batch["botId"],
                            game,
                            reservation_token=reservation_token,
                        )
                        self.repository.set_regression_game(
                            game["id"], status="running", match_id=state.id
                        )
                        await state.done.wait()
                        terminal = self.repository.get_match(state.id)["status"]
                        self.repository.set_regression_game(
                            game["id"], status=terminal, match_id=state.id
                        )
                    except asyncio.CancelledError:
                        if state is not None and not state.done.is_set():
                            await self.run_manager.stop(state.id)
                        if batch_id in self.cancelled:
                            self.repository.set_regression_game(
                                game["id"], status="stopped"
                            )
                            return
                        self.repository.set_regression_game(
                            game["id"], status="failed"
                        )
                        raise
                    except Exception:
                        if state is not None and not state.done.is_set():
                            await self.run_manager.stop(state.id)
                        if batch_id not in self.cancelled:
                            self.repository.set_regression_game(
                                game["id"], status="failed"
                            )
                        else:
                            return
                    finally:
                        queue.task_done()

            await asyncio.gather(
                *(worker() for _ in range(int(batch["concurrency"])))
            )
            if batch_id in self.cancelled:
                self.repository.cancel_queued_regression_games(batch_id)
                self.repository.set_regression_batch_status(batch_id, "cancelled")
            else:
                finished = self.repository.get_regression_batch(batch_id)
                unfinished = [
                    game
                    for game in finished["games"]
                    if game["status"] not in {"completed", "failed", "stopped"}
                ]
                if unfinished:
                    self.repository.set_regression_batch_status(batch_id, "failed")
                    return
                has_game_failures = any(
                    game["status"] in {"failed", "stopped"}
                    for game in finished["games"]
                )
                self.repository.set_regression_batch_status(
                    batch_id,
                    "completed_with_failures"
                    if has_game_failures
                    else "completed",
                )
        except asyncio.CancelledError:
            try:
                self.repository.cancel_queued_regression_games(batch_id)
                self.repository.set_regression_batch_status(batch_id, "interrupted")
            finally:
                raise
        except Exception:
            try:
                self.repository.set_regression_batch_status(batch_id, "failed")
            except Exception:
                pass
            raise
        finally:
            if reservation_token is not None:
                await self.run_manager.release_batch(reservation_token)
            self.reservations.pop(batch_id, None)
            self.cancelled.discard(batch_id)

    async def cancel(self, batch_id: str) -> dict[str, object]:
        batch = self.repository.get_regression_batch(batch_id)
        if batch["status"] not in {"queued", "running", "starting"}:
            return batch
        self.cancelled.add(batch_id)
        reservation_token = self.reservations.get(batch_id)
        if reservation_token is not None:
            self.run_manager.cancel_batch(reservation_token)
        self.repository.set_regression_batch_status(batch_id, "cancelling")
        active = [
            state.id
            for state in self.run_manager.runs.values()
            if state.regression_batch_id == batch_id and state.status in ACTIVE_STATUSES
        ]
        await asyncio.gather(
            *(self.run_manager.stop(run_id) for run_id in active),
            return_exceptions=True,
        )
        task = self.tasks.get(batch_id)
        if task:
            await task
        return self.repository.get_regression_batch(batch_id)

    async def resume(self, batch_id: str) -> dict[str, object]:
        async with self._lock:
            self._prune_completed_tasks()
            batch = self.repository.get_regression_batch(batch_id)
            if batch["status"] != "interrupted":
                raise ConflictError("Only interrupted regression batches can be resumed.")
            if self.active_ids:
                raise ConflictError("A match or regression batch is already running.")
            reservation_token = str(uuid4())
            await self.run_manager.reserve_batch(reservation_token)
            try:
                self.reservations[batch_id] = reservation_token
                self.tasks[batch_id] = asyncio.create_task(
                    self._execute(batch_id), name=f"sc2-regression-{batch_id}"
                )
                self.tasks[batch_id].add_done_callback(self._task_finished)
                return self.repository.get_regression_batch(batch_id)
            except Exception:
                self.reservations.pop(batch_id, None)
                await self.run_manager.release_batch(reservation_token)
                raise

    async def events(self, batch_id: str):
        previous: str | None = None
        while True:
            batch = self.repository.get_regression_batch(batch_id)
            encoded = json.dumps(batch, sort_keys=True)
            if encoded != previous:
                yield {"type": "progress", **batch}
                previous = encoded
            if batch["status"] in TERMINAL_BATCH_STATUSES | {"interrupted"}:
                return
            await asyncio.sleep(0.5)

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(self.cancel(batch_id) for batch_id in list(self.active_ids)),
            return_exceptions=True,
        )
