import argparse
from typing import Callable

from sc2 import maps
from sc2.data import Difficulty, Race
from sc2.main import run_game
from sc2.player import Bot, Computer

from bot import (
    ProtossBasicBot,
    ProtossIntermediateBot,
    ProtossTowerRushBot,
    TerranBasicBot,
    TerranBunkerRushBot,
    ZergBasicRoachBot,
    ZergBasicSwarmBot,
)

MAP_CANDIDATES = [
    "Simple64",
    "AbyssalReefAIE",
    "AbyssalReefLE",
    "AcropolisLE",
]

BOT_CHOICES: dict[str, tuple[Race, Callable[[], object]]] = {
    "terran-basic": (Race.Terran, TerranBasicBot),
    "terran-bunker-rush": (Race.Terran, TerranBunkerRushBot),
    "protoss-basic": (Race.Protoss, ProtossBasicBot),
    "protoss-intermediate": (Race.Protoss, ProtossIntermediateBot),
    "zerg-basic-1": (Race.Zerg, ZergBasicSwarmBot),
    "zerg-basic-2": (Race.Zerg, ZergBasicRoachBot),
    "protoss-tower-rush": (Race.Protoss, ProtossTowerRushBot),
}

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
    parser = argparse.ArgumentParser(description="Run one of the custom SC2 bots.")
    parser.add_argument(
        "--bot",
        choices=BOT_CHOICES.keys(),
        default="protoss-basic",
        help="Bot implementation to run.",
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
        "--map",
        dest="map_name",
        default=None,
        help="Optional map name. If omitted, uses the first available map from MAP_CANDIDATES.",
    )
    return parser.parse_args()


def choose_map(map_name: str | None):
    if map_name:
        return maps.get(map_name)

    for candidate in MAP_CANDIDATES:
        try:
            return maps.get(candidate)
        except Exception:
            continue
    names = ", ".join(MAP_CANDIDATES)
    raise RuntimeError(
        f"No candidate maps were found. Install one of these maps or update MAP_CANDIDATES: {names}"
    )


if __name__ == "__main__":
    args = parse_args()
    selected_map = choose_map(args.map_name)

    bot_race, bot_factory = BOT_CHOICES[args.bot]
    enemy_race = RACE_CHOICES[args.enemy_race]
    difficulty = DIFFICULTY_CHOICES[args.difficulty]

    run_game(
        selected_map,
        [
            Bot(bot_race, bot_factory()),
            Computer(enemy_race, difficulty),
        ],
        realtime=False,
    )
