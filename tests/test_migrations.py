from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from uuid import UUID

import pytest

import studio.migrations as migrations_module
import studio.repository as repository_module
from studio.catalog import RaceName
from studio.migrations import (
    LATEST_SCHEMA_VERSION,
    MigrationError,
    apply_migrations,
    canonical_json,
    content_digest,
)
from studio.models import (
    BenchmarkSuiteCreate,
    BotUpdate,
    StrategyProposal,
    blank_strategy,
)
from studio.repository import BUILTIN_FIXTURES, StudioRepository


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _insert_legacy_bot(
    connection: sqlite3.Connection,
    *,
    bot_id: str = "legacy-bot",
    revision_id: str = "legacy-revision",
) -> tuple[str, str]:
    strategy_json = blank_strategy(RaceName.TERRAN).model_dump_json()
    now = "2026-01-01T00:00:00+00:00"
    connection.execute(
        """
        INSERT INTO bots (
            id, slug, name, description, race, tags_json, is_builtin,
            current_revision, created_at, updated_at
        ) VALUES (?, 'legacy', 'Legacy', '', 'terran', '[]', 0, 1, ?, ?)
        """,
        (bot_id, now, now),
    )
    connection.execute(
        """
        INSERT INTO revisions (
            id, bot_id, number, strategy_json, summary, created_at
        ) VALUES (?, ?, 1, ?, 'Legacy revision', ?)
        """,
        (revision_id, bot_id, strategy_json, now),
    )
    return strategy_json, revision_id


def test_empty_database_migrates_in_order_and_is_idempotent(tmp_path: Path):
    database = tmp_path / "empty.db"
    repository = StudioRepository(database)

    repository.initialize(seed=False)

    assert repository.last_migration_report is not None
    assert repository.last_migration_report.applied_versions == (1, 2, 3, 4)
    assert repository.last_migration_backup is None
    with repository.connect() as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1, 2, 3, 4]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            LATEST_SCHEMA_VERSION
        )
        revision_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(revisions)")
        }
        participant_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(match_participants)")
        }
        assert "content_digest" in revision_columns
        assert {"bot_revision_id", "bot_revision_digest"} <= participant_columns
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    repository.initialize(seed=False)
    assert repository.last_migration_report is not None
    assert repository.last_migration_report.applied_versions == ()
    assert not (tmp_path / "backups").exists()


def test_concurrent_migration_callers_recheck_the_ledger_under_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database = tmp_path / "concurrent.db"
    original_migrations = migrations_module.MIGRATIONS
    first_migration_entered = threading.Event()
    release_first_migration = threading.Event()
    second_lock_attempted = threading.Event()
    gate_lock = threading.Lock()
    gate_calls = 0

    def gated_base_schema(connection: sqlite3.Connection) -> None:
        nonlocal gate_calls
        original_migrations[0].apply(connection)
        with gate_lock:
            gate_calls += 1
            is_first_call = gate_calls == 1
        if is_first_call:
            first_migration_entered.set()
            if not release_first_migration.wait(timeout=5):
                raise AssertionError("Timed out waiting to release migration gate.")

    monkeypatch.setattr(
        migrations_module,
        "MIGRATIONS",
        (
            migrations_module.Migration(
                1,
                original_migrations[0].name,
                gated_base_schema,
            ),
            *original_migrations[1:],
        ),
    )

    reports: dict[str, migrations_module.MigrationReport] = {}
    errors: list[BaseException] = []
    result_lock = threading.Lock()

    def migrate(name: str, *, observe_lock: bool = False) -> None:
        connection = _connect(database)
        if observe_lock:
            connection.set_trace_callback(
                lambda statement: (
                    second_lock_attempted.set()
                    if statement.strip().upper().startswith("BEGIN IMMEDIATE")
                    else None
                )
            )
        try:
            report = migrations_module.apply_migrations(connection)
            with result_lock:
                reports[name] = report
        except BaseException as exc:
            with result_lock:
                errors.append(exc)
        finally:
            connection.close()

    first = threading.Thread(target=migrate, args=("first",), daemon=True)
    second = threading.Thread(
        target=migrate,
        args=("second",),
        kwargs={"observe_lock": True},
        daemon=True,
    )
    first.start()
    assert first_migration_entered.wait(timeout=5)
    second.start()
    try:
        assert second_lock_attempted.wait(timeout=5)
    finally:
        release_first_migration.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert set(reports) == {"first", "second"}
    assert sorted(
        version
        for report in reports.values()
        for version in report.applied_versions
    ) == [1, 2, 3, 4]
    with _connect(database) as connection:
        assert [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == [1, 2, 3, 4]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_match_log_migration_is_durable_append_only_and_cascades(
    tmp_path: Path,
):
    database = tmp_path / "match-logs.db"
    with _connect(database) as connection:
        initial_report = apply_migrations(connection, target_version=3)
        assert initial_report.applied_versions == (1, 2, 3)
        connection.execute(
            """
            INSERT INTO matches (
                id, source, map_name, status, created_at
            ) VALUES (
                'logged-match', 'single', 'TestMap', 'completed',
                '2026-01-01T00:00:00+00:00'
            )
            """
        )
        connection.commit()

        report = apply_migrations(connection)
        assert report.from_version == 3
        assert report.to_version == 4
        assert report.applied_versions == (4,)
        assert {
            row["name"]
            for row in connection.execute("PRAGMA table_info(match_logs)")
        } == {"match_id", "sequence", "line", "created_at"}
        assert connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'index'
              AND name = 'idx_match_logs_match_created'
            """
        ).fetchone() is not None

        connection.executemany(
            """
            INSERT INTO match_logs (match_id, sequence, line, created_at)
            VALUES ('logged-match', ?, ?, ?)
            """,
            (
                (1, "first", "2026-01-01T00:00:01+00:00"),
                (2, "second", "2026-01-01T00:00:02+00:00"),
            ),
        )
        connection.commit()
        assert [
            (row["sequence"], row["line"])
            for row in connection.execute(
                """
                SELECT sequence, line FROM match_logs
                WHERE match_id = 'logged-match'
                ORDER BY sequence
                """
            )
        ] == [(1, "first"), (2, "second")]

        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                """
                INSERT INTO match_logs (match_id, sequence, line, created_at)
                VALUES (
                    'logged-match', 2, 'duplicate',
                    '2026-01-01T00:00:03+00:00'
                )
                """
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                """
                UPDATE match_logs SET line = 'changed'
                WHERE match_id = 'logged-match' AND sequence = 1
                """
            )
        connection.rollback()

        # Retention deletes remain legal even though recorded lines cannot be
        # rewritten, and deleting a match removes its stored log.
        connection.execute(
            """
            DELETE FROM match_logs
            WHERE match_id = 'logged-match' AND sequence = 1
            """
        )
        connection.commit()
        assert connection.execute(
            "SELECT sequence FROM match_logs WHERE match_id = 'logged-match'"
        ).fetchone()[0] == 2
        connection.execute("DELETE FROM matches WHERE id = 'logged-match'")
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM match_logs"
        ).fetchone()[0] == 0


def test_unversioned_current_schema_is_backed_up_and_backfilled(tmp_path: Path):
    database = tmp_path / "legacy.db"
    with _connect(database) as connection:
        apply_migrations(connection, target_version=1)
        strategy_json, revision_id = _insert_legacy_bot(connection)
        connection.execute(
            """
            INSERT INTO matches (
                id, source, map_name, status, created_at
            ) VALUES ('legacy-match', 'single', 'LegacyMap', 'completed', ?)
            """,
            ("2026-01-01T00:00:00+00:00",),
        )
        connection.execute(
            """
            INSERT INTO match_participants (
                match_id, slot, participant_type, bot_id, bot_revision,
                name, requested_race
            ) VALUES (
                'legacy-match', 1, 'bot', 'legacy-bot', 1, 'Legacy', 'terran'
            )
            """
        )
        connection.execute("DROP TABLE schema_migrations")
        connection.execute("PRAGMA user_version = 0")

    repository = StudioRepository(database)
    repository.initialize(seed=False)

    assert repository.last_migration_report is not None
    assert repository.last_migration_report.from_version == 0
    assert repository.last_migration_report.applied_versions == (1, 2, 3, 4)
    assert repository.last_migration_backup is not None
    assert repository.last_migration_backup.exists()
    with _connect(repository.last_migration_backup) as backup:
        assert "content_digest" not in {
            row["name"] for row in backup.execute("PRAGMA table_info(revisions)")
        }
    with repository.connect() as connection:
        revision = connection.execute(
            "SELECT * FROM revisions WHERE id = ?", (revision_id,)
        ).fetchone()
        participant = connection.execute(
            """
            SELECT * FROM match_participants
            WHERE match_id = 'legacy-match' AND slot = 1
            """
        ).fetchone()
        assert revision["content_digest"] == content_digest(strategy_json)
        assert participant["bot_revision_id"] == revision_id
        assert participant["bot_revision_digest"] == revision["content_digest"]

    backup_files = list((tmp_path / "backups").glob("*.db"))
    repository.initialize(seed=False)
    assert list((tmp_path / "backups").glob("*.db")) == backup_files


def test_failed_digest_migration_rolls_back_schema_and_version(tmp_path: Path):
    database = tmp_path / "invalid.db"
    with _connect(database) as connection:
        apply_migrations(connection, target_version=1)
        now = "2026-01-01T00:00:00+00:00"
        connection.execute(
            """
            INSERT INTO bots (
                id, slug, name, race, created_at, updated_at
            ) VALUES ('bad-bot', 'bad-bot', 'Bad', 'terran', ?, ?)
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO revisions (
                id, bot_id, number, strategy_json, summary, created_at
            ) VALUES ('bad-revision', 'bad-bot', 1, '{', 'Bad JSON', ?)
            """,
            (now,),
        )
        connection.commit()

        with pytest.raises(MigrationError, match="invalid JSON"):
            apply_migrations(connection)

        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "content_digest" not in {
            row["name"] for row in connection.execute("PRAGMA table_info(revisions)")
        }


def test_failed_legacy_migration_keeps_valid_pre_migration_backup(
    tmp_path: Path,
):
    database = tmp_path / "invalid-legacy.db"
    with _connect(database) as connection:
        apply_migrations(connection, target_version=1)
        now = "2026-01-01T00:00:00+00:00"
        connection.execute(
            """
            INSERT INTO bots (
                id, slug, name, race, created_at, updated_at
            ) VALUES ('bad-bot', 'bad-bot', 'Bad', 'terran', ?, ?)
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO revisions (
                id, bot_id, number, strategy_json, summary, created_at
            ) VALUES ('bad-revision', 'bad-bot', 1, '{', 'Bad JSON', ?)
            """,
            (now,),
        )
        connection.execute("DROP TABLE schema_migrations")
        connection.execute("PRAGMA user_version = 0")

    repository = StudioRepository(database)
    with pytest.raises(MigrationError, match="invalid JSON"):
        repository.initialize(seed=False)

    assert repository.last_migration_backup is not None
    assert repository.last_migration_backup.exists()
    with _connect(repository.last_migration_backup) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute(
            "SELECT strategy_json FROM revisions WHERE id = 'bad-revision'"
        ).fetchone()[0] == "{"
        assert "content_digest" not in {
            row["name"] for row in backup.execute("PRAGMA table_info(revisions)")
        }
        assert backup.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'schema_migrations'
            """
        ).fetchone() is None

    # Migration 1 was committed as the last successful boundary. Migration 2
    # left neither its schema changes nor its ledger entry behind.
    with _connect(database) as source:
        assert [
            row["version"]
            for row in source.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == [1]
        assert source.execute("PRAGMA user_version").fetchone()[0] == 1
        assert source.execute(
            "SELECT strategy_json FROM revisions WHERE id = 'bad-revision'"
        ).fetchone()[0] == "{"
        assert "content_digest" not in {
            row["name"] for row in source.execute("PRAGMA table_info(revisions)")
        }


def test_dangling_legacy_reference_rolls_back_provenance_migration(
    tmp_path: Path,
):
    database = tmp_path / "dangling.db"
    with _connect(database) as connection:
        apply_migrations(connection, target_version=1)
        _insert_legacy_bot(connection)
        connection.execute(
            """
            INSERT INTO matches (
                id, source, map_name, status, created_at
            ) VALUES (
                'dangling-match', 'single', 'LegacyMap', 'completed',
                '2026-01-01T00:00:00+00:00'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO match_participants (
                match_id, slot, participant_type, bot_id, bot_revision,
                name, requested_race
            ) VALUES (
                'dangling-match', 1, 'bot', 'legacy-bot', 999,
                'Legacy', 'terran'
            )
            """
        )
        connection.commit()

        with pytest.raises(MigrationError, match="could not be backfilled safely"):
            apply_migrations(connection)

        assert [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == [1, 2]
        assert "bot_revision_id" not in {
            row["name"]
            for row in connection.execute("PRAGMA table_info(match_participants)")
        }


def test_canonical_digest_is_independent_of_json_key_order():
    first = {"settings": {"max_supply": 200}, "race": "terran"}
    second = {"race": "terran", "settings": {"max_supply": 200}}

    assert canonical_json(first) == canonical_json(second)
    assert content_digest(first) == content_digest(json.dumps(second, indent=2))


def test_revisions_are_append_only_and_builtin_sync_is_managed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = StudioRepository(tmp_path / "builtins.db")
    repository.initialize()
    original = repository.get_bot("terran-basic")
    with repository.connect() as connection:
        original_row = connection.execute(
            """
            SELECT id, strategy_json, content_digest, created_at
            FROM revisions WHERE bot_id = ? AND number = 1
            """,
            (original["id"],),
        ).fetchone()
    historical_match = repository.create_match(
        map_name="LegacyMap",
        source="single",
        participants=[
            {
                "participant_type": "bot",
                "bot_id": original["id"],
                "bot_revision": original["currentRevision"],
                "name": original["name"],
                "requested_race": original["race"],
            },
            {
                "participant_type": "computer",
                "bot_id": None,
                "bot_revision": None,
                "name": "Zerg Computer",
                "requested_race": "zerg",
                "difficulty": "easy",
            },
        ],
    )

    fixtures = json.loads(BUILTIN_FIXTURES.read_text())
    terran_fixture = next(item for item in fixtures if item["slug"] == "terran-basic")
    terran_fixture["strategy"]["opening_chat"] = "Managed fixture revision two."
    fixture_path = tmp_path / "builtins-v2.json"
    fixture_path.write_text(json.dumps(fixtures))
    monkeypatch.setattr(repository_module, "BUILTIN_FIXTURES", fixture_path)

    repository.seed_builtins()
    synchronized = repository.get_bot(original["id"])
    revisions = repository.list_revisions(original["id"])

    assert synchronized["currentRevision"] == 2
    assert len(revisions) == 2
    assert revisions[0]["content_digest"] == synchronized["currentRevisionDigest"]
    pinned_participant = repository.get_match(historical_match["id"])["participants"][0]
    assert pinned_participant["botRevisionId"] == original_row["id"]
    assert pinned_participant["botRevisionDigest"] == original_row["content_digest"]
    pinned_revision = repository.get_bot_revision(original["id"], 1)
    assert canonical_json(pinned_revision["strategy"]) == original_row["strategy_json"]
    with repository.connect() as connection:
        unchanged = connection.execute(
            """
            SELECT id, strategy_json, content_digest, created_at
            FROM revisions WHERE bot_id = ? AND number = 1
            """,
            (original["id"],),
        ).fetchone()
        assert dict(unchanged) == dict(original_row)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE revisions SET summary = 'mutated' WHERE id = ?",
                (original_row["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM revisions WHERE id = ?",
                (original_row["id"],),
            )

    repository.seed_builtins()
    assert repository.get_bot(original["id"])["currentRevision"] == 2

    user_version = repository.update_bot(
        original["id"],
        BotUpdate(
            description="User-owned customization",
            expected_revision=2,
            change_summary="User customization",
        ),
    )
    terran_fixture["strategy"]["opening_chat"] = "A later fixture revision."
    fixture_path.write_text(json.dumps(fixtures))
    repository.seed_builtins()
    preserved = repository.get_bot(original["id"])
    assert preserved["currentRevision"] == user_version["currentRevision"] == 3
    assert preserved["description"] == "User-owned customization"


def test_new_records_pin_revision_ids_and_digests(tmp_path: Path):
    repository = StudioRepository(tmp_path / "provenance.db")
    repository.initialize()
    bot = repository.get_bot("terran-basic")
    bot = repository.update_bot(
        bot["id"],
        BotUpdate(
            description="Candidate",
            expected_revision=1,
            change_summary="Candidate revision",
        ),
    )
    opponent = repository.get_bot("zerg-basic-1")

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
            },
            {
                "participant_type": "bot",
                "bot_id": opponent["id"],
                "bot_revision": opponent["currentRevision"],
                "name": opponent["name"],
                "requested_race": opponent["race"],
            },
        ],
    )
    assert match["participants"][0]["botRevisionId"] == bot["currentRevisionId"]
    assert (
        match["participants"][0]["botRevisionDigest"]
        == bot["currentRevisionDigest"]
    )
    with repository.connect() as connection:
        connection.execute(
            """
            UPDATE match_participants SET result = 'victory'
            WHERE match_id = ? AND slot = 1
            """,
            (match["id"],),
        )
        with pytest.raises(sqlite3.IntegrityError, match="inconsistent"):
            connection.execute(
                """
                UPDATE match_participants SET bot_revision_digest = ?
                WHERE match_id = ? AND slot = 1
                """,
                ("0" * 64, match["id"]),
            )

    suite = repository.create_benchmark_suite(
        BenchmarkSuiteCreate.model_validate(
            {
                "name": "Pinned",
                "scenarios": [
                    {
                        "name": "Opponent",
                        "map_name": "TestMap",
                        "opponent_type": "bot",
                        "opponent_bot_id": opponent["id"],
                        "opponent_revision": opponent["currentRevision"],
                    }
                ],
            }
        )
    )
    assert suite["scenarios"][0]["opponentRevisionId"] == (
        opponent["currentRevisionId"]
    )
    batch = repository.create_regression_batch(
        bot_id=bot["id"],
        baseline_revision=1,
        suite_id=suite["id"],
        games_per_scenario=1,
        concurrency=1,
    )
    assert batch["candidateRevisionId"] == bot["currentRevisionId"]
    assert batch["candidateRevisionDigest"] == bot["currentRevisionDigest"]
    assert all(game["testedRevisionId"] for game in batch["games"])
    assert all(game["testedRevisionDigest"] for game in batch["games"])
    assert all(game["opponentRevisionId"] for game in batch["games"])

    proposal = repository.create_proposal(
        StrategyProposal(
            summary="Pinned proposal",
            suggested_name="Pinned proposal",
            suggested_slug="pinned-proposal",
            strategy=blank_strategy(RaceName.TERRAN),
        ),
        UUID(bot["id"]),
    )
    assert proposal["baseRevision"] == bot["currentRevision"]
    assert proposal["baseRevisionId"] == bot["currentRevisionId"]
    assert proposal["baseRevisionDigest"] == bot["currentRevisionDigest"]
