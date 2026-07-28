from argparse import Namespace
from pathlib import Path

import pytest

import run_bot
from studio.repository import StudioRepository


def _arguments(database: Path, **overrides: object) -> Namespace:
    values: dict[str, object] = {
        "bot": "terran-basic",
        "enemy_race": "zerg",
        "difficulty": "easy",
        "opponent_bot": None,
        "bot_revision": None,
        "opponent_revision": None,
        "bot_revision_id": None,
        "bot_revision_digest": None,
        "opponent_revision_id": None,
        "opponent_revision_digest": None,
        "random_seed": None,
        "match_id": None,
        "map_name": "TestMap",
        "list_bots": False,
        "database": database,
    }
    values.update(overrides)
    return Namespace(**values)


@pytest.mark.parametrize(
    ("argument", "error_fragment"),
    [
        ("bot_revision_id", "expected identity"),
        ("bot_revision_digest", "expected content-digest"),
    ],
)
def test_runner_rejects_changed_primary_revision_before_loading_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argument: str,
    error_fragment: str,
):
    database = tmp_path / "runner.db"
    repository = StudioRepository(database)
    repository.initialize()
    bot = repository.get_bot("terran-basic")

    monkeypatch.setattr(
        run_bot,
        "parse_args",
        lambda: _arguments(
            database,
            bot_revision=bot["currentRevision"],
            **{argument: "unexpected"},
        ),
    )
    monkeypatch.setattr(
        run_bot.maps,
        "get",
        lambda _name: pytest.fail("map loading must not happen after a mismatch"),
    )

    assert run_bot.main() == 2
    assert error_fragment in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argument", "error_fragment"),
    [
        ("opponent_revision_id", "expected identity"),
        ("opponent_revision_digest", "expected content-digest"),
    ],
)
def test_runner_rejects_changed_opponent_revision_before_starting_sc2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argument: str,
    error_fragment: str,
):
    database = tmp_path / "opponent-runner.db"
    repository = StudioRepository(database)
    repository.initialize()
    primary = repository.get_bot("terran-basic")
    opponent = repository.get_bot("zerg-basic-1")

    monkeypatch.setattr(
        run_bot,
        "parse_args",
        lambda: _arguments(
            database,
            bot=primary["id"],
            bot_revision=primary["currentRevision"],
            opponent_bot=opponent["id"],
            opponent_revision=opponent["currentRevision"],
            **{argument: "unexpected"},
        ),
    )
    monkeypatch.setattr(run_bot.maps, "get", lambda _name: object())
    monkeypatch.setattr(
        run_bot,
        "run_game",
        lambda *_args, **_kwargs: pytest.fail(
            "SC2 must not start after an opponent revision mismatch"
        ),
    )

    assert run_bot.main() == 2
    assert error_fragment in capsys.readouterr().err
