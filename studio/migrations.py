from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


class MigrationError(RuntimeError):
    """Raised when a database cannot be migrated safely."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class MigrationReport:
    from_version: int
    to_version: int
    applied_versions: tuple[int, ...]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    """Serialize JSON data into the stable form used for content digests."""
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


BASE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS bots (
        id TEXT PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        race TEXT NOT NULL,
        tags_json TEXT NOT NULL DEFAULT '[]',
        is_builtin INTEGER NOT NULL DEFAULT 0,
        forked_from TEXT REFERENCES bots(id),
        deleted_at TEXT,
        current_revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS revisions (
        id TEXT PRIMARY KEY,
        bot_id TEXT NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
        number INTEGER NOT NULL,
        strategy_json TEXT NOT NULL,
        summary TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(bot_id, number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proposals (
        id TEXT PRIMARY KEY,
        base_bot_id TEXT REFERENCES bots(id),
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        applied_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS matches (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL DEFAULT 'single',
        map_name TEXT NOT NULL,
        status TEXT NOT NULL,
        game_time_seconds REAL,
        return_code INTEGER,
        failure_reason TEXT,
        regression_batch_id TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS match_participants (
        match_id TEXT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
        slot INTEGER NOT NULL,
        participant_type TEXT NOT NULL,
        bot_id TEXT REFERENCES bots(id),
        bot_revision INTEGER,
        name TEXT NOT NULL,
        requested_race TEXT NOT NULL,
        resolved_race TEXT,
        difficulty TEXT,
        result TEXT,
        PRIMARY KEY (match_id, slot)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_match_participants_bot
        ON match_participants(bot_id, match_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_matches_created
        ON matches(created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS benchmark_suites (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        archived_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS benchmark_scenarios (
        id TEXT PRIMARY KEY,
        suite_id TEXT NOT NULL REFERENCES benchmark_suites(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        name TEXT NOT NULL,
        map_name TEXT NOT NULL,
        opponent_type TEXT NOT NULL,
        enemy_race TEXT,
        difficulty TEXT,
        opponent_bot_id TEXT REFERENCES bots(id),
        opponent_revision INTEGER,
        UNIQUE(suite_id, position)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS regression_batches (
        id TEXT PRIMARY KEY,
        bot_id TEXT NOT NULL REFERENCES bots(id),
        candidate_revision INTEGER NOT NULL,
        baseline_revision INTEGER NOT NULL,
        suite_id TEXT REFERENCES benchmark_suites(id),
        suite_name TEXT NOT NULL,
        games_per_scenario INTEGER NOT NULL,
        concurrency INTEGER NOT NULL,
        status TEXT NOT NULL,
        total_games INTEGER NOT NULL,
        completed_games INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS regression_games (
        id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL REFERENCES regression_batches(id) ON DELETE CASCADE,
        scenario_position INTEGER NOT NULL,
        scenario_name TEXT NOT NULL,
        map_name TEXT NOT NULL,
        opponent_type TEXT NOT NULL,
        enemy_race TEXT,
        difficulty TEXT,
        opponent_bot_id TEXT REFERENCES bots(id),
        opponent_revision INTEGER,
        tested_role TEXT NOT NULL,
        tested_revision INTEGER NOT NULL,
        repetition INTEGER NOT NULL,
        random_seed INTEGER NOT NULL,
        match_id TEXT REFERENCES matches(id),
        status TEXT NOT NULL DEFAULT 'queued'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_regression_games_batch
        ON regression_games(batch_id, status, scenario_position, repetition)
    """,
)


def _migration_001_base_schema(connection: sqlite3.Connection) -> None:
    for statement in BASE_SCHEMA_STATEMENTS:
        connection.execute(statement)


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _add_column(
    connection: sqlite3.Connection,
    table: str,
    name: str,
    declaration: str,
) -> None:
    if name not in _column_names(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _migration_002_revision_integrity(connection: sqlite3.Connection) -> None:
    _add_column(connection, "revisions", "content_digest", "TEXT")
    _add_column(
        connection,
        "bots",
        "current_revision_id",
        "TEXT REFERENCES revisions(id)",
    )
    _add_column(connection, "bots", "current_revision_digest", "TEXT")
    _add_column(
        connection,
        "bots",
        "builtin_revision_id",
        "TEXT REFERENCES revisions(id)",
    )
    _add_column(connection, "bots", "builtin_revision_digest", "TEXT")

    # A retry after a manually interrupted migration should still be repairable.
    connection.execute("DROP TRIGGER IF EXISTS revisions_immutable_update")
    connection.execute("DROP TRIGGER IF EXISTS revisions_immutable_delete")
    connection.execute("DROP TRIGGER IF EXISTS revisions_require_digest")

    rows = connection.execute(
        "SELECT id, strategy_json, content_digest FROM revisions"
    ).fetchall()
    for row in rows:
        try:
            digest = content_digest(row[1])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MigrationError(
                f"Revision {row[0]} contains invalid JSON; migration was rolled back."
            ) from exc
        if row[2] != digest:
            connection.execute(
                "UPDATE revisions SET content_digest = ? WHERE id = ?",
                (digest, row[0]),
            )

    connection.execute(
        """
        UPDATE bots
        SET current_revision_id = (
                SELECT revisions.id
                FROM revisions
                WHERE revisions.bot_id = bots.id
                  AND revisions.number = bots.current_revision
            ),
            current_revision_digest = (
                SELECT revisions.content_digest
                FROM revisions
                WHERE revisions.bot_id = bots.id
                  AND revisions.number = bots.current_revision
            )
        """
    )
    # Only revision-1 built-ins were considered pristine by the legacy seeder.
    # Later revisions may be user-authored and must never be adopted as managed.
    connection.execute(
        """
        UPDATE bots
        SET builtin_revision_id = current_revision_id,
            builtin_revision_digest = current_revision_digest
        WHERE is_builtin = 1 AND current_revision = 1
        """
    )
    unresolved_current = connection.execute(
        """
        SELECT COUNT(*)
        FROM bots
        WHERE current_revision_id IS NULL
           OR current_revision_digest IS NULL
           OR NOT EXISTS (
               SELECT 1 FROM revisions
               WHERE revisions.id = bots.current_revision_id
                 AND revisions.bot_id = bots.id
                 AND revisions.number = bots.current_revision
                 AND revisions.content_digest = bots.current_revision_digest
           )
        """
    ).fetchone()[0]
    if unresolved_current:
        raise MigrationError(
            f"{unresolved_current} bot current revision reference(s) could not "
            "be backfilled safely."
        )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_revisions_content_digest
        ON revisions(content_digest)
        """
    )
    connection.execute(
        """
        CREATE TRIGGER revisions_require_digest
        BEFORE INSERT ON revisions
        WHEN NEW.content_digest IS NULL OR length(NEW.content_digest) != 64
        BEGIN
            SELECT RAISE(ABORT, 'revision content_digest is required');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER revisions_immutable_update
        BEFORE UPDATE ON revisions
        BEGIN
            SELECT RAISE(ABORT, 'revisions are immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER revisions_immutable_delete
        BEFORE DELETE ON revisions
        BEGIN
            SELECT RAISE(ABORT, 'revisions are append-only');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS bots_current_revision_consistent
        BEFORE UPDATE OF current_revision, current_revision_id,
            current_revision_digest ON bots
        WHEN NEW.current_revision_id IS NULL
          OR NEW.current_revision_digest IS NULL
          OR NOT EXISTS (
              SELECT 1 FROM revisions
              WHERE revisions.id = NEW.current_revision_id
                AND revisions.bot_id = NEW.id
                AND revisions.number = NEW.current_revision
                AND revisions.content_digest = NEW.current_revision_digest
          )
        BEGIN
            SELECT RAISE(ABORT, 'bot current revision provenance is inconsistent');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS bots_builtin_revision_consistent
        BEFORE UPDATE OF builtin_revision_id, builtin_revision_digest ON bots
        WHEN (NEW.builtin_revision_id IS NULL) !=
                (NEW.builtin_revision_digest IS NULL)
          OR (
              NEW.builtin_revision_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM revisions
                  WHERE revisions.id = NEW.builtin_revision_id
                    AND revisions.bot_id = NEW.id
                    AND revisions.content_digest = NEW.builtin_revision_digest
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'bot built-in revision provenance is inconsistent');
        END
        """
    )


def _backfill_revision_reference(
    connection: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    digest_column: str,
    revision_lookup: str,
    where: str,
) -> None:
    connection.execute(
        f"""
        UPDATE {table}
        SET {id_column} = (
                SELECT revisions.id
                FROM revisions
                WHERE {revision_lookup}
            ),
            {digest_column} = (
                SELECT revisions.content_digest
                FROM revisions
                WHERE {revision_lookup}
            )
        WHERE {where}
        """
    )


def _migration_003_revision_provenance(connection: sqlite3.Connection) -> None:
    reference_columns = {
        "match_participants": (
            ("bot_revision_id", "TEXT REFERENCES revisions(id)"),
            ("bot_revision_digest", "TEXT"),
        ),
        "benchmark_scenarios": (
            ("opponent_revision_id", "TEXT REFERENCES revisions(id)"),
            ("opponent_revision_digest", "TEXT"),
        ),
        "regression_batches": (
            ("candidate_revision_id", "TEXT REFERENCES revisions(id)"),
            ("candidate_revision_digest", "TEXT"),
            ("baseline_revision_id", "TEXT REFERENCES revisions(id)"),
            ("baseline_revision_digest", "TEXT"),
        ),
        "regression_games": (
            ("opponent_revision_id", "TEXT REFERENCES revisions(id)"),
            ("opponent_revision_digest", "TEXT"),
            ("tested_revision_id", "TEXT REFERENCES revisions(id)"),
            ("tested_revision_digest", "TEXT"),
        ),
        "proposals": (
            ("base_revision", "INTEGER"),
            ("base_revision_id", "TEXT REFERENCES revisions(id)"),
            ("base_revision_digest", "TEXT"),
        ),
    }
    for table, columns in reference_columns.items():
        for name, declaration in columns:
            _add_column(connection, table, name, declaration)

    _backfill_revision_reference(
        connection,
        table="match_participants",
        id_column="bot_revision_id",
        digest_column="bot_revision_digest",
        revision_lookup=(
            "revisions.bot_id = match_participants.bot_id "
            "AND revisions.number = match_participants.bot_revision"
        ),
        where="bot_id IS NOT NULL AND bot_revision IS NOT NULL",
    )
    _backfill_revision_reference(
        connection,
        table="benchmark_scenarios",
        id_column="opponent_revision_id",
        digest_column="opponent_revision_digest",
        revision_lookup=(
            "revisions.bot_id = benchmark_scenarios.opponent_bot_id "
            "AND revisions.number = benchmark_scenarios.opponent_revision"
        ),
        where="opponent_bot_id IS NOT NULL AND opponent_revision IS NOT NULL",
    )
    _backfill_revision_reference(
        connection,
        table="regression_batches",
        id_column="candidate_revision_id",
        digest_column="candidate_revision_digest",
        revision_lookup=(
            "revisions.bot_id = regression_batches.bot_id "
            "AND revisions.number = regression_batches.candidate_revision"
        ),
        where="1 = 1",
    )
    _backfill_revision_reference(
        connection,
        table="regression_batches",
        id_column="baseline_revision_id",
        digest_column="baseline_revision_digest",
        revision_lookup=(
            "revisions.bot_id = regression_batches.bot_id "
            "AND revisions.number = regression_batches.baseline_revision"
        ),
        where="1 = 1",
    )
    _backfill_revision_reference(
        connection,
        table="regression_games",
        id_column="opponent_revision_id",
        digest_column="opponent_revision_digest",
        revision_lookup=(
            "revisions.bot_id = regression_games.opponent_bot_id "
            "AND revisions.number = regression_games.opponent_revision"
        ),
        where="opponent_bot_id IS NOT NULL AND opponent_revision IS NOT NULL",
    )
    connection.execute(
        """
        UPDATE regression_games
        SET tested_revision_id = (
                SELECT revisions.id
                FROM revisions
                JOIN regression_batches
                  ON regression_batches.id = regression_games.batch_id
                WHERE revisions.bot_id = regression_batches.bot_id
                  AND revisions.number = regression_games.tested_revision
            ),
            tested_revision_digest = (
                SELECT revisions.content_digest
                FROM revisions
                JOIN regression_batches
                  ON regression_batches.id = regression_games.batch_id
                WHERE revisions.bot_id = regression_batches.bot_id
                  AND revisions.number = regression_games.tested_revision
            )
        """
    )
    _validate_backfilled_provenance(connection)

    for statement in (
        """
        CREATE INDEX IF NOT EXISTS idx_match_participants_revision
        ON match_participants(bot_revision_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_benchmark_scenarios_revision
        ON benchmark_scenarios(opponent_revision_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_regression_batches_revisions
        ON regression_batches(candidate_revision_id, baseline_revision_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_regression_games_revisions
        ON regression_games(tested_revision_id, opponent_revision_id)
        """,
    ):
        connection.execute(statement)

    _create_provenance_triggers(connection)


def _validate_backfilled_provenance(connection: sqlite3.Connection) -> None:
    checks = (
        (
            "match participant",
            """
            SELECT COUNT(*) FROM match_participants
            WHERE participant_type = 'bot'
              AND (
                  bot_id IS NULL
                  OR bot_revision IS NULL
                  OR bot_revision_id IS NULL
                  OR bot_revision_digest IS NULL
                  OR NOT EXISTS (
                      SELECT 1 FROM revisions
                      WHERE revisions.id = match_participants.bot_revision_id
                        AND revisions.bot_id = match_participants.bot_id
                        AND revisions.number = match_participants.bot_revision
                        AND revisions.content_digest =
                            match_participants.bot_revision_digest
                  )
              )
            """,
        ),
        (
            "benchmark opponent",
            """
            SELECT COUNT(*) FROM benchmark_scenarios
            WHERE opponent_type = 'bot'
              AND (
                  opponent_bot_id IS NULL
                  OR opponent_revision IS NULL
                  OR opponent_revision_id IS NULL
                  OR opponent_revision_digest IS NULL
                  OR NOT EXISTS (
                      SELECT 1 FROM revisions
                      WHERE revisions.id =
                            benchmark_scenarios.opponent_revision_id
                        AND revisions.bot_id =
                            benchmark_scenarios.opponent_bot_id
                        AND revisions.number =
                            benchmark_scenarios.opponent_revision
                        AND revisions.content_digest =
                            benchmark_scenarios.opponent_revision_digest
                  )
              )
            """,
        ),
        (
            "regression batch",
            """
            SELECT COUNT(*) FROM regression_batches
            WHERE NOT EXISTS (
                    SELECT 1 FROM revisions
                    WHERE revisions.id =
                            regression_batches.candidate_revision_id
                      AND revisions.bot_id = regression_batches.bot_id
                      AND revisions.number =
                            regression_batches.candidate_revision
                      AND revisions.content_digest =
                            regression_batches.candidate_revision_digest
                )
               OR NOT EXISTS (
                    SELECT 1 FROM revisions
                    WHERE revisions.id =
                            regression_batches.baseline_revision_id
                      AND revisions.bot_id = regression_batches.bot_id
                      AND revisions.number =
                            regression_batches.baseline_revision
                      AND revisions.content_digest =
                            regression_batches.baseline_revision_digest
                )
            """,
        ),
        (
            "regression game",
            """
            SELECT COUNT(*)
            FROM regression_games
            JOIN regression_batches
              ON regression_batches.id = regression_games.batch_id
            WHERE tested_revision_id IS NULL
               OR tested_revision_digest IS NULL
               OR NOT EXISTS (
                    SELECT 1 FROM revisions
                    WHERE revisions.id = regression_games.tested_revision_id
                      AND revisions.bot_id = regression_batches.bot_id
                      AND revisions.number =
                            regression_games.tested_revision
                      AND revisions.content_digest =
                            regression_games.tested_revision_digest
               )
               OR (
                    opponent_type = 'bot'
                    AND (
                        opponent_bot_id IS NULL
                        OR opponent_revision IS NULL
                        OR opponent_revision_id IS NULL
                        OR opponent_revision_digest IS NULL
                        OR NOT EXISTS (
                            SELECT 1 FROM revisions
                            WHERE revisions.id =
                                    regression_games.opponent_revision_id
                              AND revisions.bot_id =
                                    regression_games.opponent_bot_id
                              AND revisions.number =
                                    regression_games.opponent_revision
                              AND revisions.content_digest =
                                    regression_games.opponent_revision_digest
                        )
                    )
               )
            """,
        ),
    )
    for label, statement in checks:
        unresolved = int(connection.execute(statement).fetchone()[0])
        if unresolved:
            raise MigrationError(
                f"{unresolved} {label} revision reference(s) could not be "
                "backfilled safely."
            )


def _create_provenance_triggers(connection: sqlite3.Connection) -> None:
    triggers = (
        """
        CREATE TRIGGER IF NOT EXISTS match_participant_revision_consistent
        BEFORE INSERT ON match_participants
        WHEN NEW.participant_type = 'bot'
          AND (
              NEW.bot_id IS NULL
              OR NEW.bot_revision IS NULL
              OR NEW.bot_revision_id IS NULL
              OR NEW.bot_revision_digest IS NULL
              OR NOT EXISTS (
                  SELECT 1 FROM revisions
                  WHERE revisions.id = NEW.bot_revision_id
                    AND revisions.bot_id = NEW.bot_id
                    AND revisions.number = NEW.bot_revision
                    AND revisions.content_digest = NEW.bot_revision_digest
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'match participant revision provenance is inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS match_participant_revision_update_consistent
        BEFORE UPDATE OF participant_type, bot_id, bot_revision,
            bot_revision_id, bot_revision_digest ON match_participants
        WHEN NEW.participant_type = 'bot'
          AND (
              NEW.bot_id IS NULL
              OR NEW.bot_revision IS NULL
              OR NEW.bot_revision_id IS NULL
              OR NEW.bot_revision_digest IS NULL
              OR NOT EXISTS (
                  SELECT 1 FROM revisions
                  WHERE revisions.id = NEW.bot_revision_id
                    AND revisions.bot_id = NEW.bot_id
                    AND revisions.number = NEW.bot_revision
                    AND revisions.content_digest = NEW.bot_revision_digest
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'match participant revision provenance is inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS benchmark_opponent_revision_consistent
        BEFORE INSERT ON benchmark_scenarios
        WHEN NEW.opponent_type = 'bot'
          AND (
              NEW.opponent_bot_id IS NULL
              OR NEW.opponent_revision IS NULL
              OR NEW.opponent_revision_id IS NULL
              OR NEW.opponent_revision_digest IS NULL
              OR NOT EXISTS (
                  SELECT 1 FROM revisions
                  WHERE revisions.id = NEW.opponent_revision_id
                    AND revisions.bot_id = NEW.opponent_bot_id
                    AND revisions.number = NEW.opponent_revision
                    AND revisions.content_digest = NEW.opponent_revision_digest
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'benchmark opponent revision provenance is inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS benchmark_opponent_revision_update_consistent
        BEFORE UPDATE OF opponent_type, opponent_bot_id, opponent_revision,
            opponent_revision_id, opponent_revision_digest
        ON benchmark_scenarios
        WHEN NEW.opponent_type = 'bot'
          AND (
              NEW.opponent_bot_id IS NULL
              OR NEW.opponent_revision IS NULL
              OR NEW.opponent_revision_id IS NULL
              OR NEW.opponent_revision_digest IS NULL
              OR NOT EXISTS (
                  SELECT 1 FROM revisions
                  WHERE revisions.id = NEW.opponent_revision_id
                    AND revisions.bot_id = NEW.opponent_bot_id
                    AND revisions.number = NEW.opponent_revision
                    AND revisions.content_digest = NEW.opponent_revision_digest
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'benchmark opponent revision provenance is inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS regression_batch_revisions_consistent
        BEFORE INSERT ON regression_batches
        WHEN NOT EXISTS (
                SELECT 1 FROM revisions
                WHERE revisions.id = NEW.candidate_revision_id
                  AND revisions.bot_id = NEW.bot_id
                  AND revisions.number = NEW.candidate_revision
                  AND revisions.content_digest = NEW.candidate_revision_digest
            )
          OR NOT EXISTS (
                SELECT 1 FROM revisions
                WHERE revisions.id = NEW.baseline_revision_id
                  AND revisions.bot_id = NEW.bot_id
                  AND revisions.number = NEW.baseline_revision
                  AND revisions.content_digest = NEW.baseline_revision_digest
            )
        BEGIN
            SELECT RAISE(ABORT, 'regression batch revision provenance is inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS regression_batch_revisions_update_consistent
        BEFORE UPDATE OF bot_id, candidate_revision, candidate_revision_id,
            candidate_revision_digest, baseline_revision, baseline_revision_id,
            baseline_revision_digest ON regression_batches
        WHEN NOT EXISTS (
                SELECT 1 FROM revisions
                WHERE revisions.id = NEW.candidate_revision_id
                  AND revisions.bot_id = NEW.bot_id
                  AND revisions.number = NEW.candidate_revision
                  AND revisions.content_digest = NEW.candidate_revision_digest
            )
          OR NOT EXISTS (
                SELECT 1 FROM revisions
                WHERE revisions.id = NEW.baseline_revision_id
                  AND revisions.bot_id = NEW.bot_id
                  AND revisions.number = NEW.baseline_revision
                  AND revisions.content_digest = NEW.baseline_revision_digest
            )
        BEGIN
            SELECT RAISE(ABORT, 'regression batch revision provenance is inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS regression_game_revision_consistent
        BEFORE INSERT ON regression_games
        WHEN NEW.tested_revision_id IS NULL
          OR NEW.tested_revision_digest IS NULL
          OR NOT EXISTS (
              SELECT 1
              FROM revisions
              JOIN regression_batches
                ON regression_batches.id = NEW.batch_id
              WHERE revisions.id = NEW.tested_revision_id
                AND revisions.bot_id = regression_batches.bot_id
                AND revisions.number = NEW.tested_revision
                AND revisions.content_digest = NEW.tested_revision_digest
          )
          OR (
              NEW.opponent_type = 'bot'
              AND (
                  NEW.opponent_revision_id IS NULL
                  OR NEW.opponent_revision_digest IS NULL
                  OR NOT EXISTS (
                      SELECT 1 FROM revisions
                      WHERE revisions.id = NEW.opponent_revision_id
                        AND revisions.bot_id = NEW.opponent_bot_id
                        AND revisions.number = NEW.opponent_revision
                        AND revisions.content_digest = NEW.opponent_revision_digest
                  )
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'regression game revision provenance is inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS regression_game_revision_update_consistent
        BEFORE UPDATE OF batch_id, opponent_type, opponent_bot_id,
            opponent_revision, opponent_revision_id, opponent_revision_digest,
            tested_revision, tested_revision_id, tested_revision_digest
        ON regression_games
        WHEN NEW.tested_revision_id IS NULL
          OR NEW.tested_revision_digest IS NULL
          OR NOT EXISTS (
              SELECT 1
              FROM revisions
              JOIN regression_batches
                ON regression_batches.id = NEW.batch_id
              WHERE revisions.id = NEW.tested_revision_id
                AND revisions.bot_id = regression_batches.bot_id
                AND revisions.number = NEW.tested_revision
                AND revisions.content_digest = NEW.tested_revision_digest
          )
          OR (
              NEW.opponent_type = 'bot'
              AND (
                  NEW.opponent_revision_id IS NULL
                  OR NEW.opponent_revision_digest IS NULL
                  OR NOT EXISTS (
                      SELECT 1 FROM revisions
                      WHERE revisions.id = NEW.opponent_revision_id
                        AND revisions.bot_id = NEW.opponent_bot_id
                        AND revisions.number = NEW.opponent_revision
                        AND revisions.content_digest = NEW.opponent_revision_digest
                  )
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'regression game revision provenance is inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS proposal_base_revision_consistent
        BEFORE INSERT ON proposals
        WHEN NEW.base_bot_id IS NOT NULL
          AND (
              NEW.base_revision IS NULL
              OR NEW.base_revision_id IS NULL
              OR NEW.base_revision_digest IS NULL
              OR NOT EXISTS (
                  SELECT 1 FROM revisions
                  WHERE revisions.id = NEW.base_revision_id
                    AND revisions.bot_id = NEW.base_bot_id
                    AND revisions.number = NEW.base_revision
                    AND revisions.content_digest = NEW.base_revision_digest
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'proposal base revision provenance is inconsistent');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS proposal_base_revision_update_consistent
        BEFORE UPDATE OF base_bot_id, base_revision, base_revision_id,
            base_revision_digest ON proposals
        WHEN NEW.base_bot_id IS NOT NULL
          AND (
              NEW.base_revision IS NULL
              OR NEW.base_revision_id IS NULL
              OR NEW.base_revision_digest IS NULL
              OR NOT EXISTS (
                  SELECT 1 FROM revisions
                  WHERE revisions.id = NEW.base_revision_id
                    AND revisions.bot_id = NEW.base_bot_id
                    AND revisions.number = NEW.base_revision
                    AND revisions.content_digest = NEW.base_revision_digest
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'proposal base revision provenance is inconsistent');
        END
        """,
    )
    for statement in triggers:
        connection.execute(statement)


def _migration_004_durable_match_logs(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS match_logs (
            match_id TEXT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            line TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (match_id, sequence)
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_match_logs_match_created
        ON match_logs(match_id, created_at)
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS match_logs_immutable_update
        BEFORE UPDATE ON match_logs
        BEGIN
            SELECT RAISE(ABORT, 'match logs are append-only');
        END
        """
    )


MIGRATIONS = (
    Migration(1, "base schema", _migration_001_base_schema),
    Migration(2, "immutable revision digests", _migration_002_revision_integrity),
    Migration(3, "durable revision references", _migration_003_revision_provenance),
    Migration(4, "durable bounded match logs", _migration_004_durable_match_logs),
)
LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table,),
        ).fetchone()
        is not None
    )


def applied_migration_versions(connection: sqlite3.Connection) -> tuple[int, ...]:
    if not _table_exists(connection, "schema_migrations"):
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version:
            raise MigrationError(
                "Database has PRAGMA user_version but no migration ledger."
            )
        return ()
    return tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    )


def current_schema_version(connection: sqlite3.Connection) -> int:
    versions = applied_migration_versions(connection)
    return versions[-1] if versions else 0


def pending_migrations(connection: sqlite3.Connection) -> tuple[Migration, ...]:
    applied = applied_migration_versions(connection)
    expected_prefix = tuple(migration.version for migration in MIGRATIONS[: len(applied)])
    if applied != expected_prefix:
        raise MigrationError(
            f"Migration ledger is not an ordered prefix: found {applied}."
        )
    if applied and applied[-1] > LATEST_SCHEMA_VERSION:
        raise MigrationError(
            f"Database schema v{applied[-1]} is newer than supported "
            f"v{LATEST_SCHEMA_VERSION}."
        )
    return MIGRATIONS[len(applied) :]


def has_legacy_application_schema(connection: sqlite3.Connection) -> bool:
    return any(
        _table_exists(connection, table)
        for table in ("bots", "revisions", "matches", "regression_batches")
    )


def create_migration_backup(
    connection: sqlite3.Connection,
    database_path: Path,
    *,
    from_version: int,
    to_version: int,
) -> Path:
    backup_dir = database_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / (
        f"{database_path.stem}.schema-v{from_version}-to-v{to_version}.{timestamp}.db"
    )
    temporary = backup_dir / f".{destination.name}.{uuid4().hex}.tmp"
    backup_connection = sqlite3.connect(temporary)
    try:
        connection.backup(backup_connection)
        backup_connection.execute("PRAGMA foreign_keys = ON")
        integrity = backup_connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise MigrationError("Database backup failed its integrity check.")
        backup_connection.close()
        os.replace(temporary, destination)
    except Exception:
        backup_connection.close()
        temporary.unlink(missing_ok=True)
        raise
    return destination


def apply_migrations(
    connection: sqlite3.Connection,
    *,
    target_version: int | None = None,
) -> MigrationReport:
    if connection.in_transaction:
        raise MigrationError("Migrations require a connection with no active transaction.")

    target = LATEST_SCHEMA_VERSION if target_version is None else target_version
    if target < 0 or target > LATEST_SCHEMA_VERSION:
        raise MigrationError(
            f"Unsupported migration target v{target}; latest is "
            f"v{LATEST_SCHEMA_VERSION}."
        )

    before: int | None = None
    after: int | None = None
    applied: list[int] = []
    while True:
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Inspect and validate the ledger only after acquiring SQLite's
            # writer lock. Another process may have migrated the same database
            # while this caller was waiting for the lock.
            if not _table_exists(connection, "schema_migrations"):
                user_version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                if user_version:
                    raise MigrationError(
                        "Database has PRAGMA user_version but no migration ledger."
                    )
                connection.execute(
                    """
                    CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    )
                    """
                )

            migrations = pending_migrations(connection)
            current = current_schema_version(connection)
            if before is None:
                before = current
            if target < current:
                raise MigrationError(
                    f"Unsupported migration target v{target} from v{current}."
                )

            migration = next(
                (
                    candidate
                    for candidate in migrations
                    if candidate.version <= target
                ),
                None,
            )
            if migration is None:
                pragma_version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                if pragma_version != current:
                    connection.execute(f"PRAGMA user_version = {current}")
                connection.commit()
                after = current
                break

            migration.apply(connection)
            connection.execute(
                """
                INSERT INTO schema_migrations (version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (migration.version, migration.name, _utc_now()),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        applied.append(migration.version)

    assert before is not None
    assert after is not None
    return MigrationReport(
        from_version=before,
        to_version=after,
        applied_versions=tuple(applied),
    )
