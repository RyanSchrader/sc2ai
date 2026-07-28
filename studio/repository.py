from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv

from .migrations import (
    LATEST_SCHEMA_VERSION,
    MigrationReport,
    apply_migrations,
    canonical_json,
    content_digest,
    create_migration_backup,
    current_schema_version,
    has_legacy_application_schema,
    pending_migrations,
)
from .models import (
    BenchmarkSuiteCreate,
    BotCreate,
    BotUpdate,
    StrategyDocument,
    StrategyProposal,
    utc_now,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
BUILTIN_FIXTURES = PROJECT_ROOT / "strategies" / "builtin_bots.json"
load_dotenv(PROJECT_ROOT / ".env")


class NotFoundError(KeyError):
    pass


class ConflictError(ValueError):
    pass


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "untitled-bot"


class StudioRepository:
    def __init__(self, database_path: str | Path | None = None):
        configured = os.getenv("SC2_STUDIO_DB")
        selected = Path(database_path or configured or DEFAULT_DATA_DIR / "studio.db")
        self.database_path = selected if selected.is_absolute() else PROJECT_ROOT / selected
        self.last_migration_backup: Path | None = None
        self.last_migration_report: MigrationReport | None = None

    def connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self, seed: bool = True) -> None:
        with self.connect() as connection:
            migrations = pending_migrations(connection)
            if migrations and has_legacy_application_schema(connection):
                self.last_migration_backup = create_migration_backup(
                    connection,
                    self.database_path,
                    from_version=current_schema_version(connection),
                    to_version=LATEST_SCHEMA_VERSION,
                )
            self.last_migration_report = apply_migrations(connection)
            connection.execute(
                """
                UPDATE regression_batches
                SET completed_games = (
                    SELECT COUNT(*) FROM regression_games
                    WHERE regression_games.batch_id = regression_batches.id
                      AND regression_games.status IN ('completed', 'failed', 'stopped')
                )
                """
            )
        if seed:
            self.seed_builtins()

    def seed_builtins(self) -> None:
        fixtures = json.loads(BUILTIN_FIXTURES.read_text())
        for fixture in fixtures:
            strategy = StrategyDocument.model_validate(fixture["strategy"])
            with self.connect() as connection:
                exists = connection.execute(
                    """
                    SELECT id, is_builtin, current_revision,
                        current_revision_id, builtin_revision_id
                    FROM bots WHERE slug = ?
                    """,
                    (fixture["slug"],),
                ).fetchone()
            if exists:
                if exists["is_builtin"]:
                    self._synchronize_builtin(exists["id"], fixture, strategy)
                continue
            self.create_bot(
                BotCreate(
                    name=fixture["name"],
                    slug=fixture["slug"],
                    description=fixture["description"],
                    race=fixture["race"],
                    tags=fixture["tags"],
                    strategy=strategy,
                ),
                is_builtin=True,
                summary="Imported built-in strategy",
            )

    def _synchronize_builtin(
        self,
        bot_id: str,
        fixture: dict[str, Any],
        strategy: StrategyDocument,
    ) -> None:
        """Append fixture changes while leaving user-modified built-ins alone."""
        serialized = canonical_json(strategy.model_dump(mode="json"))
        digest = content_digest(serialized)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            bot = connection.execute(
                """
                SELECT bots.*, revisions.id AS resolved_revision_id,
                    revisions.content_digest AS resolved_revision_digest
                FROM bots
                LEFT JOIN revisions
                  ON revisions.bot_id = bots.id
                 AND revisions.number = bots.current_revision
                WHERE bots.id = ?
                """,
                (bot_id,),
            ).fetchone()
            if bot is None or not bot["is_builtin"]:
                return
            # The managed pointer advances only when the seeder appends a
            # revision. Any regular edit changes current_revision_id without
            # changing builtin_revision_id and permanently opts out of sync.
            if (
                bot["resolved_revision_id"] is None
                or bot["current_revision_id"] != bot["resolved_revision_id"]
                or bot["builtin_revision_id"] != bot["current_revision_id"]
            ):
                return
            desired_tags = list(fixture["tags"])
            metadata_changed = (
                bot["name"] != fixture["name"]
                or bot["description"] != fixture["description"]
                or bot["race"] != fixture["race"]
                or json.loads(bot["tags_json"]) != desired_tags
            )
            if bot["resolved_revision_digest"] == digest:
                if not metadata_changed:
                    return
                connection.execute(
                    """
                    UPDATE bots
                    SET name = ?, description = ?, race = ?, tags_json = ?,
                        updated_at = ?
                    WHERE id = ? AND current_revision_id = ?
                        AND builtin_revision_id = ?
                    """,
                    (
                        fixture["name"],
                        fixture["description"],
                        fixture["race"],
                        json.dumps(desired_tags),
                        utc_now(),
                        bot_id,
                        bot["current_revision_id"],
                        bot["builtin_revision_id"],
                    ),
                )
                return

            now = utc_now()
            next_revision = int(bot["current_revision"]) + 1
            revision = self._insert_revision(
                connection,
                bot_id,
                next_revision,
                strategy,
                "Synchronized built-in fixture",
                now,
            )
            updated = connection.execute(
                """
                UPDATE bots
                SET name = ?, description = ?, race = ?, tags_json = ?,
                    current_revision = ?, current_revision_id = ?,
                    current_revision_digest = ?, builtin_revision_id = ?,
                    builtin_revision_digest = ?, updated_at = ?
                WHERE id = ? AND current_revision_id = ?
                    AND builtin_revision_id = ?
                """,
                (
                    fixture["name"],
                    fixture["description"],
                    fixture["race"],
                    json.dumps(desired_tags),
                    next_revision,
                    revision["id"],
                    revision["content_digest"],
                    revision["id"],
                    revision["content_digest"],
                    now,
                    bot_id,
                    bot["current_revision_id"],
                    bot["builtin_revision_id"],
                ),
            )
            if updated.rowcount != 1:
                raise ConflictError(
                    "Built-in changed while its fixture revision was being saved."
                )

    def unique_slug(self, desired: str, exclude_id: str | None = None) -> str:
        base = slugify(desired)
        candidate = base
        suffix = 2
        with self.connect() as connection:
            while True:
                row = connection.execute(
                    "SELECT id FROM bots WHERE slug = ?", (candidate,)
                ).fetchone()
                if row is None or row["id"] == exclude_id:
                    return candidate
                candidate = f"{base}-{suffix}"
                suffix += 1

    def create_bot(
        self,
        bot: BotCreate,
        *,
        is_builtin: bool = False,
        forked_from: str | None = None,
        summary: str = "Created bot",
    ) -> dict[str, Any]:
        if bot.strategy.race != bot.race:
            raise ConflictError("Bot race and strategy race must match.")
        bot_id = str(uuid4())
        slug = self.unique_slug(bot.slug or bot.name)
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO bots (
                    id, slug, name, description, race, tags_json, is_builtin,
                    forked_from, current_revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    bot_id,
                    slug,
                    bot.name,
                    bot.description,
                    bot.race.value,
                    json.dumps(bot.tags),
                    int(is_builtin),
                    forked_from,
                    now,
                    now,
                ),
            )
            revision = self._insert_revision(
                connection,
                bot_id,
                1,
                bot.strategy,
                summary,
                now,
            )
            connection.execute(
                """
                UPDATE bots
                SET current_revision_id = ?, current_revision_digest = ?,
                    builtin_revision_id = ?, builtin_revision_digest = ?
                WHERE id = ?
                """,
                (
                    revision["id"],
                    revision["content_digest"],
                    revision["id"] if is_builtin else None,
                    revision["content_digest"] if is_builtin else None,
                    bot_id,
                ),
            )
        return self.get_bot(bot_id)

    def _insert_revision(
        self,
        connection: sqlite3.Connection,
        bot_id: str,
        number: int,
        strategy: StrategyDocument,
        summary: str,
        created_at: str | None = None,
    ) -> dict[str, str]:
        revision_id = str(uuid4())
        serialized = canonical_json(strategy.model_dump(mode="json"))
        digest = content_digest(serialized)
        timestamp = created_at or utc_now()
        connection.execute(
            """
            INSERT INTO revisions (
                id, bot_id, number, strategy_json, content_digest, summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                bot_id,
                number,
                serialized,
                digest,
                summary,
                timestamp,
            ),
        )
        return {
            "id": revision_id,
            "content_digest": digest,
            "created_at": timestamp,
        }

    def _revision_identity(
        self,
        connection: sqlite3.Connection,
        bot_id: str,
        revision_number: int,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT id, bot_id, number, strategy_json, content_digest, summary,
                created_at
            FROM revisions
            WHERE bot_id = ? AND number = ?
            """,
            (bot_id, revision_number),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"Revision {revision_number} not found for bot {bot_id}."
            )
        actual_digest = content_digest(row["strategy_json"])
        if row["content_digest"] != actual_digest:
            raise ConflictError(
                f"Revision {row['id']} failed its content integrity check."
            )
        return {
            "id": row["id"],
            "bot_id": row["bot_id"],
            "number": row["number"],
            "strategy_json": row["strategy_json"],
            "content_digest": row["content_digest"],
            "summary": row["summary"],
            "created_at": row["created_at"],
        }

    def list_bots(
        self,
        *,
        include_deleted: bool = False,
        search: str | None = None,
        race: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        if search:
            clauses.append("(LOWER(name) LIKE ? OR LOWER(description) LIKE ? OR LOWER(slug) LIKE ?)")
            pattern = f"%{search.lower()}%"
            parameters.extend([pattern, pattern, pattern])
        if race:
            clauses.append("race = ?")
            parameters.append(race)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM bots {where} ORDER BY is_builtin DESC, updated_at DESC",
                parameters,
            ).fetchall()
        results = [self._bot_row(row, include_strategy=False) for row in rows]
        for result in results:
            stats = self.bot_stats(result["id"])
            result["stats"] = {
                "wins": stats["wins"],
                "losses": stats["losses"],
                "ties": stats["ties"],
                "winRate": stats["winRate"],
                "totalRuns": stats["totalRuns"],
            }
        return results

    def get_bot(self, bot_id_or_slug: str, *, include_deleted: bool = True) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM bots WHERE id = ? OR slug = ?",
                (bot_id_or_slug, bot_id_or_slug),
            ).fetchone()
            if row is None or (not include_deleted and row["deleted_at"]):
                raise NotFoundError(f"Bot not found: {bot_id_or_slug}")
            revision = self._revision_identity(
                connection,
                row["id"],
                row["current_revision"],
            )
            if (
                row["current_revision_id"] != revision["id"]
                or row["current_revision_digest"] != revision["content_digest"]
            ):
                raise ConflictError(
                    f"Bot {row['id']} has inconsistent current revision provenance."
                )
        result = self._bot_row(row, include_strategy=False)
        result["strategy"] = StrategyDocument.model_validate_json(
            revision["strategy_json"]
        ).model_dump(mode="json")
        result["revisionSummary"] = revision["summary"]
        return result

    def _bot_row(self, row: sqlite3.Row, *, include_strategy: bool) -> dict[str, Any]:
        return {
            "id": row["id"],
            "slug": row["slug"],
            "name": row["name"],
            "description": row["description"],
            "race": row["race"],
            "tags": json.loads(row["tags_json"]),
            "isBuiltin": bool(row["is_builtin"]),
            "forkedFrom": row["forked_from"],
            "deletedAt": row["deleted_at"],
            "currentRevision": row["current_revision"],
            "currentRevisionId": row["current_revision_id"],
            "currentRevisionDigest": row["current_revision_digest"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def update_bot(self, bot_id: str, update: BotUpdate) -> dict[str, Any]:
        current = self.get_bot(bot_id)
        if update.expected_revision is not None and update.expected_revision != current["currentRevision"]:
            raise ConflictError(
                f"Bot changed since it was opened. Current revision is {current['currentRevision']}."
            )
        strategy = update.strategy or StrategyDocument.model_validate(current["strategy"])
        if strategy.race.value != current["race"]:
            raise ConflictError("Changing a bot's race requires creating or forking a new bot.")
        next_revision = current["currentRevision"] + 1
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                "SELECT current_revision FROM bots WHERE id = ?",
                (bot_id,),
            ).fetchone()
            if latest is None:
                raise NotFoundError(f"Bot not found: {bot_id}")
            if latest["current_revision"] != current["currentRevision"]:
                raise ConflictError(
                    "Bot changed while the new revision was being saved."
                )
            revision = self._insert_revision(
                connection,
                bot_id,
                next_revision,
                strategy,
                update.change_summary,
                now,
            )
            updated = connection.execute(
                """
                UPDATE bots
                SET name = ?, description = ?, tags_json = ?,
                    current_revision = ?, current_revision_id = ?,
                    current_revision_digest = ?, updated_at = ?
                WHERE id = ? AND current_revision = ?
                """,
                (
                    update.name if update.name is not None else current["name"],
                    update.description if update.description is not None else current["description"],
                    json.dumps(update.tags if update.tags is not None else current["tags"]),
                    next_revision,
                    revision["id"],
                    revision["content_digest"],
                    now,
                    bot_id,
                    current["currentRevision"],
                ),
            )
            if updated.rowcount != 1:
                raise ConflictError("Bot changed while the new revision was being saved.")
        return self.get_bot(bot_id)

    def fork_bot(self, bot_id: str, requested_name: str | None = None) -> dict[str, Any]:
        source = self.get_bot(bot_id)
        name = requested_name or f"{source['name']} Fork"
        return self.create_bot(
            BotCreate(
                name=name,
                description=f"Forked from {source['name']}. {source['description']}".strip(),
                race=source["race"],
                tags=[tag for tag in source["tags"] if tag != "built-in"] + ["fork"],
                strategy=StrategyDocument.model_validate(source["strategy"]),
            ),
            forked_from=source["id"],
            summary=f"Forked from {source['name']}",
        )

    def trash_bot(self, bot_id: str) -> dict[str, Any]:
        self.get_bot(bot_id)
        with self.connect() as connection:
            connection.execute(
                "UPDATE bots SET deleted_at = ?, updated_at = ? WHERE id = ?",
                (utc_now(), utc_now(), bot_id),
            )
        return self.get_bot(bot_id)

    def restore_bot(self, bot_id: str) -> dict[str, Any]:
        self.get_bot(bot_id)
        with self.connect() as connection:
            connection.execute(
                "UPDATE bots SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                (utc_now(), bot_id),
            )
        return self.get_bot(bot_id)

    def list_revisions(self, bot_id: str) -> list[dict[str, Any]]:
        self.get_bot(bot_id)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, number, content_digest, summary, created_at
                FROM revisions WHERE bot_id = ? ORDER BY number DESC
                """,
                (bot_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def restore_revision(
        self, bot_id: str, revision_number: int, summary: str
    ) -> dict[str, Any]:
        current = self.get_bot(bot_id)
        with self.connect() as connection:
            row = self._revision_identity(connection, bot_id, revision_number)
        return self.update_bot(
            bot_id,
            BotUpdate(
                strategy=StrategyDocument.model_validate_json(row["strategy_json"]),
                change_summary=summary,
                expected_revision=current["currentRevision"],
            ),
        )

    def create_proposal(
        self, proposal: StrategyProposal, base_bot_id: UUID | None
    ) -> dict[str, Any]:
        proposal_id = str(uuid4())
        now = utc_now()
        normalized_base_id = str(base_bot_id) if base_bot_id else None
        with self.connect() as connection:
            base_revision: dict[str, Any] | None = None
            if normalized_base_id:
                base = connection.execute(
                    "SELECT current_revision FROM bots WHERE id = ?",
                    (normalized_base_id,),
                ).fetchone()
                if base is None:
                    raise NotFoundError(f"Bot not found: {normalized_base_id}")
                base_revision = self._revision_identity(
                    connection,
                    normalized_base_id,
                    base["current_revision"],
                )
            connection.execute(
                """
                INSERT INTO proposals (
                    id, base_bot_id, base_revision, base_revision_id,
                    base_revision_digest, payload_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    proposal_id,
                    normalized_base_id,
                    base_revision["number"] if base_revision else None,
                    base_revision["id"] if base_revision else None,
                    base_revision["content_digest"] if base_revision else None,
                    proposal.model_dump_json(),
                    now,
                ),
            )
        return self.get_proposal(proposal_id)

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Proposal not found: {proposal_id}")
        return {
            "id": row["id"],
            "baseBotId": row["base_bot_id"],
            "baseRevision": row["base_revision"],
            "baseRevisionId": row["base_revision_id"],
            "baseRevisionDigest": row["base_revision_digest"],
            "status": row["status"],
            "createdAt": row["created_at"],
            "appliedAt": row["applied_at"],
            "proposal": json.loads(row["payload_json"]),
        }

    def set_proposal_status(self, proposal_id: str, status: str) -> dict[str, Any]:
        if status not in {"applied", "rejected"}:
            raise ValueError("Unsupported proposal status.")
        proposal = self.get_proposal(proposal_id)
        if proposal["status"] != "pending":
            raise ConflictError("This proposal has already been resolved.")
        with self.connect() as connection:
            connection.execute(
                "UPDATE proposals SET status = ?, applied_at = ? WHERE id = ?",
                (status, utc_now() if status == "applied" else None, proposal_id),
            )
        return self.get_proposal(proposal_id)

    def get_bot_revision(
        self, bot_id_or_slug: str, revision_number: int | None = None
    ) -> dict[str, Any]:
        bot = self.get_bot(bot_id_or_slug, include_deleted=False)
        revision_number = revision_number or bot["currentRevision"]
        with self.connect() as connection:
            row = self._revision_identity(
                connection,
                bot["id"],
                revision_number,
            )
        return {
            **bot,
            "strategy": StrategyDocument.model_validate_json(
                row["strategy_json"]
            ).model_dump(mode="json"),
            "revisionSummary": row["summary"],
            "revisionCreatedAt": row["created_at"],
            "selectedRevision": revision_number,
            "selectedRevisionId": row["id"],
            "selectedRevisionDigest": row["content_digest"],
        }

    # Match history -----------------------------------------------------

    def create_match(
        self,
        *,
        map_name: str,
        source: str,
        participants: list[dict[str, Any]],
        regression_batch_id: str | None = None,
        regression_game_id: str | None = None,
        match_id: str | None = None,
    ) -> dict[str, Any]:
        if len(participants) != 2:
            raise ValueError("A match requires exactly two participants.")
        match_id = match_id or str(uuid4())
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO matches (
                    id, source, map_name, status, regression_batch_id,
                    created_at, started_at
                ) VALUES (?, ?, ?, 'starting', ?, ?, ?)
                """,
                (match_id, source, map_name, regression_batch_id, now, now),
            )
            for slot, participant in enumerate(participants, start=1):
                revision: dict[str, Any] | None = None
                if participant["participant_type"] == "bot":
                    participant_bot_id = participant.get("bot_id")
                    participant_revision = participant.get("bot_revision")
                    if participant_bot_id is None or participant_revision is None:
                        raise ValueError(
                            "Bot match participants require a pinned revision."
                        )
                    revision = self._revision_identity(
                        connection,
                        participant_bot_id,
                        int(participant_revision),
                    )
                connection.execute(
                    """
                    INSERT INTO match_participants (
                        match_id, slot, participant_type, bot_id, bot_revision,
                        bot_revision_id, bot_revision_digest,
                        name, requested_race, resolved_race, difficulty, result
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        match_id,
                        slot,
                        participant["participant_type"],
                        participant.get("bot_id"),
                        participant.get("bot_revision"),
                        revision["id"] if revision else None,
                        revision["content_digest"] if revision else None,
                        participant["name"],
                        participant["requested_race"],
                        participant.get("resolved_race"),
                        participant.get("difficulty"),
                    ),
                )
            if regression_game_id is not None:
                linked = connection.execute(
                    """
                    UPDATE regression_games
                    SET status = 'starting', match_id = ?
                    WHERE id = ? AND batch_id = ?
                    """,
                    (match_id, regression_game_id, regression_batch_id),
                )
                if linked.rowcount == 0:
                    raise NotFoundError(
                        f"Regression game not found: {regression_game_id}"
                    )
        return self.get_match(match_id)

    def set_match_running(self, match_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            updated = connection.execute(
                "UPDATE matches SET status = 'running', started_at = ? WHERE id = ?",
                (utc_now(), match_id),
            )
        if updated.rowcount == 0:
            raise NotFoundError(f"Match not found: {match_id}")
        return self.get_match(match_id)

    def update_match_game_time(
        self, match_id: str, game_time_seconds: float
    ) -> None:
        with self.connect() as connection:
            updated = connection.execute(
                """
                UPDATE matches SET game_time_seconds = ?
                WHERE id = ? AND status IN ('starting', 'running', 'stopping')
                """,
                (game_time_seconds, match_id),
            )
        if updated.rowcount == 0:
            raise NotFoundError(f"Active match not found: {match_id}")

    def append_match_log(
        self,
        match_id: str,
        sequence: int,
        line: str,
        *,
        retain: int,
    ) -> None:
        if sequence < 1:
            raise ValueError("Match log sequences start at 1.")
        if retain < 1:
            raise ValueError("At least one match log must be retained.")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO match_logs (match_id, sequence, line, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (match_id, sequence, line, utc_now()),
            )
            connection.execute(
                """
                DELETE FROM match_logs
                WHERE match_id = ? AND sequence <= ?
                """,
                (match_id, sequence - retain),
            )

    def list_match_logs(
        self, match_id: str, *, after: int = 0
    ) -> dict[str, Any]:
        self.get_match(match_id)
        cursor = max(0, after)
        with self.connect() as connection:
            bounds = connection.execute(
                """
                SELECT MIN(sequence) AS first_sequence,
                       MAX(sequence) AS last_sequence
                FROM match_logs WHERE match_id = ?
                """,
                (match_id,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT sequence, line FROM match_logs
                WHERE match_id = ? AND sequence > ?
                ORDER BY sequence
                """,
                (match_id, cursor),
            ).fetchall()
        return {
            "firstSequence": bounds["first_sequence"],
            "lastSequence": bounds["last_sequence"],
            "items": [
                {"sequence": row["sequence"], "line": row["line"]}
                for row in rows
            ],
        }

    def get_match_log_bounds(self, match_id: str) -> dict[str, int | None]:
        self.get_match(match_id)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(sequence) AS first_sequence,
                       MAX(sequence) AS last_sequence,
                       COUNT(*) AS retained_count
                FROM match_logs WHERE match_id = ?
                """,
                (match_id,),
            ).fetchone()
        return {
            "firstSequence": row["first_sequence"],
            "lastSequence": row["last_sequence"],
            "retainedCount": row["retained_count"],
        }

    def finalize_match(
        self,
        match_id: str,
        *,
        status: str,
        return_code: int | None = None,
        game_time_seconds: float | None = None,
        failure_reason: str | None = None,
        participant_results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "stopped"}:
            raise ValueError(f"Unsupported terminal match status: {status}")
        with self.connect() as connection:
            updated = connection.execute(
                """
                UPDATE matches SET status = ?, return_code = ?,
                    game_time_seconds = COALESCE(?, game_time_seconds),
                    failure_reason = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    return_code,
                    game_time_seconds,
                    failure_reason,
                    utc_now(),
                    match_id,
                ),
            )
            if updated.rowcount == 0:
                raise NotFoundError(f"Match not found: {match_id}")
            for result in participant_results or []:
                connection.execute(
                    """
                    UPDATE match_participants
                    SET result = ?, resolved_race = COALESCE(?, resolved_race)
                    WHERE match_id = ? AND slot = ?
                    """,
                    (
                        result.get("result"),
                        result.get("resolvedRace"),
                        match_id,
                        result["slot"],
                    ),
                )
        return self.get_match(match_id)

    def get_match(self, match_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM matches WHERE id = ?", (match_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Match not found: {match_id}")
            participants = connection.execute(
                """
                SELECT * FROM match_participants
                WHERE match_id = ? ORDER BY slot
                """,
                (match_id,),
            ).fetchall()
        return self._match_row(row, participants)

    def _match_row(
        self, row: sqlite3.Row, participants: list[sqlite3.Row]
    ) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source": row["source"],
            "mapName": row["map_name"],
            "status": row["status"],
            "gameTimeSeconds": row["game_time_seconds"],
            "returnCode": row["return_code"],
            "failureReason": row["failure_reason"],
            "regressionBatchId": row["regression_batch_id"],
            "createdAt": row["created_at"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "participants": [
                {
                    "slot": participant["slot"],
                    "participantType": participant["participant_type"],
                    "botId": participant["bot_id"],
                    "botRevision": participant["bot_revision"],
                    "botRevisionId": participant["bot_revision_id"],
                    "botRevisionDigest": participant["bot_revision_digest"],
                    "name": participant["name"],
                    "requestedRace": participant["requested_race"],
                    "resolvedRace": participant["resolved_race"],
                    "difficulty": participant["difficulty"],
                    "result": participant["result"],
                }
                for participant in participants
            ],
        }

    def list_bot_matches(
        self,
        bot_id: str,
        *,
        opponent_type: str | None = None,
        enemy_race: str | None = None,
        difficulty: str | None = None,
        map_name: str | None = None,
        result: str | None = None,
        include_regression: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.get_bot(bot_id)
        clauses = ["mine.bot_id = ?"]
        parameters: list[Any] = [bot_id]
        if result:
            clauses.append("mine.result = ?")
            parameters.append(result)
        if map_name:
            clauses.append("m.map_name = ?")
            parameters.append(map_name)
        if not include_regression:
            clauses.append("m.source != 'regression'")
        if opponent_type:
            clauses.append("other.participant_type = ?")
            parameters.append(opponent_type)
        if enemy_race:
            clauses.append("COALESCE(other.resolved_race, other.requested_race) = ?")
            parameters.append(enemy_race)
        if difficulty:
            clauses.append("other.difficulty = ?")
            parameters.append(difficulty)
        where = " AND ".join(clauses)
        base = f"""
            FROM matches m
            JOIN match_participants mine ON mine.match_id = m.id
            JOIN match_participants other
                ON other.match_id = m.id AND other.slot != mine.slot
            WHERE {where}
        """
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS amount {base}", parameters
            ).fetchone()["amount"]
            ids = connection.execute(
                f"""
                SELECT m.id {base}
                ORDER BY m.created_at DESC LIMIT ? OFFSET ?
                """,
                [*parameters, limit, offset],
            ).fetchall()
        items = [self.get_match(row["id"]) for row in ids]
        for item in items:
            mine = next(p for p in item["participants"] if p["botId"] == bot_id)
            other = next(p for p in item["participants"] if p["slot"] != mine["slot"])
            item["perspectiveResult"] = mine["result"]
            item["opponent"] = other
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def bot_stats(
        self, bot_id: str, *, include_regression: bool = True
    ) -> dict[str, Any]:
        self.get_bot(bot_id)
        clauses = ["mine.bot_id = ?"]
        parameters: list[Any] = [bot_id]
        if not include_regression:
            clauses.append("m.source != 'regression'")
        where = " AND ".join(clauses)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT m.status, m.game_time_seconds, m.source, m.map_name,
                       mine.result, other.participant_type,
                       other.name AS opponent_name,
                       COALESCE(other.resolved_race, other.requested_race) AS enemy_race,
                       other.difficulty
                FROM matches m
                JOIN match_participants mine ON mine.match_id = m.id
                JOIN match_participants other
                    ON other.match_id = m.id AND other.slot != mine.slot
                WHERE {where}
                """,
                parameters,
            ).fetchall()

        counts = {
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "undecided": 0,
            "stopped": 0,
            "failed": 0,
        }
        durations: list[float] = []
        breakdown: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
        for row in rows:
            if row["status"] == "stopped":
                counts["stopped"] += 1
            elif row["status"] == "failed":
                counts["failed"] += 1
            elif row["result"] == "victory":
                counts["wins"] += 1
            elif row["result"] == "defeat":
                counts["losses"] += 1
            elif row["result"] == "tie":
                counts["ties"] += 1
            else:
                counts["undecided"] += 1
            if row["status"] == "completed" and row["game_time_seconds"] is not None:
                durations.append(float(row["game_time_seconds"]))

            key = (
                row["participant_type"],
                row["opponent_name"],
                row["enemy_race"],
                row["difficulty"],
            )
            group = breakdown.setdefault(
                key,
                {
                    "opponentType": row["participant_type"],
                    "opponentName": row["opponent_name"],
                    "enemyRace": row["enemy_race"],
                    "difficulty": row["difficulty"],
                    "wins": 0,
                    "losses": 0,
                    "ties": 0,
                    "games": 0,
                },
            )
            if row["result"] in {"victory", "defeat", "tie"}:
                group["games"] += 1
                field = {
                    "victory": "wins",
                    "defeat": "losses",
                    "tie": "ties",
                }[row["result"]]
                group[field] += 1

        decisive = counts["wins"] + counts["losses"]
        return {
            "botId": bot_id,
            "totalRuns": len(rows),
            "completedMatches": sum(
                1 for row in rows if row["status"] == "completed"
            ),
            **counts,
            "winRate": counts["wins"] / decisive if decisive else None,
            "averageGameTimeSeconds": (
                sum(durations) / len(durations) if durations else None
            ),
            "breakdown": sorted(
                breakdown.values(),
                key=lambda item: (-item["games"], item["opponentName"].lower()),
            ),
        }

    def interrupt_active_matches(self) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE matches SET status = 'failed',
                    failure_reason = 'Backend restarted while match was active',
                    finished_at = ?
                WHERE status IN ('starting', 'running', 'stopping')
                """,
                (now,),
            )
            active_batches = connection.execute(
                """
                SELECT id FROM regression_batches
                WHERE status IN ('starting', 'running', 'cancelling')
                """
            ).fetchall()
            for batch in active_batches:
                connection.execute(
                    """
                    UPDATE regression_batches SET status = 'interrupted'
                    WHERE id = ?
                    """,
                    (batch["id"],),
                )
                connection.execute(
                    """
                    UPDATE regression_games SET status = 'queued', match_id = NULL
                    WHERE batch_id = ? AND status IN ('starting', 'running')
                    """,
                    (batch["id"],),
                )

    # Benchmark suites -------------------------------------------------

    def create_benchmark_suite(self, suite: BenchmarkSuiteCreate) -> dict[str, Any]:
        suite_id = str(uuid4())
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO benchmark_suites (
                    id, name, description, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (suite_id, suite.name, suite.description, now, now),
            )
            self._replace_benchmark_scenarios(connection, suite_id, suite)
        return self.get_benchmark_suite(suite_id)

    def _replace_benchmark_scenarios(
        self,
        connection: sqlite3.Connection,
        suite_id: str,
        suite: BenchmarkSuiteCreate,
    ) -> None:
        connection.execute(
            "DELETE FROM benchmark_scenarios WHERE suite_id = ?", (suite_id,)
        )
        for position, scenario in enumerate(suite.scenarios):
            opponent_bot_id = (
                str(scenario.opponent_bot_id) if scenario.opponent_bot_id else None
            )
            opponent_revision: dict[str, Any] | None = None
            if scenario.opponent_type == "bot":
                if opponent_bot_id is None or scenario.opponent_revision is None:
                    raise ValueError(
                        "Bot benchmark opponents require a pinned revision."
                    )
                opponent_revision = self._revision_identity(
                    connection,
                    opponent_bot_id,
                    scenario.opponent_revision,
                )
            connection.execute(
                """
                INSERT INTO benchmark_scenarios (
                    id, suite_id, position, name, map_name, opponent_type,
                    enemy_race, difficulty, opponent_bot_id, opponent_revision,
                    opponent_revision_id, opponent_revision_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    suite_id,
                    position,
                    scenario.name,
                    scenario.map_name,
                    scenario.opponent_type,
                    scenario.enemy_race if scenario.opponent_type == "computer" else None,
                    scenario.difficulty if scenario.opponent_type == "computer" else None,
                    opponent_bot_id,
                    scenario.opponent_revision,
                    opponent_revision["id"] if opponent_revision else None,
                    (
                        opponent_revision["content_digest"]
                        if opponent_revision
                        else None
                    ),
                ),
            )

    def update_benchmark_suite(
        self, suite_id: str, suite: BenchmarkSuiteCreate
    ) -> dict[str, Any]:
        self.get_benchmark_suite(suite_id)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE benchmark_suites
                SET name = ?, description = ?, updated_at = ?
                WHERE id = ?
                """,
                (suite.name, suite.description, utc_now(), suite_id),
            )
            self._replace_benchmark_scenarios(connection, suite_id, suite)
        return self.get_benchmark_suite(suite_id)

    def get_benchmark_suite(self, suite_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM benchmark_suites WHERE id = ?", (suite_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Benchmark suite not found: {suite_id}")
            scenarios = connection.execute(
                """
                SELECT * FROM benchmark_scenarios
                WHERE suite_id = ? ORDER BY position
                """,
                (suite_id,),
            ).fetchall()
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "archivedAt": row["archived_at"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "scenarios": [
                {
                    "id": scenario["id"],
                    "position": scenario["position"],
                    "name": scenario["name"],
                    "mapName": scenario["map_name"],
                    "opponentType": scenario["opponent_type"],
                    "enemyRace": scenario["enemy_race"],
                    "difficulty": scenario["difficulty"],
                    "opponentBotId": scenario["opponent_bot_id"],
                    "opponentRevision": scenario["opponent_revision"],
                    "opponentRevisionId": scenario["opponent_revision_id"],
                    "opponentRevisionDigest": scenario["opponent_revision_digest"],
                }
                for scenario in scenarios
            ],
        }

    def list_benchmark_suites(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id FROM benchmark_suites {where}
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [self.get_benchmark_suite(row["id"]) for row in rows]

    def duplicate_benchmark_suite(self, suite_id: str) -> dict[str, Any]:
        source = self.get_benchmark_suite(suite_id)
        return self.create_benchmark_suite(
            BenchmarkSuiteCreate.model_validate(
                {
                    "name": f"{source['name']} Copy",
                    "description": source["description"],
                    "scenarios": [
                        {
                            "name": item["name"],
                            "map_name": item["mapName"],
                            "opponent_type": item["opponentType"],
                            "enemy_race": item["enemyRace"] or "zerg",
                            "difficulty": item["difficulty"] or "easy",
                            "opponent_bot_id": item["opponentBotId"],
                            "opponent_revision": item["opponentRevision"],
                        }
                        for item in source["scenarios"]
                    ],
                }
            )
        )

    def archive_benchmark_suite(self, suite_id: str) -> dict[str, Any]:
        self.get_benchmark_suite(suite_id)
        with self.connect() as connection:
            connection.execute(
                "UPDATE benchmark_suites SET archived_at = ?, updated_at = ? WHERE id = ?",
                (utc_now(), utc_now(), suite_id),
            )
        return self.get_benchmark_suite(suite_id)

    # Regression batches -----------------------------------------------

    def create_regression_batch(
        self,
        *,
        bot_id: str,
        baseline_revision: int,
        suite_id: str,
        games_per_scenario: int,
        concurrency: int,
    ) -> dict[str, Any]:
        candidate = self.get_bot_revision(bot_id)
        baseline = self.get_bot_revision(bot_id, baseline_revision)
        if baseline_revision == candidate["currentRevision"]:
            raise ConflictError("Baseline revision must be older than the current revision.")
        suite = self.get_benchmark_suite(suite_id)
        if suite["archivedAt"]:
            raise ConflictError("Archived benchmark suites cannot be launched.")
        for scenario in suite["scenarios"]:
            if scenario["opponentType"] == "bot":
                if scenario["opponentBotId"] == bot_id:
                    raise ConflictError(
                        "A regression benchmark cannot use the tested bot as its opponent."
                    )
                self.get_bot_revision(
                    scenario["opponentBotId"], scenario["opponentRevision"]
                )

        batch_id = str(uuid4())
        total_games = len(suite["scenarios"]) * games_per_scenario * 2
        now = utc_now()
        seed_base = int(batch_id.replace("-", "")[:8], 16)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO regression_batches (
                    id, bot_id, candidate_revision, baseline_revision,
                    candidate_revision_id, candidate_revision_digest,
                    baseline_revision_id, baseline_revision_digest,
                    suite_id, suite_name, games_per_scenario, concurrency,
                    status, total_games, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    batch_id,
                    bot_id,
                    candidate["currentRevision"],
                    baseline["selectedRevision"],
                    candidate["selectedRevisionId"],
                    candidate["selectedRevisionDigest"],
                    baseline["selectedRevisionId"],
                    baseline["selectedRevisionDigest"],
                    suite_id,
                    suite["name"],
                    games_per_scenario,
                    concurrency,
                    total_games,
                    now,
                ),
            )
            for scenario in suite["scenarios"]:
                for repetition in range(1, games_per_scenario + 1):
                    seed = (
                        seed_base
                        + scenario["position"] * 1009
                        + repetition * 9176
                    ) % 2_147_483_647
                    for role, revision in (
                        ("candidate", candidate),
                        ("baseline", baseline),
                    ):
                        connection.execute(
                            """
                            INSERT INTO regression_games (
                                id, batch_id, scenario_position, scenario_name,
                                map_name, opponent_type, enemy_race, difficulty,
                                opponent_bot_id, opponent_revision,
                                opponent_revision_id, opponent_revision_digest,
                                tested_role, tested_revision, tested_revision_id,
                                tested_revision_digest, repetition, random_seed,
                                status
                            ) VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, 'queued'
                            )
                            """,
                            (
                                str(uuid4()),
                                batch_id,
                                scenario["position"],
                                scenario["name"],
                                scenario["mapName"],
                                scenario["opponentType"],
                                scenario["enemyRace"],
                                scenario["difficulty"],
                                scenario["opponentBotId"],
                                scenario["opponentRevision"],
                                scenario["opponentRevisionId"],
                                scenario["opponentRevisionDigest"],
                                role,
                                revision["selectedRevision"],
                                revision["selectedRevisionId"],
                                revision["selectedRevisionDigest"],
                                repetition,
                                seed,
                            ),
                        )
        return self.get_regression_batch(batch_id)

    def get_regression_game(self, game_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM regression_games WHERE id = ?", (game_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Regression game not found: {game_id}")
        return self._regression_game_row(row)

    def list_queued_regression_games(self, batch_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM regression_games
                WHERE batch_id = ? AND status = 'queued'
                ORDER BY scenario_position, repetition,
                    CASE tested_role WHEN 'candidate' THEN 0 ELSE 1 END
                """,
                (batch_id,),
            ).fetchall()
        return [self._regression_game_row(row) for row in rows]

    def set_regression_game(
        self, game_id: str, *, status: str, match_id: str | None = None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE regression_games SET status = ?, match_id = COALESCE(?, match_id)
                WHERE id = ?
                """,
                (status, match_id, game_id),
            )
        self.refresh_regression_progress(self.get_regression_game(game_id)["batchId"])

    def set_regression_batch_status(self, batch_id: str, status: str) -> None:
        started = utc_now() if status == "running" else None
        finished = (
            utc_now()
            if status
            in {
                "completed",
                "completed_with_failures",
                "cancelled",
                "failed",
                "interrupted",
            }
            else None
        )
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE regression_batches
                SET status = ?,
                    started_at = COALESCE(started_at, ?),
                    finished_at = ?
                WHERE id = ?
                """,
                (status, started, finished, batch_id),
            )

    def cancel_queued_regression_games(self, batch_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE regression_games SET status = 'cancelled'
                WHERE batch_id = ? AND status = 'queued'
                """,
                (batch_id,),
            )
        self.refresh_regression_progress(batch_id)

    def refresh_regression_progress(self, batch_id: str) -> None:
        with self.connect() as connection:
            completed = connection.execute(
                """
                SELECT COUNT(*) AS amount FROM regression_games
                WHERE batch_id = ?
                  AND status IN ('completed', 'failed', 'stopped')
                """,
                (batch_id,),
            ).fetchone()["amount"]
            connection.execute(
                "UPDATE regression_batches SET completed_games = ? WHERE id = ?",
                (completed, batch_id),
            )

    def get_regression_batch(self, batch_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM regression_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Regression batch not found: {batch_id}")
            games = connection.execute(
                """
                SELECT * FROM regression_games WHERE batch_id = ?
                ORDER BY scenario_position, repetition,
                    CASE tested_role WHEN 'candidate' THEN 0 ELSE 1 END
                """,
                (batch_id,),
            ).fetchall()
        game_items = [self._regression_game_row(game) for game in games]
        match_cache: dict[str, dict[str, Any]] = {}

        games_by_sample: dict[tuple[int, int, int], dict[str, dict[str, Any]]] = {}
        for game in game_items:
            sample_key = (
                int(game["scenarioPosition"]),
                int(game["repetition"]),
                int(game["randomSeed"]),
            )
            games_by_sample.setdefault(sample_key, {})[game["testedRole"]] = game

        paired_samples = [
            roles
            for roles in games_by_sample.values()
            if set(roles) == {"candidate", "baseline"}
            and all(
                game["status"] == "completed" and game["matchId"]
                for game in roles.values()
            )
        ]
        paired_games = [
            roles[role]
            for roles in paired_samples
            for role in ("candidate", "baseline")
        ]

        def summarize(relevant: list[dict[str, Any]]) -> dict[str, Any]:
            results: list[str] = []
            durations: list[float] = []
            for game in relevant:
                if not game["matchId"]:
                    continue
                if game["matchId"] not in match_cache:
                    match_cache[game["matchId"]] = self.get_match(game["matchId"])
                match = match_cache[game["matchId"]]
                mine = next(
                    (
                        participant
                        for participant in match["participants"]
                        if participant["botId"] == row["bot_id"]
                        and participant["botRevision"] == game["testedRevision"]
                    ),
                    None,
                )
                if mine and mine["result"]:
                    results.append(mine["result"])
                if (
                    match["status"] == "completed"
                    and match["gameTimeSeconds"] is not None
                ):
                    durations.append(match["gameTimeSeconds"])
            wins = results.count("victory")
            losses = results.count("defeat")
            decisive = wins + losses
            return {
                "wins": wins,
                "losses": losses,
                "ties": results.count("tie"),
                "undecided": results.count("undecided"),
                "winRate": wins / decisive if decisive else None,
                "averageGameTimeSeconds": (
                    sum(durations) / len(durations) if durations else None
                ),
            }

        role_stats = {
            role: summarize(
                [game for game in paired_games if game["testedRole"] == role]
            )
            for role in ("candidate", "baseline")
        }
        candidate_rate = role_stats["candidate"]["winRate"]
        baseline_rate = role_stats["baseline"]["winRate"]
        scenario_comparisons: list[dict[str, Any]] = []
        scenario_positions = sorted(
            {int(game["scenarioPosition"]) for game in game_items}
        )
        for position in scenario_positions:
            scenario_games = [
                game
                for game in paired_games
                if int(game["scenarioPosition"]) == position
            ]
            candidate = summarize(
                [game for game in scenario_games if game["testedRole"] == "candidate"]
            )
            baseline = summarize(
                [game for game in scenario_games if game["testedRole"] == "baseline"]
            )
            scenario_candidate_rate = candidate["winRate"]
            scenario_baseline_rate = baseline["winRate"]
            scenario_comparisons.append(
                {
                    "position": position,
                    "name": next(
                        game["scenarioName"]
                        for game in game_items
                        if int(game["scenarioPosition"]) == position
                    ),
                    "pairedSamples": len(scenario_games) // 2,
                    "candidate": candidate,
                    "baseline": baseline,
                    "winRateDelta": (
                        scenario_candidate_rate - scenario_baseline_rate
                        if scenario_candidate_rate is not None
                        and scenario_baseline_rate is not None
                        else None
                    ),
                }
            )
        return {
            "id": row["id"],
            "botId": row["bot_id"],
            "candidateRevision": row["candidate_revision"],
            "baselineRevision": row["baseline_revision"],
            "candidateRevisionId": row["candidate_revision_id"],
            "candidateRevisionDigest": row["candidate_revision_digest"],
            "baselineRevisionId": row["baseline_revision_id"],
            "baselineRevisionDigest": row["baseline_revision_digest"],
            "suiteId": row["suite_id"],
            "suiteName": row["suite_name"],
            "gamesPerScenario": row["games_per_scenario"],
            "concurrency": row["concurrency"],
            "status": row["status"],
            "totalGames": row["total_games"],
            "completedGames": row["completed_games"],
            "pairedSamples": len(paired_samples),
            "createdAt": row["created_at"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "games": game_items,
            "scenarioComparisons": scenario_comparisons,
            "comparison": {
                "candidate": role_stats["candidate"],
                "baseline": role_stats["baseline"],
                "winRateDelta": (
                    candidate_rate - baseline_rate
                    if candidate_rate is not None and baseline_rate is not None
                    else None
                ),
            },
        }

    def list_regression_batches(self, bot_id: str) -> list[dict[str, Any]]:
        self.get_bot(bot_id)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM regression_batches
                WHERE bot_id = ? ORDER BY created_at DESC
                """,
                (bot_id,),
            ).fetchall()
        return [self.get_regression_batch(row["id"]) for row in rows]

    def _regression_game_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "batchId": row["batch_id"],
            "scenarioPosition": row["scenario_position"],
            "scenarioName": row["scenario_name"],
            "mapName": row["map_name"],
            "opponentType": row["opponent_type"],
            "enemyRace": row["enemy_race"],
            "difficulty": row["difficulty"],
            "opponentBotId": row["opponent_bot_id"],
            "opponentRevision": row["opponent_revision"],
            "opponentRevisionId": row["opponent_revision_id"],
            "opponentRevisionDigest": row["opponent_revision_digest"],
            "testedRole": row["tested_role"],
            "testedRevision": row["tested_revision"],
            "testedRevisionId": row["tested_revision_id"],
            "testedRevisionDigest": row["tested_revision_digest"],
            "repetition": row["repetition"],
            "randomSeed": row["random_seed"],
            "matchId": row["match_id"],
            "status": row["status"],
        }
