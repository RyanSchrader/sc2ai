import argparse
import json
import sys
from typing import Any

from sc2 import maps
from sc2.data import Difficulty, Race, Result
from sc2.main import run_game
from sc2.player import Bot, Computer

from studio.catalog import RaceName
from studio.models import StrategyDocument
from studio.repository import NotFoundError, StudioRepository
from studio.runtime import DeclarativeBot

RESULT_PREFIX = "SC2_STUDIO_RESULT:"
PROGRESS_PREFIX = "SC2_STUDIO_PROGRESS:"

RACE_CHOICES = {
    "terran": Race.Terran,
    "protoss": Race.Protoss,
    "zerg": Race.Zerg,
    "random": Race.Random,
}

DIFFICULTY_CHOICES = {
    "very_easy": Difficulty.VeryEasy,
    "easy": Difficulty.Easy,
    "medium": Difficulty.Medium,
    "medium_hard": Difficulty.MediumHard,
    "hard": Difficulty.Hard,
    "harder": Difficulty.Harder,
    "very_hard": Difficulty.VeryHard,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Bot Studio strategy.")
    parser.add_argument(
        "--bot",
        default="protoss-basic",
        help="Bot UUID or slug. Use --list-bots to see available strategies.",
    )
    parser.add_argument(
        "--enemy-race",
        choices=RACE_CHOICES.keys(),
        default="zerg",
        help="Computer opponent race.",
    )
    parser.add_argument(
        "--difficulty",
        choices=DIFFICULTY_CHOICES.keys(),
        default="easy",
        help="Computer opponent difficulty.",
    )
    parser.add_argument(
        "--opponent-bot",
        default=None,
        help="Optional Studio bot UUID or slug to use instead of a computer.",
    )
    parser.add_argument(
        "--bot-revision",
        type=int,
        default=None,
        help="Run an exact strategy revision (used by regression tests).",
    )
    parser.add_argument(
        "--opponent-revision",
        type=int,
        default=None,
        help="Exact revision for --opponent-bot.",
    )
    parser.add_argument(
        "--random-seed",
        type=int,
        default=None,
        help="Optional SC2 random seed for reproducible paired matches.",
    )
    parser.add_argument(
        "--match-id",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--map",
        dest="map_name",
        default="AcropolisLE",
        help="Installed SC2 map name.",
    )
    parser.add_argument(
        "--list-bots",
        action="store_true",
        help="List active bot UUIDs and slugs, then exit.",
    )
    parser.add_argument(
        "--database",
        default=None,
        help="Optional Bot Studio SQLite database path.",
    )
    return parser.parse_args()


class InstrumentedDeclarativeBot(DeclarativeBot):
    final_result: Result | None = None
    final_game_time: float | None = None
    resolved_enemy_race: Race | None = None

    def __init__(
        self,
        strategy: StrategyDocument,
        *,
        accept_computer_surrender: bool = False,
        emit_progress: bool = False,
    ):
        super().__init__(
            strategy,
            accept_computer_surrender=accept_computer_surrender,
        )
        self.emit_progress = emit_progress
        self._last_progress_time = -30.0

    async def on_step(self, iteration: int) -> None:
        await super().on_step(iteration)
        if self.emit_progress and self.time - self._last_progress_time >= 30:
            self._last_progress_time = self.time
            print(
                f"{PROGRESS_PREFIX}"
                f"{json.dumps({'gameTimeSeconds': self.time}, separators=(',', ':'))}",
                flush=True,
            )

    async def on_end(self, game_result: Result) -> None:
        self.final_result = game_result
        try:
            self.final_game_time = float(self.time)
        except (AttributeError, TypeError):
            self.final_game_time = None
        self.resolved_enemy_race = self.enemy_race
        await super().on_end(game_result)


def _participant(
    record: dict[str, Any], revision: int, *, participant_type: str = "bot"
) -> dict[str, Any]:
    return {
        "participant_type": participant_type,
        "bot_id": record["id"],
        "bot_revision": revision,
        "name": record["name"],
        "requested_race": record["race"],
        "resolved_race": record["race"],
        "difficulty": None,
    }


def _opposite(result: str) -> str:
    return {
        "victory": "defeat",
        "defeat": "victory",
        "tie": "tie",
        "undecided": "undecided",
    }[result]


def _result_name(result: Result | None) -> str:
    return result.name.lower() if result is not None else "undecided"


def main() -> int:
    args = parse_args()
    repository = StudioRepository(args.database)
    repository.initialize()

    if args.list_bots:
        for bot in repository.list_bots():
            print(f"{bot['slug']:<26} {bot['race']:<8} {bot['name']}  ({bot['id']})")
        return 0

    try:
        record = repository.get_bot_revision(args.bot, args.bot_revision)
    except NotFoundError as exc:
        print(str(exc), file=sys.stderr)
        print("Run with --list-bots to see available bots.", file=sys.stderr)
        return 2

    try:
        selected_map = maps.get(args.map_name)
    except Exception as exc:
        print(f"Map '{args.map_name}' could not be loaded: {exc}", file=sys.stderr)
        return 2

    strategy = StrategyDocument.model_validate(record["strategy"])
    bot_race = RACE_CHOICES[RaceName(record["race"]).value]
    primary_ai = InstrumentedDeclarativeBot(
        strategy,
        accept_computer_surrender=args.opponent_bot is None,
        emit_progress=True,
    )
    primary_revision = record["selectedRevision"]

    opponent_record: dict[str, Any] | None = None
    opponent_ai: InstrumentedDeclarativeBot | None = None
    if args.opponent_bot:
        try:
            opponent_record = repository.get_bot_revision(
                args.opponent_bot, args.opponent_revision
            )
        except NotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if opponent_record["id"] == record["id"] and (
            opponent_record["selectedRevision"] == primary_revision
        ):
            print("Choose a different opponent bot or revision.", file=sys.stderr)
            return 2
        opponent_strategy = StrategyDocument.model_validate(opponent_record["strategy"])
        opponent_ai = InstrumentedDeclarativeBot(opponent_strategy)
        players = [
            Bot(bot_race, primary_ai, name=record["name"]),
            Bot(
                RACE_CHOICES[RaceName(opponent_record["race"]).value],
                opponent_ai,
                name=opponent_record["name"],
            ),
        ]
        opponent_label = (
            f"{opponent_record['name']} v{opponent_record['selectedRevision']}"
        )
        participants = [
            _participant(record, primary_revision),
            _participant(opponent_record, opponent_record["selectedRevision"]),
        ]
    else:
        enemy_race = RACE_CHOICES[args.enemy_race]
        difficulty = DIFFICULTY_CHOICES[args.difficulty]
        players = [
            Bot(bot_race, primary_ai, name=record["name"]),
            Computer(enemy_race, difficulty),
        ]
        opponent_label = f"{args.enemy_race} ({args.difficulty})"
        participants = [
            _participant(record, primary_revision),
            {
                "participant_type": "computer",
                "bot_id": None,
                "bot_revision": None,
                "name": f"{args.enemy_race.title()} Computer",
                "requested_race": args.enemy_race,
                "resolved_race": None,
                "difficulty": args.difficulty,
            },
        ]

    print(
        f"Starting {record['name']} v{primary_revision} on {args.map_name} "
        f"vs {opponent_label}",
        flush=True,
    )
    match_id = args.match_id
    owns_match = match_id is None
    if owns_match:
        match = repository.create_match(
            map_name=args.map_name,
            source="cli",
            participants=participants,
        )
        match_id = match["id"]
        repository.set_match_running(match_id)

    try:
        raw_result = run_game(
            selected_map,
            players,
            realtime=False,
            random_seed=args.random_seed,
        )
        if isinstance(raw_result, list):
            result_names = [_result_name(item) for item in raw_result]
        else:
            result_names = [_result_name(raw_result), _opposite(_result_name(raw_result))]
        stalemate = primary_ai.stalemate_detected or bool(
            opponent_ai and opponent_ai.stalemate_detected
        )
        if stalemate:
            result_names = ["tie", "tie"]
        elif primary_ai.accepted_opponent_surrender:
            result_names = ["victory", "defeat"]
        elif opponent_ai is not None and opponent_ai.accepted_opponent_surrender:
            result_names = ["defeat", "victory"]
        game_time = primary_ai.final_game_time
        if game_time is None and opponent_ai is not None:
            game_time = opponent_ai.final_game_time
        resolved_enemy = (
            primary_ai.resolved_enemy_race.name.lower()
            if primary_ai.resolved_enemy_race is not None
            else participants[1]["resolved_race"]
        )
        payload = {
            "matchId": match_id,
            "gameTimeSeconds": game_time,
            "stalemate": stalemate,
            "participants": [
                {
                    "slot": 1,
                    "result": result_names[0],
                    "resolvedRace": record["race"],
                },
                {
                    "slot": 2,
                    "result": result_names[1],
                    "resolvedRace": (
                        opponent_record["race"] if opponent_record else resolved_enemy
                    ),
                },
            ],
        }
        print(f"{RESULT_PREFIX}{json.dumps(payload, separators=(',', ':'))}", flush=True)
        if owns_match:
            repository.finalize_match(
                match_id,
                status="completed",
                return_code=0,
                game_time_seconds=game_time,
                participant_results=payload["participants"],
            )
        return 0
    except KeyboardInterrupt:
        if owns_match:
            repository.finalize_match(
                match_id,
                status="stopped",
                failure_reason="Stopped by user",
            )
        return 130
    except Exception as exc:
        print(f"Match failed: {exc}", file=sys.stderr, flush=True)
        if owns_match:
            repository.finalize_match(
                match_id,
                status="failed",
                return_code=1,
                failure_reason=str(exc),
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
