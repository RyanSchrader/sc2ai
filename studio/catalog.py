from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


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
    ENGINEERINGBAY = "ENGINEERINGBAY"
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


class UpgradeName(str, Enum):
    TERRAN_INFANTRY_WEAPONS_1 = "TERRANINFANTRYWEAPONSLEVEL1"
    WARPGATE = "WARPGATERESEARCH"
    ZERGLING_SPEED = "ZERGLINGMOVEMENTSPEED"


class UnitRole(str, Enum):
    WORKER = "worker"
    COMBAT = "combat"
    SUPPLY = "supply"


class StructureRole(str, Enum):
    TOWNHALL = "townhall"
    SUPPLY = "supply"
    GAS = "gas"
    PRODUCTION = "production"
    TECH = "tech"
    DEFENSE = "defense"
    FORWARD = "forward"


class ActionType(str, Enum):
    DISTRIBUTE_WORKERS = "distribute_workers"
    TRAIN_WORKERS = "train_workers"
    MAINTAIN_SUPPLY = "maintain_supply"
    BUILD_STRUCTURE = "build_structure"
    MAINTAIN_GAS = "maintain_gas"
    TRAIN_UNITS = "train_units"
    EXPAND = "expand"
    ATTACK = "attack"
    BUILD_FORWARD = "build_forward"
    EMERGENCY_WORKER_ATTACK = "emergency_worker_attack"
    SCOUT = "scout"
    DEFEND = "defend"
    RETREAT = "retreat"
    RESEARCH = "research"


class TargetLocation(str, Enum):
    MAIN = "main"
    MAP_CENTER = "map_center"
    ENEMY_START = "enemy_start"
    LEAST_SCOUTED_EXPANSION = "least_scouted_expansion"


@dataclass(frozen=True)
class UnitSpec:
    race: RaceName
    roles: frozenset[UnitRole]
    producer: StructureName | None
    tech_requirement: StructureName | None


@dataclass(frozen=True)
class StructureSpec:
    race: RaceName
    roles: frozenset[StructureRole]


@dataclass(frozen=True)
class UpgradeSpec:
    race: RaceName
    producer: StructureName


@dataclass(frozen=True)
class ActionFieldSpec:
    name: str
    kind: str
    required: bool = False
    unit_roles: frozenset[UnitRole] = frozenset()
    structure_roles: frozenset[StructureRole] = frozenset()
    targets: frozenset[TargetLocation] = frozenset()

    def public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "required": self.required,
        }
        if self.unit_roles:
            result["unitRoles"] = sorted(role.value for role in self.unit_roles)
        if self.structure_roles:
            result["structureRoles"] = sorted(role.value for role in self.structure_roles)
        if self.targets:
            result["targets"] = sorted(target.value for target in self.targets)
        return result


@dataclass(frozen=True)
class ActionSpec:
    label: str
    description: str
    handler: str
    fields: tuple[ActionFieldSpec, ...]
    defaults_by_race: dict[RaceName, dict[str, Any]]

    @property
    def required_fields(self) -> frozenset[str]:
        return frozenset(field.name for field in self.fields if field.required)

    @property
    def optional_fields(self) -> frozenset[str]:
        return frozenset(field.name for field in self.fields if not field.required)

    @property
    def allowed_fields(self) -> frozenset[str]:
        return self.required_fields | self.optional_fields

    def field(self, name: str) -> ActionFieldSpec | None:
        return next((field for field in self.fields if field.name == name), None)

    def public_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "description": self.description,
            "handler": self.handler,
            "requiredFields": sorted(self.required_fields),
            "optionalFields": sorted(self.optional_fields),
            "races": [race.value for race in self.defaults_by_race],
            "fields": [field.public_dict() for field in self.fields],
            "defaultsByRace": {
                race.value: _json_values(defaults)
                for race, defaults in self.defaults_by_race.items()
            },
        }


UNIT_SPECS: dict[UnitName, UnitSpec] = {
    UnitName.SCV: UnitSpec(
        RaceName.TERRAN,
        frozenset({UnitRole.WORKER}),
        StructureName.COMMANDCENTER,
        None,
    ),
    UnitName.MARINE: UnitSpec(
        RaceName.TERRAN,
        frozenset({UnitRole.COMBAT}),
        StructureName.BARRACKS,
        StructureName.BARRACKS,
    ),
    UnitName.PROBE: UnitSpec(
        RaceName.PROTOSS,
        frozenset({UnitRole.WORKER}),
        StructureName.NEXUS,
        None,
    ),
    UnitName.ZEALOT: UnitSpec(
        RaceName.PROTOSS,
        frozenset({UnitRole.COMBAT}),
        StructureName.GATEWAY,
        StructureName.GATEWAY,
    ),
    UnitName.STALKER: UnitSpec(
        RaceName.PROTOSS,
        frozenset({UnitRole.COMBAT}),
        StructureName.GATEWAY,
        StructureName.CYBERNETICSCORE,
    ),
    UnitName.DRONE: UnitSpec(
        RaceName.ZERG,
        frozenset({UnitRole.WORKER}),
        None,
        None,
    ),
    UnitName.OVERLORD: UnitSpec(
        RaceName.ZERG,
        frozenset({UnitRole.SUPPLY}),
        None,
        None,
    ),
    UnitName.ZERGLING: UnitSpec(
        RaceName.ZERG,
        frozenset({UnitRole.COMBAT}),
        None,
        StructureName.SPAWNINGPOOL,
    ),
    UnitName.ROACH: UnitSpec(
        RaceName.ZERG,
        frozenset({UnitRole.COMBAT}),
        None,
        StructureName.ROACHWARREN,
    ),
}


STRUCTURE_SPECS: dict[StructureName, StructureSpec] = {
    StructureName.COMMANDCENTER: StructureSpec(
        RaceName.TERRAN, frozenset({StructureRole.TOWNHALL})
    ),
    StructureName.SUPPLYDEPOT: StructureSpec(
        RaceName.TERRAN,
        frozenset({StructureRole.SUPPLY, StructureRole.FORWARD}),
    ),
    StructureName.BARRACKS: StructureSpec(
        RaceName.TERRAN,
        frozenset({StructureRole.PRODUCTION, StructureRole.FORWARD}),
    ),
    StructureName.REFINERY: StructureSpec(
        RaceName.TERRAN, frozenset({StructureRole.GAS})
    ),
    StructureName.BUNKER: StructureSpec(
        RaceName.TERRAN,
        frozenset({StructureRole.DEFENSE, StructureRole.FORWARD}),
    ),
    StructureName.ENGINEERINGBAY: StructureSpec(
        RaceName.TERRAN, frozenset({StructureRole.TECH})
    ),
    StructureName.NEXUS: StructureSpec(
        RaceName.PROTOSS, frozenset({StructureRole.TOWNHALL})
    ),
    StructureName.PYLON: StructureSpec(
        RaceName.PROTOSS,
        frozenset({StructureRole.SUPPLY, StructureRole.FORWARD}),
    ),
    StructureName.GATEWAY: StructureSpec(
        RaceName.PROTOSS,
        frozenset({StructureRole.PRODUCTION, StructureRole.FORWARD}),
    ),
    StructureName.CYBERNETICSCORE: StructureSpec(
        RaceName.PROTOSS, frozenset({StructureRole.TECH})
    ),
    StructureName.ASSIMILATOR: StructureSpec(
        RaceName.PROTOSS, frozenset({StructureRole.GAS})
    ),
    StructureName.PHOTONCANNON: StructureSpec(
        RaceName.PROTOSS,
        frozenset({StructureRole.DEFENSE, StructureRole.FORWARD}),
    ),
    StructureName.HATCHERY: StructureSpec(
        RaceName.ZERG, frozenset({StructureRole.TOWNHALL})
    ),
    StructureName.SPAWNINGPOOL: StructureSpec(
        RaceName.ZERG, frozenset({StructureRole.TECH})
    ),
    StructureName.EXTRACTOR: StructureSpec(
        RaceName.ZERG, frozenset({StructureRole.GAS})
    ),
    StructureName.ROACHWARREN: StructureSpec(
        RaceName.ZERG, frozenset({StructureRole.TECH})
    ),
}


UPGRADE_SPECS: dict[UpgradeName, UpgradeSpec] = {
    UpgradeName.TERRAN_INFANTRY_WEAPONS_1: UpgradeSpec(
        RaceName.TERRAN,
        StructureName.ENGINEERINGBAY,
    ),
    UpgradeName.WARPGATE: UpgradeSpec(
        RaceName.PROTOSS,
        StructureName.CYBERNETICSCORE,
    ),
    UpgradeName.ZERGLING_SPEED: UpgradeSpec(
        RaceName.ZERG,
        StructureName.SPAWNINGPOOL,
    ),
}


RACE_UNITS: dict[RaceName, set[UnitName]] = {
    race: {unit for unit, spec in UNIT_SPECS.items() if spec.race == race}
    for race in RaceName
}

RACE_STRUCTURES: dict[RaceName, set[StructureName]] = {
    race: {
        structure
        for structure, spec in STRUCTURE_SPECS.items()
        if spec.race == race
    }
    for race in RaceName
}

RACE_UPGRADES: dict[RaceName, set[UpgradeName]] = {
    race: {
        upgrade
        for upgrade, spec in UPGRADE_SPECS.items()
        if spec.race == race
    }
    for race in RaceName
}


WORKER_BY_RACE = {
    RaceName.TERRAN: UnitName.SCV,
    RaceName.PROTOSS: UnitName.PROBE,
    RaceName.ZERG: UnitName.DRONE,
}

COMBAT_UNIT_BY_RACE = {
    RaceName.TERRAN: UnitName.MARINE,
    RaceName.PROTOSS: UnitName.ZEALOT,
    RaceName.ZERG: UnitName.ZERGLING,
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

DEFAULT_STRUCTURE_BY_RACE = {
    RaceName.TERRAN: StructureName.BARRACKS,
    RaceName.PROTOSS: StructureName.GATEWAY,
    RaceName.ZERG: StructureName.SPAWNINGPOOL,
}

DEFAULT_FORWARD_STRUCTURE_BY_RACE = {
    RaceName.TERRAN: StructureName.SUPPLYDEPOT,
    RaceName.PROTOSS: StructureName.PYLON,
}

DEFAULT_UPGRADE_BY_RACE = {
    RaceName.TERRAN: UpgradeName.TERRAN_INFANTRY_WEAPONS_1,
    RaceName.PROTOSS: UpgradeName.WARPGATE,
    RaceName.ZERG: UpgradeName.ZERGLING_SPEED,
}

PRODUCER_BY_UNIT: dict[UnitName, StructureName | None] = {
    unit: spec.producer for unit, spec in UNIT_SPECS.items()
}

TECH_REQUIREMENT_BY_UNIT: dict[UnitName, StructureName | None] = {
    unit: spec.tech_requirement for unit, spec in UNIT_SPECS.items()
}

PRODUCER_BY_UPGRADE: dict[UpgradeName, StructureName] = {
    upgrade: spec.producer for upgrade, spec in UPGRADE_SPECS.items()
}


def _field(
    name: str,
    kind: str,
    *,
    required: bool = False,
    unit_roles: set[UnitRole] | None = None,
    structure_roles: set[StructureRole] | None = None,
    targets: set[TargetLocation] | None = None,
) -> ActionFieldSpec:
    return ActionFieldSpec(
        name=name,
        kind=kind,
        required=required,
        unit_roles=frozenset(unit_roles or set()),
        structure_roles=frozenset(structure_roles or set()),
        targets=frozenset(targets or set()),
    )


_ALL_RACES = tuple(RaceName)
_COMBAT = {UnitRole.COMBAT}
_BUILDABLE = {
    StructureRole.SUPPLY,
    StructureRole.PRODUCTION,
    StructureRole.TECH,
    StructureRole.DEFENSE,
}


def _for_all_races(factory) -> dict[RaceName, dict[str, Any]]:
    return {race: factory(race) for race in _ALL_RACES}


ACTION_SPECS: dict[ActionType, ActionSpec] = {
    ActionType.DISTRIBUTE_WORKERS: ActionSpec(
        "Distribute workers",
        "Balance workers across mineral and gas resources.",
        "_distribute_workers",
        (),
        _for_all_races(lambda _race: {"type": ActionType.DISTRIBUTE_WORKERS}),
    ),
    ActionType.TRAIN_WORKERS: ActionSpec(
        "Train workers",
        "Maintain a target number of workers, including workers in production.",
        "_train_workers",
        (_field("amount", "integer", required=True),),
        _for_all_races(
            lambda _race: {"type": ActionType.TRAIN_WORKERS, "amount": 50}
        ),
    ),
    ActionType.MAINTAIN_SUPPLY: ActionSpec(
        "Maintain supply",
        "Create the race's supply provider before the configured free-supply buffer is exhausted.",
        "_maintain_supply",
        (_field("buffer", "integer", required=True),),
        _for_all_races(
            lambda _race: {
                "type": ActionType.MAINTAIN_SUPPLY,
                "buffer": 5,
            }
        ),
    ),
    ActionType.BUILD_STRUCTURE: ActionSpec(
        "Build structure",
        "Maintain a total count of a standard, production, tech, supply, or defensive structure.",
        "_build_structure",
        (
            _field(
                "structure",
                "structure",
                required=True,
                structure_roles=_BUILDABLE,
            ),
            _field("amount", "integer", required=True),
            _field("distance", "number"),
        ),
        _for_all_races(
            lambda race: {
                "type": ActionType.BUILD_STRUCTURE,
                "structure": DEFAULT_STRUCTURE_BY_RACE[race],
                "amount": 1,
                "distance": 7,
            }
        ),
    ),
    ActionType.MAINTAIN_GAS: ActionSpec(
        "Maintain gas",
        "Maintain a target number of the race's gas-extraction structure.",
        "_maintain_gas",
        (
            _field(
                "structure",
                "structure",
                required=True,
                structure_roles={StructureRole.GAS},
            ),
            _field("amount", "integer", required=True),
        ),
        _for_all_races(
            lambda race: {
                "type": ActionType.MAINTAIN_GAS,
                "structure": GAS_BY_RACE[race],
                "amount": 1,
            }
        ),
    ),
    ActionType.TRAIN_UNITS: ActionSpec(
        "Train units",
        "Train the first affordable combat unit from a primary and optional fallback list.",
        "_train_units",
        (
            _field("unit", "unit", unit_roles=_COMBAT),
            _field("units", "unit_list", unit_roles=_COMBAT),
            _field("fallback_units", "unit_list", unit_roles=_COMBAT),
        ),
        _for_all_races(
            lambda race: {
                "type": ActionType.TRAIN_UNITS,
                "unit": COMBAT_UNIT_BY_RACE[race],
            }
        ),
    ),
    ActionType.EXPAND: ActionSpec(
        "Expand",
        "Maintain a target number of race-appropriate town halls at expansion locations.",
        "_expand",
        (
            _field(
                "structure",
                "structure",
                required=True,
                structure_roles={StructureRole.TOWNHALL},
            ),
            _field("amount", "integer", required=True),
        ),
        _for_all_races(
            lambda race: {
                "type": ActionType.EXPAND,
                "structure": TOWNHALL_BY_RACE[race],
                "amount": 2,
            }
        ),
    ),
    ActionType.ATTACK: ActionSpec(
        "Attack",
        "Attack visible enemies, then search unvisited expansions, with a minimum army size.",
        "_attack",
        (
            _field("units", "unit_list", required=True, unit_roles=_COMBAT),
            _field("min_size", "integer", required=True),
            _field("required_unit", "unit", unit_roles=_COMBAT),
            _field("required_amount", "integer"),
        ),
        _for_all_races(
            lambda race: {
                "type": ActionType.ATTACK,
                "units": [COMBAT_UNIT_BY_RACE[race]],
                "min_size": 10,
            }
        ),
    ),
    ActionType.BUILD_FORWARD: ActionSpec(
        "Build forward",
        "Maintain a target count of supported structures near the enemy side of the map.",
        "_build_forward",
        (
            _field(
                "structure",
                "structure",
                required=True,
                structure_roles={StructureRole.FORWARD},
            ),
            _field("amount", "integer", required=True),
            _field("distance", "number"),
        ),
        {
            race: {
                "type": ActionType.BUILD_FORWARD,
                "structure": structure,
                "amount": 1,
                "distance": 8,
            }
            for race, structure in DEFAULT_FORWARD_STRUCTURE_BY_RACE.items()
        },
    ),
    ActionType.EMERGENCY_WORKER_ATTACK: ActionSpec(
        "Emergency worker attack",
        "Send all remaining workers to fight after every town hall is lost.",
        "_emergency_worker_attack",
        (),
        _for_all_races(
            lambda _race: {"type": ActionType.EMERGENCY_WORKER_ATTACK}
        ),
    ),
    ActionType.SCOUT: ActionSpec(
        "Scout",
        "Send one selected unit toward a concrete scouting destination.",
        "_scout",
        (
            _field("unit", "unit", required=True),
            _field(
                "target",
                "target",
                targets={
                    TargetLocation.ENEMY_START,
                    TargetLocation.MAP_CENTER,
                    TargetLocation.LEAST_SCOUTED_EXPANSION,
                },
            ),
        ),
        _for_all_races(
            lambda race: {
                "type": ActionType.SCOUT,
                "unit": WORKER_BY_RACE[race],
                "target": TargetLocation.ENEMY_START,
            }
        ),
    ),
    ActionType.DEFEND: ActionSpec(
        "Defend",
        "Attack threats near a defensive anchor, or regroup idle defenders there.",
        "_defend",
        (
            _field("units", "unit_list", required=True, unit_roles=_COMBAT),
            _field("min_size", "integer", required=True),
            _field(
                "target",
                "target",
                targets={TargetLocation.MAIN, TargetLocation.MAP_CENTER},
            ),
            _field("distance", "number"),
        ),
        _for_all_races(
            lambda race: {
                "type": ActionType.DEFEND,
                "units": [COMBAT_UNIT_BY_RACE[race]],
                "min_size": 1,
                "target": TargetLocation.MAIN,
                "distance": 30,
            }
        ),
    ),
    ActionType.RETREAT: ActionSpec(
        "Retreat wounded units",
        "Move selected combat units below a health threshold to a safe anchor.",
        "_retreat",
        (
            _field("units", "unit_list", required=True, unit_roles=_COMBAT),
            _field("health_threshold", "ratio", required=True),
            _field(
                "target",
                "target",
                targets={TargetLocation.MAIN, TargetLocation.MAP_CENTER},
            ),
        ),
        _for_all_races(
            lambda race: {
                "type": ActionType.RETREAT,
                "units": [COMBAT_UNIT_BY_RACE[race]],
                "health_threshold": 0.35,
                "target": TargetLocation.MAIN,
            }
        ),
    ),
    ActionType.RESEARCH: ActionSpec(
        "Research upgrade",
        "Research a supported race-specific upgrade from its eligible producer.",
        "_research",
        (_field("upgrade", "upgrade", required=True),),
        _for_all_races(
            lambda race: {
                "type": ActionType.RESEARCH,
                "upgrade": DEFAULT_UPGRADE_BY_RACE[race],
            }
        ),
    ),
}


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _json_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_value(value) for key, value in values.items()}


def public_catalog() -> dict[str, object]:
    return {
        "schemaVersions": [1],
        "races": [value.value for value in RaceName],
        "units": {
            race.value: sorted(unit.value for unit in units)
            for race, units in RACE_UNITS.items()
        },
        "structures": {
            race.value: sorted(structure.value for structure in structures)
            for race, structures in RACE_STRUCTURES.items()
        },
        "upgrades": {
            race.value: sorted(upgrade.value for upgrade in upgrades)
            for race, upgrades in RACE_UPGRADES.items()
        },
        "unitMetadata": {
            unit.value: {
                "race": spec.race.value,
                "roles": sorted(role.value for role in spec.roles),
                "producer": spec.producer.value if spec.producer else None,
                "techRequirement": (
                    spec.tech_requirement.value if spec.tech_requirement else None
                ),
            }
            for unit, spec in UNIT_SPECS.items()
        },
        "structureMetadata": {
            structure.value: {
                "race": spec.race.value,
                "roles": sorted(role.value for role in spec.roles),
            }
            for structure, spec in STRUCTURE_SPECS.items()
        },
        "upgradeMetadata": {
            upgrade.value: {
                "race": spec.race.value,
                "producer": spec.producer.value,
            }
            for upgrade, spec in UPGRADE_SPECS.items()
        },
        "entityDefaults": {
            race.value: {
                "worker": WORKER_BY_RACE[race].value,
                "combatUnit": COMBAT_UNIT_BY_RACE[race].value,
                "townhall": TOWNHALL_BY_RACE[race].value,
                "gas": GAS_BY_RACE[race].value,
                "supply": SUPPLY_BY_RACE[race].value,
                "upgrade": DEFAULT_UPGRADE_BY_RACE[race].value,
            }
            for race in RaceName
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
        "actionTypes": [action.value for action in ACTION_SPECS],
        "actionSpecs": {
            action.value: spec.public_dict() for action, spec in ACTION_SPECS.items()
        },
        "executionPolicies": ["continuous", "once", "cooldown"],
        "targets": [target.value for target in TargetLocation],
        # Kept as an empty compatibility surface for older frontend clients. New
        # action contracts enumerate operational targets per action.
    }
