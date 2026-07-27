from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

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
TERMINAL_BATCH_STATUSES = {"completed", "cancelled", "failed"}


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

    def public(self, repository: StudioRepository) -> dict[str, object]:
        match = repository.get_match(self.id)
        primary = match["participants"][0]
        opponent = match["participants"][1]
        return {
            "id": self.id,
            "botId": self.bot_id,
            "botName": self.bot_name,
            "botRevision": self.bot_revision,
            "mapName": self.map_name,
            "opponentType": self.opponent_type,
            "opponentBotId": self.opponent_bot_id,
            "opponentBotName": self.opponent_bot_name,
            "opponentRevision": self.opponent_revision,
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
            "logCount": len(self.logs),
            "regressionBatchId": self.regression_batch_id,
        }


class RunManager:
    def __init__(self, repository: StudioRepository):
        self.repository = repository
        self.runs: dict[str, RunState] = {}
        self.active_ids: set[str] = set()
        self.batch_active = False
        self._lock = asyncio.Lock()

    @property
    def active_id(self) -> str | None:
        return next(iter(self.active_ids), None)

    async def start(self, request: MatchCreate) -> dict[str, object]:
        async with self._lock:
            if self.active_ids or self.batch_active:
                raise ConflictError("A match or regression batch is already running.")
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
        self, bot_id: str, game: dict[str, object]
    ) -> RunState:
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

        state.logs.append(f"$ {' '.join(command)}")
        self.runs[state.id] = state
        try:
            state.process = await asyncio.create_subprocess_exec(
                *command,
                cwd=PROJECT_ROOT,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as exc:
            state.status = "failed"
            state.finished_at = utc_now()
            self.repository.finalize_match(
                state.id,
                status="failed",
                failure_reason=f"Could not launch match process: {exc}",
            )
            state.done.set()
            raise

        state.status = "running"
        self.repository.set_match_running(state.id)
        self.active_ids.add(state.id)
        asyncio.create_task(self._drain(state))
        return state

    async def _drain(self, state: RunState) -> None:
        assert state.process and state.process.stdout
        try:
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
                        state.logs.append("Invalid structured result received from runner.")
                elif decoded.startswith(PROGRESS_PREFIX):
                    try:
                        progress = json.loads(decoded[len(PROGRESS_PREFIX) :])
                        state.game_time_seconds = float(progress["gameTimeSeconds"])
                        self.repository.update_match_game_time(
                            state.id, state.game_time_seconds
                        )
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        state.logs.append(
                            "Invalid structured progress received from runner."
                        )
                else:
                    state.logs.append(decoded)
                if len(state.logs) > 5000:
                    del state.logs[:1000]

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
        finally:
            state.finished_at = utc_now()
            self.active_ids.discard(state.id)
            state.done.set()

    def get(self, run_id: str) -> RunState:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise KeyError(f"Live run not found: {run_id}") from exc

    def public(self, run_id: str) -> dict[str, object]:
        if run_id in self.runs:
            return self.runs[run_id].public(self.repository)
        match = self.repository.get_match(run_id)
        primary, opponent = match["participants"]
        return {
            "id": match["id"],
            "botId": primary["botId"],
            "botName": primary["name"],
            "botRevision": primary["botRevision"],
            "mapName": match["mapName"],
            "opponentType": opponent["participantType"],
            "opponentBotId": opponent["botId"],
            "opponentBotName": opponent["name"] if opponent["botId"] else None,
            "opponentRevision": opponent["botRevision"],
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
            "logCount": 0,
            "regressionBatchId": match["regressionBatchId"],
        }

    async def stop(self, run_id: str) -> dict[str, object]:
        state = self.runs.get(run_id)
        if state is None:
            return self.public(run_id)
        if state.status not in {"starting", "running"} or not state.process:
            return state.public(self.repository)
        state.status = "stopping"
        try:
            os.killpg(state.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(state.done.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                os.killpg(state.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await state.done.wait()
        return state.public(self.repository)

    async def stop_all(self) -> None:
        await asyncio.gather(
            *(self.stop(run_id) for run_id in list(self.active_ids)),
            return_exceptions=True,
        )

    async def events(self, run_id: str, after: int = 0):
        state = self.runs.get(run_id)
        if state is None:
            yield {"type": "status", **self.public(run_id)}
            return
        cursor = max(0, after)
        while True:
            while cursor < len(state.logs):
                line = state.logs[cursor]
                cursor += 1
                yield {"type": "log", "index": cursor, "line": line}
            if state.status not in ACTIVE_STATUSES:
                yield {"type": "status", **state.public(self.repository)}
                return
            await asyncio.sleep(0.25)


class RegressionManager:
    def __init__(self, repository: StudioRepository, run_manager: RunManager):
        self.repository = repository
        self.run_manager = run_manager
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.cancelled: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def active_ids(self) -> set[str]:
        return {
            batch_id
            for batch_id, task in self.tasks.items()
            if not task.done()
        }

    async def start(self, request: RegressionCreate) -> dict[str, object]:
        async with self._lock:
            if self.run_manager.active_ids or self.active_ids:
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
            batch = self.repository.create_regression_batch(
                bot_id=str(request.bot_id),
                baseline_revision=request.baseline_revision,
                suite_id=str(request.suite_id),
                games_per_scenario=request.games_per_scenario,
                concurrency=request.concurrency,
            )
            self.run_manager.batch_active = True
            self.tasks[batch["id"]] = asyncio.create_task(self._execute(batch["id"]))
            return batch

    async def _execute(self, batch_id: str) -> None:
        batch = self.repository.get_regression_batch(batch_id)
        self.repository.set_regression_batch_status(batch_id, "running")
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        for game in self.repository.list_queued_regression_games(batch_id):
            queue.put_nowait(game)

        async def worker() -> None:
            while not queue.empty() and batch_id not in self.cancelled:
                try:
                    game = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    state = await self.run_manager.start_regression_game(
                        batch["botId"], game
                    )
                    self.repository.set_regression_game(
                        game["id"], status="running", match_id=state.id
                    )
                    await state.done.wait()
                    terminal = self.repository.get_match(state.id)["status"]
                    self.repository.set_regression_game(
                        game["id"], status=terminal, match_id=state.id
                    )
                except Exception:
                    self.repository.set_regression_game(game["id"], status="failed")
                finally:
                    queue.task_done()

        try:
            await asyncio.gather(
                *(worker() for _ in range(int(batch["concurrency"])))
            )
            if batch_id in self.cancelled:
                self.repository.cancel_queued_regression_games(batch_id)
                self.repository.set_regression_batch_status(batch_id, "cancelled")
            else:
                self.repository.set_regression_batch_status(batch_id, "completed")
        except Exception:
            self.repository.set_regression_batch_status(batch_id, "failed")
            raise
        finally:
            self.run_manager.batch_active = False
            self.cancelled.discard(batch_id)

    async def cancel(self, batch_id: str) -> dict[str, object]:
        batch = self.repository.get_regression_batch(batch_id)
        if batch["status"] not in {"queued", "running", "starting"}:
            return batch
        self.cancelled.add(batch_id)
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
            batch = self.repository.get_regression_batch(batch_id)
            if batch["status"] != "interrupted":
                raise ConflictError("Only interrupted regression batches can be resumed.")
            if self.run_manager.active_ids or self.active_ids:
                raise ConflictError("A match or regression batch is already running.")
            self.run_manager.batch_active = True
            self.tasks[batch_id] = asyncio.create_task(self._execute(batch_id))
            return self.repository.get_regression_batch(batch_id)

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
