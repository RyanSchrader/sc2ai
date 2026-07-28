import asyncio
import sqlite3
from pathlib import Path

import pytest

from studio.models import (
    BenchmarkSuiteCreate,
    BotUpdate,
    MatchCreate,
    RegressionCreate,
)
from studio.repository import ConflictError, StudioRepository
from studio.runs import (
    MAX_RETAINED_LOGS,
    RegressionManager,
    RunManager,
    RunState,
)


class FakeStdout:
    def __init__(self):
        self.lines = [
            b"starting fake match\n",
            (
                b'SC2_STUDIO_RESULT:{"gameTimeSeconds":123.5,"participants":'
                b'[{"slot":1,"result":"victory","resolvedRace":"terran"},'
                b'{"slot":2,"result":"defeat","resolvedRace":"zerg"}]}\n'
            ),
            b"",
        ]

    async def readline(self):
        return self.lines.pop(0)


class FakeProcess:
    pid = 99999

    def __init__(self):
        self.stdout = FakeStdout()

    async def wait(self):
        return 0


class BrokenStdout:
    async def readline(self):
        raise RuntimeError("stream disappeared")


class BrokenProcess:
    pid = None
    stdout = BrokenStdout()

    def __init__(self):
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    async def wait(self):
        return self.returncode


class BlockingStdout:
    def __init__(self, release: asyncio.Event):
        self.release = release

    async def readline(self):
        await self.release.wait()
        return b""


class BlockingProcess:
    pid = 99997

    def __init__(self, release: asyncio.Event):
        self.stdout = BlockingStdout(release)

    async def wait(self):
        return 0


class TerminableBlockingProcess:
    pid = None

    def __init__(self):
        self.release = asyncio.Event()
        self.stdout = BlockingStdout(self.release)
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.release.set()

    def kill(self):
        self.terminate()

    async def wait(self):
        await self.release.wait()
        return self.returncode


@pytest.mark.asyncio
async def test_run_manager_uses_argument_array_and_streams_logs(tmp_path: Path, monkeypatch):
    repository = StudioRepository(tmp_path / "runs.db")
    repository.initialize()
    bot = repository.get_bot("terran-basic")
    manager = RunManager(repository)
    captured: list[object] = []

    async def fake_subprocess(*arguments, **options):
        captured.extend(arguments)
        captured.append(options)
        return FakeProcess()

    monkeypatch.setattr("studio.runs.discover_maps", lambda: ["TestMap"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)

    started = await manager.start(
        MatchCreate(
            bot_id=bot["id"],
            map_name="TestMap",
            enemy_race="zerg",
            difficulty="easy",
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    state = manager.get(started["id"])
    assert state.status == "completed"
    assert "starting fake match" in state.logs
    recorded = repository.get_match(started["id"])
    assert recorded["participants"][0]["result"] == "victory"
    assert recorded["gameTimeSeconds"] == 123.5
    assert "--bot" in captured
    assert bot["id"] in captured
    assert "--bot-revision-id" in captured
    assert bot["currentRevisionId"] in captured
    assert "--bot-revision-digest" in captured
    assert bot["currentRevisionDigest"] in captured
    assert isinstance(captured[-1], dict)
    assert captured[-1]["start_new_session"] is True


@pytest.mark.asyncio
async def test_bot_opponent_and_parallel_regression_queue(
    tmp_path: Path, monkeypatch
):
    repository = StudioRepository(tmp_path / "regression.db")
    repository.initialize()
    candidate = repository.get_bot("terran-basic")
    candidate = repository.update_bot(
        candidate["id"],
        BotUpdate(
            description="Candidate",
            expected_revision=1,
            change_summary="Candidate v2",
        ),
    )
    opponent = repository.get_bot("zerg-basic-1")
    suite = repository.create_benchmark_suite(
        BenchmarkSuiteCreate.model_validate(
            {
                "name": "Core",
                "scenarios": [
                    {
                        "name": "Pinned opponent",
                        "map_name": "TestMap",
                        "opponent_type": "bot",
                        "opponent_bot_id": opponent["id"],
                        "opponent_revision": 1,
                    }
                ],
            }
        )
    )
    launched: list[tuple[object, ...]] = []

    async def fake_subprocess(*arguments, **_options):
        launched.append(arguments)
        return FakeProcess()

    monkeypatch.setattr("studio.runs.discover_maps", lambda: ["TestMap"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    run_manager = RunManager(repository)
    regressions = RegressionManager(repository, run_manager)

    batch = await regressions.start(
        RegressionCreate(
            bot_id=candidate["id"],
            baseline_revision=1,
            suite_id=suite["id"],
            games_per_scenario=2,
            concurrency=2,
        )
    )
    await regressions.tasks[batch["id"]]
    finished = repository.get_regression_batch(batch["id"])

    assert finished["status"] == "completed"
    assert finished["completedGames"] == 4
    assert len(launched) == 4
    assert all("--opponent-bot" in command for command in launched)
    assert {game["testedRevision"] for game in finished["games"]} == {1, 2}


@pytest.mark.asyncio
async def test_run_monitor_failure_becomes_a_persisted_terminal_state(
    tmp_path: Path, monkeypatch
):
    repository = StudioRepository(tmp_path / "broken-stream.db")
    repository.initialize()
    bot = repository.get_bot("terran-basic")
    manager = RunManager(repository)
    process = BrokenProcess()

    async def fake_subprocess(*_arguments, **_options):
        return process

    monkeypatch.setattr("studio.runs.discover_maps", lambda: ["TestMap"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)

    started = await manager.start(
        MatchCreate(
            bot_id=bot["id"],
            map_name="TestMap",
            enemy_race="zerg",
            difficulty="easy",
        )
    )
    state = manager.get(started["id"])
    await asyncio.wait_for(state.done.wait(), timeout=1)
    await asyncio.sleep(0)

    persisted = repository.get_match(state.id)
    assert state.status == "failed"
    assert persisted["status"] == "failed"
    assert "Match monitor failed: RuntimeError: stream disappeared" in (
        persisted["failureReason"] or ""
    )
    assert state.id not in manager.active_ids
    assert state.id not in manager._drain_tasks
    assert process.terminated is True
    assert process.returncode == -15


@pytest.mark.asyncio
async def test_launch_failure_releases_scheduler_and_persists_diagnostics(
    tmp_path: Path, monkeypatch
):
    repository = StudioRepository(tmp_path / "launch-failure.db")
    repository.initialize()
    bot = repository.get_bot("terran-basic")
    manager = RunManager(repository)

    async def fake_subprocess(*_arguments, **_options):
        raise OSError("SC2 executable missing")

    monkeypatch.setattr("studio.runs.discover_maps", lambda: ["TestMap"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)

    with pytest.raises(OSError, match="SC2 executable missing"):
        await manager.start(
            MatchCreate(
                bot_id=bot["id"],
                map_name="TestMap",
                enemy_race="zerg",
                difficulty="easy",
            )
        )

    state = next(iter(manager.runs.values()))
    persisted = repository.get_match(state.id)
    assert state.done.is_set()
    assert manager.active_ids == set()
    assert persisted["status"] == "failed"
    assert persisted["failureReason"] == (
        "Could not launch match process: SC2 executable missing"
    )


@pytest.mark.asyncio
async def test_trimmed_log_stream_reports_gap_and_absolute_sequences(tmp_path: Path):
    repository = StudioRepository(tmp_path / "logs.db")
    repository.initialize()
    manager = RunManager(repository)
    state = RunState(
        id="retained-run",
        bot_id="bot",
        bot_name="Bot",
        bot_revision=1,
        map_name="TestMap",
        opponent_type="computer",
        enemy_race="zerg",
        difficulty="easy",
    )
    for index in range(MAX_RETAINED_LOGS + 1):
        state.append_log(f"line {index + 1}")
    manager.runs[state.id] = state

    stream = manager.events(state.id)
    gap = await anext(stream)
    first_log = await anext(stream)
    await stream.aclose()

    assert gap == {
        "type": "log_gap",
        "after": 0,
        "firstAvailable": 2,
        "lastDropped": 1,
    }
    assert first_log == {
        "type": "log",
        "sequence": 2,
        "index": 2,
        "line": "line 2",
    }


@pytest.mark.asyncio
async def test_single_match_and_regression_share_one_atomic_admission_gate(
    tmp_path: Path, monkeypatch
):
    repository = StudioRepository(tmp_path / "admission.db")
    repository.initialize()
    candidate = repository.get_bot("terran-basic")
    candidate = repository.update_bot(
        candidate["id"],
        BotUpdate(
            description="Candidate",
            expected_revision=1,
            change_summary="Candidate v2",
        ),
    )
    suite = repository.create_benchmark_suite(
        BenchmarkSuiteCreate.model_validate(
            {
                "name": "Admission",
                "scenarios": [
                    {
                        "name": "Computer",
                        "map_name": "TestMap",
                        "opponent_type": "computer",
                        "enemy_race": "zerg",
                        "difficulty": "easy",
                    }
                ],
            }
        )
    )
    spawn_entered = asyncio.Event()
    allow_spawn = asyncio.Event()
    release = asyncio.Event()

    async def fake_subprocess(*_arguments, **_options):
        spawn_entered.set()
        await allow_spawn.wait()
        return BlockingProcess(release)

    monkeypatch.setattr("studio.runs.discover_maps", lambda: ["TestMap"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runs = RunManager(repository)
    regressions = RegressionManager(repository, runs)
    single = MatchCreate(
        bot_id=candidate["id"],
        map_name="TestMap",
        enemy_race="zerg",
        difficulty="easy",
    )
    regression = RegressionCreate(
        bot_id=candidate["id"],
        baseline_revision=1,
        suite_id=suite["id"],
        games_per_scenario=1,
        concurrency=1,
    )

    single_task = asyncio.create_task(runs.start(single))
    await asyncio.wait_for(spawn_entered.wait(), timeout=1)
    regression_task = asyncio.create_task(regressions.start(regression))
    await asyncio.sleep(0)
    allow_spawn.set()
    outcomes = await asyncio.gather(
        single_task,
        regression_task,
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, ConflictError) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1

    release.set()
    if isinstance(outcomes[0], dict):
        await asyncio.wait_for(runs.get(outcomes[0]["id"]).done.wait(), timeout=1)
    if isinstance(outcomes[1], dict):
        await asyncio.wait_for(regressions.tasks[outcomes[1]["id"]], timeout=1)


@pytest.mark.asyncio
async def test_regression_finishes_with_failures_instead_of_reporting_success(
    tmp_path: Path, monkeypatch
):
    repository = StudioRepository(tmp_path / "partial-regression.db")
    repository.initialize()
    candidate = repository.get_bot("terran-basic")
    candidate = repository.update_bot(
        candidate["id"],
        BotUpdate(
            description="Candidate",
            expected_revision=1,
            change_summary="Candidate v2",
        ),
    )
    suite = repository.create_benchmark_suite(
        BenchmarkSuiteCreate.model_validate(
            {
                "name": "Failure visibility",
                "scenarios": [
                    {
                        "name": "Computer",
                        "map_name": "TestMap",
                        "opponent_type": "computer",
                        "enemy_race": "zerg",
                        "difficulty": "easy",
                    }
                ],
            }
        )
    )
    release = asyncio.Event()
    release.set()

    async def fake_subprocess(*_arguments, **_options):
        return BlockingProcess(release)

    monkeypatch.setattr("studio.runs.discover_maps", lambda: ["TestMap"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    run_manager = RunManager(repository)
    regressions = RegressionManager(repository, run_manager)

    batch = await regressions.start(
        RegressionCreate(
            bot_id=candidate["id"],
            baseline_revision=1,
            suite_id=suite["id"],
            games_per_scenario=1,
            concurrency=1,
        )
    )
    await regressions.tasks[batch["id"]]
    finished = repository.get_regression_batch(batch["id"])

    assert finished["status"] == "completed_with_failures"
    assert finished["completedGames"] == finished["totalGames"] == 2
    assert finished["finishedAt"] is not None
    assert {game["status"] for game in finished["games"]} == {"failed"}


@pytest.mark.asyncio
async def test_cancelling_a_pending_launch_releases_admission_and_persists_failure(
    tmp_path: Path, monkeypatch
):
    repository = StudioRepository(tmp_path / "cancelled-launch.db")
    repository.initialize()
    bot = repository.get_bot("terran-basic")
    manager = RunManager(repository)
    spawn_entered = asyncio.Event()
    spawn_cancelled = asyncio.Event()
    never_release = asyncio.Event()

    async def fake_subprocess(*_arguments, **_options):
        spawn_entered.set()
        try:
            await never_release.wait()
        except asyncio.CancelledError:
            spawn_cancelled.set()
            raise

    monkeypatch.setattr("studio.runs.discover_maps", lambda: ["TestMap"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)

    launch = asyncio.create_task(
        manager.start(
            MatchCreate(
                bot_id=bot["id"],
                map_name="TestMap",
                enemy_race="zerg",
                difficulty="easy",
            )
        )
    )
    await asyncio.wait_for(spawn_entered.wait(), timeout=1)
    state = next(iter(manager.runs.values()))
    launch.cancel()

    with pytest.raises(asyncio.CancelledError):
        await launch

    persisted = repository.get_match(state.id)
    assert spawn_cancelled.is_set()
    assert state.done.is_set()
    assert manager.active_ids == set()
    assert manager._drain_tasks == {}
    assert persisted["status"] == "failed"
    assert persisted["failureReason"] == (
        "Match launch was cancelled before process creation."
    )


@pytest.mark.asyncio
async def test_regression_cancel_stops_pending_launch_and_blocks_queued_spawns(
    tmp_path: Path, monkeypatch
):
    repository = StudioRepository(tmp_path / "cancelled-regression.db")
    repository.initialize()
    candidate = repository.get_bot("terran-basic")
    candidate = repository.update_bot(
        candidate["id"],
        BotUpdate(
            description="Candidate",
            expected_revision=1,
            change_summary="Candidate v2",
        ),
    )
    suite = repository.create_benchmark_suite(
        BenchmarkSuiteCreate.model_validate(
            {
                "name": "Cancellation",
                "scenarios": [
                    {
                        "name": "Computer",
                        "map_name": "TestMap",
                        "opponent_type": "computer",
                        "enemy_race": "zerg",
                        "difficulty": "easy",
                    }
                ],
            }
        )
    )
    spawn_entered = asyncio.Event()
    never_release = asyncio.Event()
    spawn_calls = 0

    async def fake_subprocess(*_arguments, **_options):
        nonlocal spawn_calls
        spawn_calls += 1
        spawn_entered.set()
        await never_release.wait()

    monkeypatch.setattr("studio.runs.discover_maps", lambda: ["TestMap"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runs = RunManager(repository)
    regressions = RegressionManager(repository, runs)

    batch = await regressions.start(
        RegressionCreate(
            bot_id=candidate["id"],
            baseline_revision=1,
            suite_id=suite["id"],
            games_per_scenario=1,
            concurrency=2,
        )
    )
    await asyncio.wait_for(spawn_entered.wait(), timeout=1)
    finished = await asyncio.wait_for(regressions.cancel(batch["id"]), timeout=2)

    assert finished["status"] == "cancelled"
    assert spawn_calls == 1
    assert runs.active_ids == set()
    assert runs.batch_active is False
    assert {game["status"] for game in finished["games"]} <= {
        "stopped",
        "cancelled",
    }
    assert any(game["status"] == "stopped" for game in finished["games"])


@pytest.mark.asyncio
async def test_regression_launch_failure_keeps_game_linked_to_diagnostic_match(
    tmp_path: Path, monkeypatch
):
    repository = StudioRepository(tmp_path / "linked-launch-failure.db")
    repository.initialize()
    candidate = repository.get_bot("terran-basic")
    candidate = repository.update_bot(
        candidate["id"],
        BotUpdate(
            description="Candidate",
            expected_revision=1,
            change_summary="Candidate v2",
        ),
    )
    suite = repository.create_benchmark_suite(
        BenchmarkSuiteCreate.model_validate(
            {
                "name": "Launch diagnostics",
                "scenarios": [
                    {
                        "name": "Computer",
                        "map_name": "TestMap",
                        "opponent_type": "computer",
                        "enemy_race": "zerg",
                        "difficulty": "easy",
                    }
                ],
            }
        )
    )

    async def fake_subprocess(*_arguments, **_options):
        raise OSError("runner unavailable")

    monkeypatch.setattr("studio.runs.discover_maps", lambda: ["TestMap"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runs = RunManager(repository)
    regressions = RegressionManager(repository, runs)

    batch = await regressions.start(
        RegressionCreate(
            bot_id=candidate["id"],
            baseline_revision=1,
            suite_id=suite["id"],
            games_per_scenario=1,
            concurrency=2,
        )
    )
    await regressions.tasks[batch["id"]]
    finished = repository.get_regression_batch(batch["id"])

    assert finished["status"] == "completed_with_failures"
    assert {game["status"] for game in finished["games"]} == {"failed"}
    assert all(game["matchId"] for game in finished["games"])
    for game in finished["games"]:
        diagnostic = repository.get_match(game["matchId"])
        assert diagnostic["status"] == "failed"
        assert diagnostic["failureReason"] == (
            "Could not launch match process: runner unavailable"
        )


@pytest.mark.asyncio
async def test_completed_run_logs_survive_manager_restart_with_gap_metadata(
    tmp_path: Path, monkeypatch
):
    repository = StudioRepository(tmp_path / "durable-logs.db")
    repository.initialize()
    bot = repository.get_bot("terran-basic")
    manager = RunManager(repository)

    async def fake_subprocess(*_arguments, **_options):
        return FakeProcess()

    monkeypatch.setattr("studio.runs.discover_maps", lambda: ["TestMap"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)

    started = await manager.start(
        MatchCreate(
            bot_id=bot["id"],
            map_name="TestMap",
            enemy_race="zerg",
            difficulty="easy",
        )
    )
    await asyncio.wait_for(manager.get(started["id"]).done.wait(), timeout=1)
    bounds = repository.get_match_log_bounds(started["id"])
    assert bounds["lastSequence"] == 3
    for sequence in range(4, 7):
        repository.append_match_log(
            started["id"],
            sequence,
            f"durable {sequence}",
            retain=3,
        )

    restarted = RunManager(repository)
    events = [
        event async for event in restarted.events(started["id"], after=0)
    ]

    assert events[0] == {
        "type": "log_gap",
        "after": 0,
        "firstAvailable": 4,
        "lastDropped": 3,
    }
    assert [
        event["sequence"] for event in events if event["type"] == "log"
    ] == [4, 5, 6]
    assert events[-1]["type"] == "status"
    assert events[-1]["logCount"] == 6
    assert events[-1]["firstLogSequence"] == 4


@pytest.mark.asyncio
async def test_restart_requeues_starting_regression_games_before_resume(
    tmp_path: Path, monkeypatch
):
    repository = StudioRepository(tmp_path / "resume-starting.db")
    repository.initialize()
    candidate = repository.get_bot("terran-basic")
    candidate = repository.update_bot(
        candidate["id"],
        BotUpdate(
            description="Candidate",
            expected_revision=1,
            change_summary="Candidate v2",
        ),
    )
    suite = repository.create_benchmark_suite(
        BenchmarkSuiteCreate.model_validate(
            {
                "name": "Restart recovery",
                "scenarios": [
                    {
                        "name": "Computer",
                        "map_name": "TestMap",
                        "opponent_type": "computer",
                        "enemy_race": "zerg",
                        "difficulty": "easy",
                    }
                ],
            }
        )
    )
    batch = repository.create_regression_batch(
        bot_id=candidate["id"],
        baseline_revision=1,
        suite_id=suite["id"],
        games_per_scenario=1,
        concurrency=2,
    )
    repository.set_regression_batch_status(batch["id"], "running")
    failed_attempt_ids: list[str] = []
    for game in repository.list_queued_regression_games(batch["id"]):
        tested = repository.get_bot_revision(
            candidate["id"], game["testedRevision"]
        )
        attempt = repository.create_match(
            map_name=game["mapName"],
            source="regression",
            regression_batch_id=batch["id"],
            regression_game_id=game["id"],
            participants=[
                {
                    "participant_type": "bot",
                    "bot_id": tested["id"],
                    "bot_revision": tested["selectedRevision"],
                    "name": tested["name"],
                    "requested_race": tested["race"],
                    "resolved_race": tested["race"],
                    "difficulty": None,
                },
                {
                    "participant_type": "computer",
                    "bot_id": None,
                    "bot_revision": None,
                    "name": "Zerg Computer",
                    "requested_race": "zerg",
                    "resolved_race": None,
                    "difficulty": "easy",
                },
            ],
        )
        failed_attempt_ids.append(attempt["id"])

    repository.interrupt_active_matches()
    interrupted = repository.get_regression_batch(batch["id"])
    assert interrupted["status"] == "interrupted"
    assert {game["status"] for game in interrupted["games"]} == {"queued"}
    assert all(game["matchId"] is None for game in interrupted["games"])
    assert {
        repository.get_match(match_id)["status"]
        for match_id in failed_attempt_ids
    } == {"failed"}

    async def fake_subprocess(*_arguments, **_options):
        return FakeProcess()

    monkeypatch.setattr("studio.runs.discover_maps", lambda: ["TestMap"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runs = RunManager(repository)
    regressions = RegressionManager(repository, runs)

    resumed = await regressions.resume(batch["id"])
    await regressions.tasks[resumed["id"]]
    finished = repository.get_regression_batch(batch["id"])

    assert finished["status"] == "completed"
    assert finished["completedGames"] == finished["totalGames"] == 2
    assert {game["status"] for game in finished["games"]} == {"completed"}
    assert all(game["matchId"] for game in finished["games"])


@pytest.mark.asyncio
async def test_batch_never_reports_completed_with_unfinished_games(tmp_path: Path):
    repository = StudioRepository(tmp_path / "terminal-invariant.db")
    repository.initialize()
    candidate = repository.get_bot("terran-basic")
    candidate = repository.update_bot(
        candidate["id"],
        BotUpdate(
            description="Candidate",
            expected_revision=1,
            change_summary="Candidate v2",
        ),
    )
    suite = repository.create_benchmark_suite(
        BenchmarkSuiteCreate.model_validate(
            {
                "name": "Terminal invariant",
                "scenarios": [
                    {
                        "name": "Computer",
                        "map_name": "TestMap",
                        "opponent_type": "computer",
                        "enemy_race": "zerg",
                        "difficulty": "easy",
                    }
                ],
            }
        )
    )
    batch = repository.create_regression_batch(
        bot_id=candidate["id"],
        baseline_revision=1,
        suite_id=suite["id"],
        games_per_scenario=1,
        concurrency=1,
    )
    for game in batch["games"]:
        repository.set_regression_game(game["id"], status="starting")
    repository.set_regression_batch_status(batch["id"], "interrupted")
    runs = RunManager(repository)
    regressions = RegressionManager(repository, runs)

    resumed = await regressions.resume(batch["id"])
    await regressions.tasks[resumed["id"]]

    finished = repository.get_regression_batch(batch["id"])
    assert finished["status"] == "failed"
    assert finished["completedGames"] == 0
    assert {game["status"] for game in finished["games"]} == {"starting"}


@pytest.mark.asyncio
async def test_regression_metadata_failure_stops_the_acquired_process(
    tmp_path: Path, monkeypatch
):
    repository = StudioRepository(tmp_path / "post-launch-failure.db")
    repository.initialize()
    candidate = repository.get_bot("terran-basic")
    candidate = repository.update_bot(
        candidate["id"],
        BotUpdate(
            description="Candidate",
            expected_revision=1,
            change_summary="Candidate v2",
        ),
    )
    suite = repository.create_benchmark_suite(
        BenchmarkSuiteCreate.model_validate(
            {
                "name": "Metadata failure",
                "scenarios": [
                    {
                        "name": "Computer",
                        "map_name": "TestMap",
                        "opponent_type": "computer",
                        "enemy_race": "zerg",
                        "difficulty": "easy",
                    }
                ],
            }
        )
    )
    first_process = TerminableBlockingProcess()
    launches = 0

    async def fake_subprocess(*_arguments, **_options):
        nonlocal launches
        launches += 1
        return first_process if launches == 1 else FakeProcess()

    original_set_game = repository.set_regression_game
    injected = False

    def flaky_set_game(game_id: str, *, status: str, match_id: str | None = None):
        nonlocal injected
        if status == "running" and not injected:
            injected = True
            raise sqlite3.OperationalError("transient metadata write failure")
        return original_set_game(game_id, status=status, match_id=match_id)

    monkeypatch.setattr("studio.runs.discover_maps", lambda: ["TestMap"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(repository, "set_regression_game", flaky_set_game)
    runs = RunManager(repository)
    regressions = RegressionManager(repository, runs)

    batch = await regressions.start(
        RegressionCreate(
            bot_id=candidate["id"],
            baseline_revision=1,
            suite_id=suite["id"],
            games_per_scenario=1,
            concurrency=1,
        )
    )
    await asyncio.wait_for(regressions.tasks[batch["id"]], timeout=2)
    finished = repository.get_regression_batch(batch["id"])

    assert finished["status"] == "completed_with_failures"
    assert first_process.terminated is True
    assert first_process.returncode == -15
    assert runs.active_ids == set()
    assert runs._drain_tasks == {}
    assert runs.batch_active is False
