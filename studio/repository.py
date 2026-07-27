from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv

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

    def connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self, seed: bool = True) -> None:
        with self.connect() as connection:
            connection.executescript(
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
                );

                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY,
                    bot_id TEXT NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                    number INTEGER NOT NULL,
                    strategy_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(bot_id, number)
                );

                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    base_bot_id TEXT REFERENCES bots(id),
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    applied_at TEXT
                );

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
                );

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
                );

                CREATE INDEX IF NOT EXISTS idx_match_participants_bot
                    ON match_participants(bot_id, match_id);
                CREATE INDEX IF NOT EXISTS idx_matches_created
                    ON matches(created_at DESC);

                CREATE TABLE IF NOT EXISTS benchmark_suites (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    archived_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

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
                );

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
                );

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
                );

                CREATE INDEX IF NOT EXISTS idx_regression_games_batch
                    ON regression_games(batch_id, status, scenario_position, repetition);
                """
            )
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
                    "SELECT id, is_builtin, current_revision FROM bots WHERE slug = ?",
                    (fixture["slug"],),
                ).fetchone()
            if exists:
                if exists["is_builtin"] and exists["current_revision"] == 1:
                    self._refresh_pristine_builtin(exists["id"], fixture, strategy)
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

    def _refresh_pristine_builtin(
        self,
        bot_id: str,
        fixture: dict[str, Any],
        strategy: StrategyDocument,
    ) -> None:
        """Keep untouched seed records aligned with their checked-in fixture."""
        serialized = strategy.model_dump_json()
        with self.connect() as connection:
            revision = connection.execute(
                "SELECT strategy_json FROM revisions WHERE bot_id = ? AND number = 1",
                (bot_id,),
            ).fetchone()
            if revision is None:
                return
            if json.loads(revision["strategy_json"]) == json.loads(serialized):
                return
            now = utc_now()
            connection.execute(
                """
                UPDATE revisions SET strategy_json = ?, created_at = ?
                WHERE bot_id = ? AND number = 1
                """,
                (serialized, now, bot_id),
            )
            connection.execute(
                """
                UPDATE bots
                SET name = ?, description = ?, race = ?, tags_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    fixture["name"],
                    fixture["description"],
                    fixture["race"],
                    json.dumps(fixture["tags"]),
                    now,
                    bot_id,
                ),
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
            self._insert_revision(
                connection,
                bot_id,
                1,
                bot.strategy,
                summary,
                now,
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
    ) -> None:
        connection.execute(
            """
            INSERT INTO revisions (id, bot_id, number, strategy_json, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                bot_id,
                number,
                strategy.model_dump_json(),
                summary,
                created_at or utc_now(),
            ),
        )

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
            revision = connection.execute(
                "SELECT * FROM revisions WHERE bot_id = ? AND number = ?",
                (row["id"], row["current_revision"]),
            ).fetchone()
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
            connection.execute(
                """
                UPDATE bots
                SET name = ?, description = ?, tags_json = ?, current_revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    update.name if update.name is not None else current["name"],
                    update.description if update.description is not None else current["description"],
                    json.dumps(update.tags if update.tags is not None else current["tags"]),
                    next_revision,
                    now,
                    bot_id,
                ),
            )
            self._insert_revision(
                connection,
                bot_id,
                next_revision,
                strategy,
                update.change_summary,
                now,
            )
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
                SELECT id, number, summary, created_at
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
            row = connection.execute(
                "SELECT strategy_json FROM revisions WHERE bot_id = ? AND number = ?",
                (bot_id, revision_number),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Revision {revision_number} not found.")
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
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO proposals (id, base_bot_id, payload_json, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (
                    proposal_id,
                    str(base_bot_id) if base_bot_id else None,
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
            row = connection.execute(
                """
                SELECT strategy_json, summary, created_at
                FROM revisions WHERE bot_id = ? AND number = ?
                """,
                (bot["id"], revision_number),
            ).fetchone()
        if row is None:
            raise NotFoundError(
                f"Revision {revision_number} not found for {bot['name']}."
            )
        return {
            **bot,
            "strategy": StrategyDocument.model_validate_json(
                row["strategy_json"]
            ).model_dump(mode="json"),
            "revisionSummary": row["summary"],
            "revisionCreatedAt": row["created_at"],
            "selectedRevision": revision_number,
        }

    # Match history -----------------------------------------------------

    def create_match(
        self,
        *,
        map_name: str,
        source: str,
        participants: list[dict[str, Any]],
        regression_batch_id: str | None = None,
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
                connection.execute(
                    """
                    INSERT INTO match_participants (
                        match_id, slot, participant_type, bot_id, bot_revision,
                        name, requested_race, resolved_race, difficulty, result
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        match_id,
                        slot,
                        participant["participant_type"],
                        participant.get("bot_id"),
                        participant.get("bot_revision"),
                        participant["name"],
                        participant["requested_race"],
                        participant.get("resolved_race"),
                        participant.get("difficulty"),
                    ),
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
                    WHERE batch_id = ? AND status = 'running'
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
            connection.execute(
                """
                INSERT INTO benchmark_scenarios (
                    id, suite_id, position, name, map_name, opponent_type,
                    enemy_race, difficulty, opponent_bot_id, opponent_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    str(scenario.opponent_bot_id) if scenario.opponent_bot_id else None,
                    scenario.opponent_revision,
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
                    suite_id, suite_name, games_per_scenario, concurrency,
                    status, total_games, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    batch_id,
                    bot_id,
                    candidate["currentRevision"],
                    baseline["selectedRevision"],
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
                        ("candidate", candidate["currentRevision"]),
                        ("baseline", baseline["selectedRevision"]),
                    ):
                        connection.execute(
                            """
                            INSERT INTO regression_games (
                                id, batch_id, scenario_position, scenario_name,
                                map_name, opponent_type, enemy_race, difficulty,
                                opponent_bot_id, opponent_revision, tested_role,
                                tested_revision, repetition, random_seed, status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')
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
                                role,
                                revision,
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
            if status in {"completed", "cancelled", "failed", "interrupted"}
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
            "testedRole": row["tested_role"],
            "testedRevision": row["tested_revision"],
            "repetition": row["repetition"],
            "randomSeed": row["random_seed"],
            "matchId": row["match_id"],
            "status": row["status"],
        }
