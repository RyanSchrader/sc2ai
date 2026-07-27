import asyncio
from pathlib import Path

import pytest

from studio.models import (
    BenchmarkSuiteCreate,
    BotUpdate,
    MatchCreate,
    RegressionCreate,
)
from studio.repository import StudioRepository
from studio.runs import RegressionManager, RunManager


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
