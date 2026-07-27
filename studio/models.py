from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .catalog import RACE_STRUCTURES, RACE_UNITS, RaceName, StructureName, UnitName


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


class Condition(BaseModel):
    kind: ConditionKind = ConditionKind.ALWAYS
    metric: MetricName | None = None
    comparator: Comparator = Comparator.GTE
    value: float = 0
    subject: str | None = None
    status: str = "total"
    children: list["Condition"] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_shape(self) -> "Condition":
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


class Placement(str, Enum):
    MAIN = "main"
    ENEMY = "enemy"
    MAP_CENTER = "map_center"


class StrategyAction(BaseModel):
    type: ActionType
    unit: UnitName | None = None
    units: list[UnitName] = Field(default_factory=list)
    fallback_units: list[UnitName] = Field(default_factory=list)
    structure: StructureName | None = None
    amount: int | None = Field(default=None, ge=0)
    buffer: int | None = Field(default=None, ge=0)
    distance: float = Field(default=7, ge=0)
    placement: Placement = Placement.MAIN
    min_size: int | None = Field(default=None, ge=1)
    required_unit: UnitName | None = None
    required_amount: int | None = Field(default=None, ge=1)
    target: str = "enemy_start"

    @model_validator(mode="after")
    def validate_action(self) -> "StrategyAction":
        if self.type == ActionType.TRAIN_WORKERS and self.amount is None:
            raise ValueError("train_workers requires amount.")
        if self.type == ActionType.MAINTAIN_SUPPLY and self.buffer is None:
            raise ValueError("maintain_supply requires buffer.")
        if self.type in {
            ActionType.BUILD_STRUCTURE,
            ActionType.MAINTAIN_GAS,
            ActionType.BUILD_FORWARD,
            ActionType.EXPAND,
        }:
            if self.structure is None or self.amount is None:
                raise ValueError(f"{self.type.value} requires structure and amount.")
        if self.type == ActionType.TRAIN_UNITS and not ([self.unit] if self.unit else self.units):
            raise ValueError("train_units requires unit or units.")
        if self.type == ActionType.ATTACK and (not self.units or self.min_size is None):
            raise ValueError("attack requires units and min_size.")
        return self


class ExecutionPolicy(str, Enum):
    CONTINUOUS = "continuous"
    ONCE = "once"
    COOLDOWN = "cooldown"


class StrategyRule(BaseModel):
    id: str
    name: str
    enabled: bool = True
    priority: int = 100
    execution: ExecutionPolicy = ExecutionPolicy.CONTINUOUS
    cooldown_seconds: float = Field(default=1, ge=0)
    trigger: Condition = Field(default_factory=Condition)
    actions: list[StrategyAction] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_execution(self) -> "StrategyRule":
        if self.execution == ExecutionPolicy.COOLDOWN and self.cooldown_seconds <= 0:
            raise ValueError("Cooldown rules require cooldown_seconds greater than zero.")
        return self


class StrategyPhase(BaseModel):
    id: str
    name: str
    enabled: bool = True
    order: int = 0
    activation: Condition = Field(default_factory=Condition)
    rules: list[StrategyRule] = Field(default_factory=list)


class GlobalSettings(BaseModel):
    max_supply: int = Field(default=200, ge=1, le=200)
    attack_target: str = "enemy_start"
    stalemate_detection: bool = True
    stalemate_grace_period_seconds: int = Field(default=600, ge=0, le=7200)
    stalemate_timeout_seconds: int = Field(default=180, ge=60, le=1800)


class StrategyDocument(BaseModel):
    schema_version: int = Field(default=1, ge=1)
    race: RaceName
    opening_chat: str | None = None
    settings: GlobalSettings = Field(default_factory=GlobalSettings)
    phases: list[StrategyPhase] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_race_catalog(self) -> "StrategyDocument":
        allowed_units = RACE_UNITS[self.race]
        allowed_structures = RACE_STRUCTURES[self.race]
        errors: list[str] = []
        seen_ids: set[str] = set()

        def validate_condition(condition: Condition) -> None:
            if condition.metric == MetricName.UNIT_COUNT and condition.subject:
                if UnitName(condition.subject) not in allowed_units:
                    errors.append(
                        f"{condition.subject} unit trigger does not belong to {self.race.value}."
                    )
            if condition.metric == MetricName.STRUCTURE_COUNT and condition.subject:
                if StructureName(condition.subject) not in allowed_structures:
                    errors.append(
                        f"{condition.subject} structure trigger does not belong to {self.race.value}."
                    )
            for child in condition.children:
                validate_condition(child)

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
    return {"valid": True, "errors": [], "warnings": warnings}
