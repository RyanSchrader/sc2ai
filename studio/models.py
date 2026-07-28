from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    model_serializer,
    model_validator,
)

from .catalog import (
    ACTION_SPECS,
    PRODUCER_BY_UPGRADE,
    RACE_STRUCTURES,
    RACE_UNITS,
    RACE_UPGRADES,
    STRUCTURE_SPECS,
    TECH_REQUIREMENT_BY_UNIT,
    UNIT_SPECS,
    ActionType,
    RaceName,
    StructureName,
    TargetLocation,
    TOWNHALL_BY_RACE,
    UnitName,
    UpgradeName,
)


class StrategyModel(BaseModel):
    """Strict base for persisted strategy-language objects."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Comparator(str, Enum):
    LT = "lt"
    LTE = "lte"
    EQ = "eq"
    GTE = "gte"
    GT = "gt"


class MetricName(str, Enum):
    GAME_TIME = "game_time"
    SUPPLY_USED = "supply_used"
    SUPPLY_LEFT = "supply_left"
    WORKERS = "workers"
    MINERALS = "minerals"
    VESPENE = "vespene"
    BASES = "bases"
    UNIT_COUNT = "unit_count"
    STRUCTURE_COUNT = "structure_count"
    ENEMY_UNIT_COUNT = "enemy_unit_count"


class ConditionKind(str, Enum):
    ALWAYS = "always"
    ALL = "all"
    ANY = "any"
    NOT = "not"
    METRIC = "metric"


_LEGACY_CONDITION_FIELDS = frozenset(
    {"kind", "metric", "comparator", "value", "subject", "status", "children"}
)
_LEGACY_CONDITION_DEFAULTS: dict[str, Any] = {
    "metric": None,
    "comparator": "gte",
    "value": 0,
    "subject": None,
    "status": "total",
    "children": [],
}


class Condition(StrategyModel):
    kind: ConditionKind = ConditionKind.ALWAYS
    metric: MetricName | None = None
    comparator: Comparator = Comparator.GTE
    value: float = 0
    subject: str | None = None
    status: str = "total"
    children: list["Condition"] = Field(default_factory=list, max_length=20)

    @staticmethod
    def _allowed_fields(
        kind: ConditionKind,
        metric: MetricName | None,
    ) -> frozenset[str]:
        if kind == ConditionKind.ALWAYS:
            return frozenset({"kind"})
        if kind in {ConditionKind.ALL, ConditionKind.ANY, ConditionKind.NOT}:
            return frozenset({"kind", "children"})
        allowed = {"kind", "metric", "comparator", "value"}
        if metric in {
            MetricName.UNIT_COUNT,
            MetricName.STRUCTURE_COUNT,
            MetricName.ENEMY_UNIT_COUNT,
        }:
            allowed.add("subject")
        if metric == MetricName.STRUCTURE_COUNT:
            allowed.add("status")
        return frozenset(allowed)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_shape(cls, value: Any) -> Any:
        """Read the exact expanded shape emitted by pre-contract releases.

        New partial payloads do not receive this compatibility treatment: an
        irrelevant field is rejected even when its value looks harmless.
        """
        if not isinstance(value, dict) or frozenset(value) != _LEGACY_CONDITION_FIELDS:
            return value
        try:
            kind = ConditionKind(value.get("kind", ConditionKind.ALWAYS))
            metric = (
                MetricName(value["metric"])
                if value.get("metric") is not None
                else None
            )
        except ValueError:
            return value
        allowed = cls._allowed_fields(kind, metric)
        for field in _LEGACY_CONDITION_FIELDS - allowed:
            if value[field] != _LEGACY_CONDITION_DEFAULTS[field]:
                raise ValueError(
                    f"Legacy condition field {field!r} has a non-default value "
                    f"and cannot be ignored for {kind.value}."
                )
        return {field: item for field, item in value.items() if field in allowed}

    @model_validator(mode="after")
    def validate_shape(self) -> "Condition":
        allowed = self._allowed_fields(self.kind, self.metric)
        unexpected = self.model_fields_set - allowed
        if unexpected:
            fields = ", ".join(sorted(unexpected))
            raise ValueError(
                f"{self.kind.value} conditions do not support fields: {fields}."
            )
        if self.kind == ConditionKind.METRIC and self.metric is None:
            raise ValueError("Metric conditions require a metric.")
        if self.kind == ConditionKind.NOT and len(self.children) != 1:
            raise ValueError("NOT conditions require exactly one child.")
        if self.kind in {ConditionKind.ALL, ConditionKind.ANY} and not self.children:
            raise ValueError(f"{self.kind.value} conditions require at least one child.")
        if self.metric in {
            MetricName.UNIT_COUNT,
            MetricName.STRUCTURE_COUNT,
            MetricName.ENEMY_UNIT_COUNT,
        } and not self.subject:
            raise ValueError(f"{self.metric.value} conditions require a subject.")
        if self.subject and self.metric in {MetricName.UNIT_COUNT, MetricName.ENEMY_UNIT_COUNT}:
            try:
                UnitName(self.subject)
            except ValueError as exc:
                raise ValueError(f"Unsupported unit subject: {self.subject}") from exc
        if self.subject and self.metric == MetricName.STRUCTURE_COUNT:
            try:
                StructureName(self.subject)
            except ValueError as exc:
                raise ValueError(f"Unsupported structure subject: {self.subject}") from exc
        if self.status not in {"total", "ready", "pending"}:
            raise ValueError("Condition status must be total, ready, or pending.")
        return self

    @model_serializer(mode="plain")
    def serialize_contract(self, info: SerializationInfo) -> dict[str, Any]:
        allowed = self._allowed_fields(self.kind, self.metric)
        values: dict[str, Any] = {
            "kind": self.kind,
            "metric": self.metric,
            "comparator": self.comparator,
            "value": self.value,
            "subject": self.subject,
            "status": self.status,
            "children": self.children,
        }
        result = {
            field: value
            for field, value in values.items()
            if field in allowed and value is not None
        }
        if info.mode == "json":
            for field in ("kind", "metric", "comparator"):
                if isinstance(result.get(field), Enum):
                    result[field] = result[field].value
        return result


_LEGACY_ACTION_FIELDS = frozenset(
    {
        "type",
        "unit",
        "units",
        "fallback_units",
        "structure",
        "amount",
        "buffer",
        "distance",
        "placement",
        "min_size",
        "required_unit",
        "required_amount",
        "target",
    }
)
_LEGACY_ACTION_DEFAULTS: dict[str, Any] = {
    "unit": None,
    "units": [],
    "fallback_units": [],
    "structure": None,
    "amount": None,
    "buffer": None,
    "distance": 7,
    "placement": "main",
    "min_size": None,
    "required_unit": None,
    "required_amount": None,
    "target": "enemy_start",
}


class StrategyAction(StrategyModel):
    type: ActionType
    unit: UnitName | None = None
    units: list[UnitName] = Field(default_factory=list, max_length=20)
    fallback_units: list[UnitName] = Field(default_factory=list, max_length=20)
    structure: StructureName | None = None
    upgrade: UpgradeName | None = None
    amount: int | None = Field(default=None, ge=1, le=200)
    buffer: int | None = Field(default=None, ge=1, le=200)
    distance: float | None = Field(default=None, ge=0, le=200)
    min_size: int | None = Field(default=None, ge=1, le=200)
    required_unit: UnitName | None = None
    required_amount: int | None = Field(default=None, ge=1, le=200)
    target: TargetLocation | None = None
    health_threshold: float | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict) or frozenset(value) != _LEGACY_ACTION_FIELDS:
            return value
        try:
            action_type = ActionType(value.get("type"))
        except ValueError:
            return value
        allowed = ACTION_SPECS[action_type].allowed_fields | {"type"}
        for field in _LEGACY_ACTION_FIELDS - allowed:
            if value[field] != _LEGACY_ACTION_DEFAULTS[field]:
                raise ValueError(
                    f"Legacy action field {field!r} has a non-default value "
                    f"and cannot be ignored for {action_type.value}."
                )
        return {field: item for field, item in value.items() if field in allowed}

    @model_validator(mode="after")
    def validate_action(self) -> "StrategyAction":
        spec = ACTION_SPECS[self.type]
        unexpected = self.model_fields_set - spec.allowed_fields - {"type"}
        if unexpected:
            fields = ", ".join(sorted(unexpected))
            raise ValueError(f"{self.type.value} does not support fields: {fields}.")
        missing: list[str] = []
        for field in sorted(spec.required_fields):
            value = getattr(self, field)
            if value is None or value == "" or (
                isinstance(value, list) and not value
            ):
                missing.append(field)
        if missing:
            raise ValueError(
                f"{self.type.value} requires fields: {', '.join(missing)}."
            )
        if len(self.units) != len(set(self.units)):
            raise ValueError("units cannot contain duplicates.")
        if len(self.fallback_units) != len(set(self.fallback_units)):
            raise ValueError("fallback_units cannot contain duplicates.")
        if self.type == ActionType.TRAIN_UNITS:
            if bool(self.unit) == bool(self.units):
                raise ValueError("train_units requires exactly one of unit or units.")
            primary = ({self.unit} if self.unit else set(self.units))
            if primary.intersection(self.fallback_units):
                raise ValueError("fallback_units cannot repeat a primary unit.")
        if bool(self.required_unit) != bool(self.required_amount):
            raise ValueError(
                "required_unit and required_amount must be provided together."
            )
        if (
            self.type == ActionType.ATTACK
            and self.required_unit
            and self.required_unit not in self.units
        ):
            raise ValueError("required_unit must also appear in attack units.")
        for field in spec.fields:
            value = getattr(self, field.name)
            values = value if isinstance(value, list) else [value]
            if field.unit_roles:
                for unit in (item for item in values if item is not None):
                    if UNIT_SPECS[unit].roles.isdisjoint(field.unit_roles):
                        roles = ", ".join(sorted(role.value for role in field.unit_roles))
                        raise ValueError(
                            f"{field.name} only accepts units with roles: {roles}."
                        )
            if field.structure_roles and value is not None:
                if STRUCTURE_SPECS[value].roles.isdisjoint(field.structure_roles):
                    roles = ", ".join(
                        sorted(role.value for role in field.structure_roles)
                    )
                    raise ValueError(
                        f"{field.name} only accepts structures with roles: {roles}."
                    )
            if field.targets and value is not None and value not in field.targets:
                targets = ", ".join(sorted(target.value for target in field.targets))
                raise ValueError(
                    f"{self.type.value} target must be one of: {targets}."
                )
        return self

    @model_serializer(mode="plain")
    def serialize_contract(self, info: SerializationInfo) -> dict[str, Any]:
        spec = ACTION_SPECS[self.type]
        values = {
            "type": self.type,
            **{field.name: getattr(self, field.name) for field in spec.fields},
        }
        result = {
            field: value
            for field, value in values.items()
            if value is not None and value != []
        }
        if info.mode == "json":
            for field, value in list(result.items()):
                if isinstance(value, Enum):
                    result[field] = value.value
                elif isinstance(value, list):
                    result[field] = [
                        item.value if isinstance(item, Enum) else item for item in value
                    ]
        return result


class ExecutionPolicy(str, Enum):
    CONTINUOUS = "continuous"
    ONCE = "once"
    COOLDOWN = "cooldown"


_LEGACY_RULE_FIELDS = frozenset(
    {
        "id",
        "name",
        "enabled",
        "priority",
        "execution",
        "cooldown_seconds",
        "trigger",
        "actions",
    }
)


class StrategyRule(StrategyModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    priority: int = 100
    execution: ExecutionPolicy = ExecutionPolicy.CONTINUOUS
    cooldown_seconds: float | None = Field(default=None, ge=0)
    trigger: Condition = Field(default_factory=Condition)
    actions: list[StrategyAction] = Field(min_length=1, max_length=20)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict) or frozenset(value) != _LEGACY_RULE_FIELDS:
            return value
        execution = ExecutionPolicy(
            value.get("execution", ExecutionPolicy.CONTINUOUS)
        )
        if execution != ExecutionPolicy.COOLDOWN:
            if value["cooldown_seconds"] != 1 and value["cooldown_seconds"] != 1.0:
                raise ValueError(
                    "Legacy cooldown_seconds has a non-default value and cannot "
                    f"be ignored for {execution.value}."
                )
            return {
                field: item
                for field, item in value.items()
                if field != "cooldown_seconds"
            }
        return value

    @model_validator(mode="after")
    def validate_execution(self) -> "StrategyRule":
        if self.execution == ExecutionPolicy.COOLDOWN:
            if self.cooldown_seconds is None or self.cooldown_seconds <= 0:
                raise ValueError(
                    "Cooldown rules require cooldown_seconds greater than zero."
                )
        elif "cooldown_seconds" in self.model_fields_set:
            raise ValueError(
                f"{self.execution.value} rules do not support cooldown_seconds."
            )
        return self

    @model_serializer(mode="plain")
    def serialize_contract(self, info: SerializationInfo) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            "execution": self.execution,
            "trigger": self.trigger,
            "actions": self.actions,
        }
        if self.execution == ExecutionPolicy.COOLDOWN:
            result["cooldown_seconds"] = self.cooldown_seconds
        if info.mode == "json":
            result["execution"] = self.execution.value
        return result


class StrategyPhase(StrategyModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    order: int = 0
    activation: Condition = Field(default_factory=Condition)
    rules: list[StrategyRule] = Field(default_factory=list, max_length=100)


_LEGACY_SETTINGS_FIELDS = frozenset(
    {
        "max_supply",
        "attack_target",
        "stalemate_detection",
        "stalemate_grace_period_seconds",
        "stalemate_timeout_seconds",
    }
)
_LEGACY_MINIMAL_SETTINGS_FIELDS = frozenset({"max_supply", "attack_target"})


class GlobalSettings(StrategyModel):
    max_supply: int = Field(default=200, ge=1, le=200)
    stalemate_detection: bool = True
    stalemate_grace_period_seconds: int = Field(default=600, ge=0, le=7200)
    stalemate_timeout_seconds: int = Field(default=180, ge=60, le=1800)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict) or frozenset(value) not in {
            _LEGACY_SETTINGS_FIELDS,
            _LEGACY_MINIMAL_SETTINGS_FIELDS,
        }:
            return value
        if value["attack_target"] != "enemy_start":
            raise ValueError(
                "Legacy attack_target has a non-default value and cannot be ignored."
            )
        return {
            field: item
            for field, item in value.items()
            if field != "attack_target"
        }


class StrategyDocument(StrategyModel):
    schema_version: Literal[1] = 1
    race: RaceName
    opening_chat: str | None = Field(default=None, max_length=500)
    settings: GlobalSettings = Field(default_factory=GlobalSettings)
    phases: list[StrategyPhase] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_race_catalog(self) -> "StrategyDocument":
        allowed_units = RACE_UNITS[self.race]
        allowed_structures = RACE_STRUCTURES[self.race]
        errors: list[str] = []
        seen_ids: set[str] = set()

        def validate_condition(condition: Condition, depth: int = 1) -> None:
            if depth > 8:
                errors.append("Conditions cannot be nested more than 8 levels deep.")
                return
            if condition.metric == MetricName.UNIT_COUNT and condition.subject:
                if UnitName(condition.subject) not in allowed_units:
                    errors.append(
                        f"{condition.subject} unit trigger does not belong to {self.race.value}."
                    )
            if condition.metric == MetricName.STRUCTURE_COUNT and condition.subject:
                if StructureName(condition.subject) not in allowed_structures:
                    errors.append(
                        f"{condition.subject} structure trigger does not belong "
                        f"to {self.race.value}."
                    )
            for child in condition.children:
                validate_condition(child, depth + 1)

        for phase in self.phases:
            if phase.id in seen_ids:
                errors.append(f"Duplicate id: {phase.id}")
            seen_ids.add(phase.id)
            validate_condition(phase.activation)
            for rule in phase.rules:
                if rule.id in seen_ids:
                    errors.append(f"Duplicate id: {rule.id}")
                seen_ids.add(rule.id)
                validate_condition(rule.trigger)
                for action in rule.actions:
                    spec = ACTION_SPECS[action.type]
                    if self.race not in spec.defaults_by_race:
                        errors.append(
                            f"{action.type.value} is not supported for {self.race.value}."
                        )
                    action_units = action.units + action.fallback_units
                    if action.unit:
                        action_units.append(action.unit)
                    if action.required_unit:
                        action_units.append(action.required_unit)
                    for unit in action_units:
                        if unit not in allowed_units:
                            errors.append(f"{unit.value} does not belong to {self.race.value}.")
                    if action.structure and action.structure not in allowed_structures:
                        errors.append(
                            f"{action.structure.value} does not belong to {self.race.value}."
                        )
                    if action.upgrade and action.upgrade not in RACE_UPGRADES[self.race]:
                        errors.append(
                            f"{action.upgrade.value} does not belong to {self.race.value}."
                        )
        if errors:
            raise ValueError(" ".join(errors))
        return self


class BotCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str | None = None
    description: str = ""
    race: RaceName
    tags: list[str] = Field(default_factory=list)
    strategy: StrategyDocument


class BotUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    tags: list[str] | None = None
    strategy: StrategyDocument | None = None
    change_summary: str = "Manual edit"
    expected_revision: int | None = None


class ForkRequest(BaseModel):
    name: str | None = None


class RestoreRevisionRequest(BaseModel):
    change_summary: str = "Restored earlier revision"


class AssistantProposalRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    base_bot_id: UUID | None = None
    strategy: StrategyDocument | None = None
    requested_name: str | None = None
    requested_race: RaceName | None = None


class StrategyProposal(BaseModel):
    summary: str
    suggested_name: str
    suggested_slug: str
    description: str = ""
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    strategy: StrategyDocument


class ApplyProposalRequest(BaseModel):
    expected_revision: int | None = None


class MatchCreate(BaseModel):
    bot_id: UUID
    map_name: str
    opponent_type: Literal["computer", "bot"] = "computer"
    enemy_race: str = "zerg"
    difficulty: str = "easy"
    opponent_bot_id: UUID | None = None

    @model_validator(mode="after")
    def validate_opponent(self) -> "MatchCreate":
        if self.opponent_type == "bot" and self.opponent_bot_id is None:
            raise ValueError("Studio bot matches require opponent_bot_id.")
        if self.opponent_type == "bot" and self.opponent_bot_id == self.bot_id:
            raise ValueError("Choose a different Studio bot as the opponent.")
        if self.opponent_type == "computer" and self.opponent_bot_id is not None:
            raise ValueError("Computer matches cannot include opponent_bot_id.")
        return self


class BenchmarkScenarioInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    map_name: str = Field(min_length=1)
    opponent_type: Literal["computer", "bot"] = "computer"
    enemy_race: str = "zerg"
    difficulty: str = "easy"
    opponent_bot_id: UUID | None = None
    opponent_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_opponent(self) -> "BenchmarkScenarioInput":
        if self.opponent_type == "bot":
            if self.opponent_bot_id is None or self.opponent_revision is None:
                raise ValueError("Bot benchmarks require a bot and pinned revision.")
        elif self.opponent_bot_id is not None or self.opponent_revision is not None:
            raise ValueError("Computer benchmarks cannot include a Studio bot revision.")
        return self


class BenchmarkSuiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    scenarios: list[BenchmarkScenarioInput] = Field(min_length=1, max_length=20)


class BenchmarkSuiteUpdate(BenchmarkSuiteCreate):
    pass


class RegressionCreate(BaseModel):
    bot_id: UUID
    baseline_revision: int = Field(ge=1)
    suite_id: UUID
    games_per_scenario: int = Field(default=3, ge=1, le=10)
    concurrency: int = Field(default=1, ge=1, le=2)


class RevisionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    number: int
    summary: str
    created_at: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def blank_strategy(race: RaceName) -> StrategyDocument:
    return StrategyDocument(
        race=race,
        phases=[
            StrategyPhase(
                id="opening",
                name="Opening",
                order=0,
                rules=[
                    StrategyRule(
                        id="distribute-workers",
                        name="Distribute workers",
                        priority=10,
                        actions=[StrategyAction(type=ActionType.DISTRIBUTE_WORKERS)],
                    )
                ],
            )
        ],
    )


def validation_result(strategy: StrategyDocument) -> dict[str, Any]:
    warnings: list[str] = []
    if not strategy.phases:
        warnings.append("This strategy has no phases and will take no actions.")
    if strategy.phases and not any(phase.enabled and phase.rules for phase in strategy.phases):
        warnings.append("No enabled phase contains any rules.")
    for phase in strategy.phases:
        if phase.enabled and not phase.rules:
            warnings.append(f"Phase {phase.name!r} is enabled but has no rules.")
        if phase.enabled and phase.rules and not any(rule.enabled for rule in phase.rules):
            warnings.append(f"Phase {phase.name!r} has no enabled rules.")

    available_structures = {TOWNHALL_BY_RACE[strategy.race]}
    available_structures.update(
        action.structure
        for phase in strategy.phases
        if phase.enabled
        for rule in phase.rules
        if rule.enabled
        for action in rule.actions
        if action.type in {ActionType.BUILD_STRUCTURE, ActionType.BUILD_FORWARD}
        and action.structure is not None
    )
    missing_dependencies: set[tuple[str, StructureName]] = set()
    for phase in strategy.phases:
        if not phase.enabled:
            continue
        for rule in phase.rules:
            if not rule.enabled:
                continue
            for action in rule.actions:
                if action.type == ActionType.TRAIN_UNITS:
                    requested = (
                        ([action.unit] if action.unit else action.units)
                        + action.fallback_units
                    )
                    for unit in requested:
                        if unit is None:
                            continue
                        requirement = TECH_REQUIREMENT_BY_UNIT[unit]
                        if (
                            requirement
                            and requirement not in available_structures
                        ):
                            missing_dependencies.add(
                                (unit.value, requirement)
                            )
                if action.type == ActionType.RESEARCH and action.upgrade:
                    producer = PRODUCER_BY_UPGRADE[action.upgrade]
                    if producer not in available_structures:
                        missing_dependencies.add((action.upgrade.value, producer))
    for entity, producer in sorted(
        missing_dependencies,
        key=lambda item: (item[1].value, item[0]),
    ):
        warnings.append(
            f"{entity} needs {producer.value}, but no enabled build action "
            "provides that structure."
        )
    return {"valid": True, "errors": [], "warnings": warnings}
