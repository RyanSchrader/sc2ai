from __future__ import annotations

from enum import Enum


class RaceName(str, Enum):
    TERRAN = "terran"
    PROTOSS = "protoss"
    ZERG = "zerg"


class UnitName(str, Enum):
    SCV = "SCV"
    MARINE = "MARINE"
    PROBE = "PROBE"
    ZEALOT = "ZEALOT"
    STALKER = "STALKER"
    DRONE = "DRONE"
    OVERLORD = "OVERLORD"
    ZERGLING = "ZERGLING"
    ROACH = "ROACH"


class StructureName(str, Enum):
    COMMANDCENTER = "COMMANDCENTER"
    SUPPLYDEPOT = "SUPPLYDEPOT"
    BARRACKS = "BARRACKS"
    REFINERY = "REFINERY"
    BUNKER = "BUNKER"
    NEXUS = "NEXUS"
    PYLON = "PYLON"
    GATEWAY = "GATEWAY"
    CYBERNETICSCORE = "CYBERNETICSCORE"
    ASSIMILATOR = "ASSIMILATOR"
    PHOTONCANNON = "PHOTONCANNON"
    HATCHERY = "HATCHERY"
    SPAWNINGPOOL = "SPAWNINGPOOL"
    EXTRACTOR = "EXTRACTOR"
    ROACHWARREN = "ROACHWARREN"


RACE_UNITS: dict[RaceName, set[UnitName]] = {
    RaceName.TERRAN: {UnitName.SCV, UnitName.MARINE},
    RaceName.PROTOSS: {UnitName.PROBE, UnitName.ZEALOT, UnitName.STALKER},
    RaceName.ZERG: {
        UnitName.DRONE,
        UnitName.OVERLORD,
        UnitName.ZERGLING,
        UnitName.ROACH,
    },
}

RACE_STRUCTURES: dict[RaceName, set[StructureName]] = {
    RaceName.TERRAN: {
        StructureName.COMMANDCENTER,
        StructureName.SUPPLYDEPOT,
        StructureName.BARRACKS,
        StructureName.REFINERY,
        StructureName.BUNKER,
    },
    RaceName.PROTOSS: {
        StructureName.NEXUS,
        StructureName.PYLON,
        StructureName.GATEWAY,
        StructureName.CYBERNETICSCORE,
        StructureName.ASSIMILATOR,
        StructureName.PHOTONCANNON,
    },
    RaceName.ZERG: {
        StructureName.HATCHERY,
        StructureName.SPAWNINGPOOL,
        StructureName.EXTRACTOR,
        StructureName.ROACHWARREN,
    },
}

WORKER_BY_RACE = {
    RaceName.TERRAN: UnitName.SCV,
    RaceName.PROTOSS: UnitName.PROBE,
    RaceName.ZERG: UnitName.DRONE,
}

TOWNHALL_BY_RACE = {
    RaceName.TERRAN: StructureName.COMMANDCENTER,
    RaceName.PROTOSS: StructureName.NEXUS,
    RaceName.ZERG: StructureName.HATCHERY,
}

SUPPLY_BY_RACE: dict[RaceName, UnitName | StructureName] = {
    RaceName.TERRAN: StructureName.SUPPLYDEPOT,
    RaceName.PROTOSS: StructureName.PYLON,
    RaceName.ZERG: UnitName.OVERLORD,
}

GAS_BY_RACE = {
    RaceName.TERRAN: StructureName.REFINERY,
    RaceName.PROTOSS: StructureName.ASSIMILATOR,
    RaceName.ZERG: StructureName.EXTRACTOR,
}

PRODUCER_BY_UNIT: dict[UnitName, StructureName | None] = {
    UnitName.SCV: StructureName.COMMANDCENTER,
    UnitName.MARINE: StructureName.BARRACKS,
    UnitName.PROBE: StructureName.NEXUS,
    UnitName.ZEALOT: StructureName.GATEWAY,
    UnitName.STALKER: StructureName.GATEWAY,
    UnitName.DRONE: None,
    UnitName.OVERLORD: None,
    UnitName.ZERGLING: None,
    UnitName.ROACH: None,
}


def public_catalog() -> dict[str, object]:
    return {
        "races": [value.value for value in RaceName],
        "units": {
            race.value: sorted(unit.value for unit in units)
            for race, units in RACE_UNITS.items()
        },
        "structures": {
            race.value: sorted(structure.value for structure in structures)
            for race, structures in RACE_STRUCTURES.items()
        },
        "conditionKinds": ["always", "all", "any", "not", "metric"],
        "metrics": [
            "game_time",
            "supply_used",
            "supply_left",
            "workers",
            "minerals",
            "vespene",
            "bases",
            "unit_count",
            "structure_count",
            "enemy_unit_count",
        ],
        "comparators": ["lt", "lte", "eq", "gte", "gt"],
        "actionTypes": [
            "distribute_workers",
            "train_workers",
            "maintain_supply",
            "build_structure",
            "maintain_gas",
            "train_units",
            "expand",
            "attack",
            "build_forward",
            "emergency_worker_attack",
        ],
        "executionPolicies": ["continuous", "once", "cooldown"],
        "placements": ["main", "enemy", "map_center"],
    }
