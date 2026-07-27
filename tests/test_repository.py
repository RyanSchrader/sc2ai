from pathlib import Path

import pytest

from studio.catalog import RaceName
from studio.models import BenchmarkSuiteCreate, BotCreate, BotUpdate, blank_strategy
from studio.repository import ConflictError, StudioRepository


@pytest.fixture()
def repository(tmp_path: Path) -> StudioRepository:
    repo = StudioRepository(tmp_path / "studio.db")
    repo.initialize()
    return repo


def test_seed_is_idempotent_and_contains_all_bots(repository: StudioRepository):
    assert len(repository.list_bots()) == 8
    repository.seed_builtins()
    assert len(repository.list_bots()) == 8


def test_create_edit_fork_trash_and_restore(repository: StudioRepository):
    created = repository.create_bot(
        BotCreate(
            name="Test Pressure",
            race=RaceName.TERRAN,
            strategy=blank_strategy(RaceName.TERRAN),
        )
    )
    updated = repository.update_bot(
        created["id"],
        BotUpdate(
            description="A revised test bot.",
            expected_revision=1,
            change_summary="Updated description",
        ),
    )
    assert updated["currentRevision"] == 2
    assert len(repository.list_revisions(created["id"])) == 2

    with pytest.raises(ConflictError):
        repository.update_bot(
            created["id"],
            BotUpdate(description="Stale write", expected_revision=1),
        )

    fork = repository.fork_bot(created["id"])
    assert fork["forkedFrom"] == created["id"]
    assert fork["currentRevision"] == 1

    repository.trash_bot(created["id"])
    assert created["id"] not in {bot["id"] for bot in repository.list_bots()}
    assert repository.get_bot(created["id"])["deletedAt"] is not None
    repository.restore_bot(created["id"])
    assert repository.get_bot(created["id"])["deletedAt"] is None


def test_revision_restore_creates_new_revision(repository: StudioRepository):
    bot = repository.get_bot("protoss-basic")
    changed = repository.update_bot(
        bot["id"],
        BotUpdate(
            description="Changed",
            expected_revision=bot["currentRevision"],
            change_summary="Changed metadata",
        ),
    )
    restored = repository.restore_revision(changed["id"], 1, "Restore v1")
    assert restored["currentRevision"] == 3
    assert restored["strategy"] == bot["strategy"]


def test_match_history_and_decisive_win_rate(repository: StudioRepository):
    bot = repository.get_bot("terran-basic")
    opponent = repository.get_bot("zerg-basic-1")

    def record(result: str, opponent_result: str, source: str = "single"):
        match = repository.create_match(
            map_name="TestMap",
            source=source,
            participants=[
                {
                    "participant_type": "bot",
                    "bot_id": bot["id"],
                    "bot_revision": 1,
                    "name": bot["name"],
                    "requested_race": bot["race"],
                    "resolved_race": bot["race"],
                    "difficulty": None,
                },
                {
                    "participant_type": "bot",
                    "bot_id": opponent["id"],
                    "bot_revision": 1,
                    "name": opponent["name"],
                    "requested_race": opponent["race"],
                    "resolved_race": opponent["race"],
                    "difficulty": None,
                },
            ],
        )
        repository.finalize_match(
            match["id"],
            status="completed",
            game_time_seconds=90,
            participant_results=[
                {"slot": 1, "result": result, "resolvedRace": bot["race"]},
                {
                    "slot": 2,
                    "result": opponent_result,
                    "resolvedRace": opponent["race"],
                },
            ],
        )

    record("victory", "defeat")
    record("defeat", "victory")
    record("tie", "tie", source="regression")

    stats = repository.bot_stats(bot["id"])
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["ties"] == 1
    assert stats["winRate"] == 0.5
    assert repository.bot_stats(bot["id"], include_regression=False)["ties"] == 0
    assert repository.bot_stats(opponent["id"])["wins"] == 1


def test_benchmark_suite_pins_revision_and_snapshots_regression(repository: StudioRepository):
    bot = repository.get_bot("terran-basic")
    bot = repository.update_bot(
        bot["id"],
        BotUpdate(
            description="Current candidate",
            expected_revision=1,
            change_summary="Candidate v2",
        ),
    )
    opponent = repository.get_bot("zerg-basic-1")
    suite = repository.create_benchmark_suite(
        BenchmarkSuiteCreate.model_validate(
            {
                "name": "Pinned core",
                "scenarios": [
                    {
                        "name": "Pinned Zerg",
                        "map_name": "TestMap",
                        "opponent_type": "bot",
                        "opponent_bot_id": opponent["id"],
                        "opponent_revision": 1,
                    }
                ],
            }
        )
    )
    batch = repository.create_regression_batch(
        bot_id=bot["id"],
        baseline_revision=1,
        suite_id=suite["id"],
        games_per_scenario=3,
        concurrency=2,
    )
    assert batch["candidateRevision"] == 2
    assert batch["baselineRevision"] == 1
    assert batch["totalGames"] == 6
    assert {game["randomSeed"] for game in batch["games"] if game["repetition"] == 1}
    assert len(
        {game["randomSeed"] for game in batch["games"] if game["repetition"] == 1}
    ) == 1
    assert all(game["opponentRevision"] == 1 for game in batch["games"])


def test_stopped_match_preserves_last_reported_game_time(repository: StudioRepository):
    bot = repository.get_bot("terran-basic")
    match = repository.create_match(
        map_name="TestMap",
        source="single",
        participants=[
            {
                "participant_type": "bot",
                "bot_id": bot["id"],
                "bot_revision": bot["currentRevision"],
                "name": bot["name"],
                "requested_race": bot["race"],
                "resolved_race": bot["race"],
                "difficulty": None,
            },
            {
                "participant_type": "computer",
                "bot_id": None,
                "bot_revision": None,
                "name": "Zerg Computer",
                "requested_race": "zerg",
                "resolved_race": "zerg",
                "difficulty": "easy",
            },
        ],
    )
    repository.set_match_running(match["id"])
    repository.update_match_game_time(match["id"], 732.5)
    stopped = repository.finalize_match(
        match["id"],
        status="stopped",
        failure_reason="Stopped by user",
    )

    assert stopped["gameTimeSeconds"] == 732.5


def test_cancelled_regression_games_do_not_count_as_completed(
    repository: StudioRepository,
):
    bot = repository.get_bot("terran-basic")
    bot = repository.update_bot(
        bot["id"],
        BotUpdate(
            description="Candidate",
            expected_revision=1,
            change_summary="Candidate v2",
        ),
    )
    suite = repository.create_benchmark_suite(
        BenchmarkSuiteCreate.model_validate(
            {
                "name": "Cancellation accounting",
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
        bot_id=bot["id"],
        baseline_revision=1,
        suite_id=suite["id"],
        games_per_scenario=2,
        concurrency=1,
    )

    repository.cancel_queued_regression_games(batch["id"])

    cancelled = repository.get_regression_batch(batch["id"])
    assert cancelled["completedGames"] == 0
    assert cancelled["pairedSamples"] == 0
    assert {game["status"] for game in cancelled["games"]} == {"cancelled"}


def test_regression_comparison_uses_only_completed_pairs(
    repository: StudioRepository,
):
    bot = repository.get_bot("terran-basic")
    bot = repository.update_bot(
        bot["id"],
        BotUpdate(
            description="Candidate",
            expected_revision=1,
            change_summary="Candidate v2",
        ),
    )
    suite = repository.create_benchmark_suite(
        BenchmarkSuiteCreate.model_validate(
            {
                "name": "Paired accounting",
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
        bot_id=bot["id"],
        baseline_revision=1,
        suite_id=suite["id"],
        games_per_scenario=2,
        concurrency=1,
    )

    def finish(game: dict, result: str) -> None:
        match = repository.create_match(
            map_name="TestMap",
            source="regression",
            participants=[
                {
                    "participant_type": "bot",
                    "bot_id": bot["id"],
                    "bot_revision": game["testedRevision"],
                    "name": bot["name"],
                    "requested_race": bot["race"],
                    "resolved_race": bot["race"],
                    "difficulty": None,
                },
                {
                    "participant_type": "computer",
                    "bot_id": None,
                    "bot_revision": None,
                    "name": "Zerg Computer",
                    "requested_race": "zerg",
                    "resolved_race": "zerg",
                    "difficulty": "easy",
                },
            ],
        )
        repository.finalize_match(
            match["id"],
            status="completed",
            game_time_seconds=90,
            participant_results=[
                {"slot": 1, "result": result, "resolvedRace": bot["race"]},
                {
                    "slot": 2,
                    "result": "defeat" if result == "victory" else "victory",
                    "resolvedRace": "zerg",
                },
            ],
        )
        repository.set_regression_game(
            game["id"], status="completed", match_id=match["id"]
        )

    games = batch["games"]
    first_pair = [game for game in games if game["repetition"] == 1]
    for game in first_pair:
        finish(game, "victory" if game["testedRole"] == "candidate" else "defeat")

    unpaired_candidate = next(
        game
        for game in games
        if game["repetition"] == 2 and game["testedRole"] == "candidate"
    )
    finish(unpaired_candidate, "victory")

    compared = repository.get_regression_batch(batch["id"])
    assert compared["pairedSamples"] == 1
    assert compared["comparison"]["candidate"]["wins"] == 1
    assert compared["comparison"]["baseline"]["losses"] == 1
    assert compared["scenarioComparisons"][0]["pairedSamples"] == 1
