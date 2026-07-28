from __future__ import annotations

from types import SimpleNamespace

import pytest
from sc2.position import Point2

from studio.catalog import ACTION_SPECS, ActionType, RaceName, TargetLocation
from studio.models import (
    Condition,
    ExecutionPolicy,
    StrategyAction,
    StrategyRule,
    blank_strategy,
)
from studio.runtime import (
    ActionOutcome,
    ActionStatus,
    DeclarativeBot,
    condition_matches,
    once_rule_completed,
    rule_eligibility,
)


class FakeUnit:
    def __init__(self, health: float = 1.0):
        self.shield_health_percentage = health
        self.commands: list[tuple[str, object]] = []

    def attack(self, target):
        self.commands.append(("attack", target))
        return True

    def move(self, target):
        self.commands.append(("move", target))
        return True

    def research(self, upgrade):
        self.commands.append(("research", upgrade))
        return True


class FakeGroup(list):
    @property
    def amount(self) -> int:
        return len(self)

    @property
    def ready(self) -> "FakeGroup":
        return self

    @property
    def idle(self) -> "FakeGroup":
        return self

    @property
    def first(self):
        return self[0]

    @property
    def center(self) -> Point2:
        return Point2((0, 0))

    def closer_than(self, _distance, _target) -> "FakeGroup":
        return self

    def further_than(self, _distance, _target) -> "FakeGroup":
        return self

    def closest_to(self, _target):
        return self.first

    def filter(self, predicate) -> "FakeGroup":
        return FakeGroup(unit for unit in self if predicate(unit))


class HandlerHarness:
    """Small deterministic SC2 adapter used to exercise every handler."""

    def __init__(self, action_type: ActionType):
        self.action_type = action_type
        self.strategy = blank_strategy(RaceName.PROTOSS)
        self.supply_workers = 50
        self.supply_cap = 200
        self.supply_left = 20
        self.townhalls = FakeGroup([FakeUnit(), FakeUnit()])
        self.workers = FakeGroup()
        self.enemy_units = FakeGroup()
        self.enemy_structures = FakeGroup()
        self.enemy_start_locations = [Point2((100, 100))]
        self._scheduled_structures = {}
        self._scheduled_units = {}
        self._scheduled_upgrades = set()
        self.distributed = False

    async def distribute_workers(self):
        self.distributed = True

    def already_pending(self, _entity) -> float:
        return 0

    def already_pending_upgrade(self, _upgrade) -> float:
        return 1

    def can_afford(self, _entity) -> bool:
        return False

    def structures(self, _structure) -> FakeGroup:
        return FakeGroup([FakeUnit()])

    def units(self, _unit) -> FakeGroup:
        return FakeGroup()

    def _structure_total(self, _structure) -> float:
        return 200

    def _scheduled_structure(self, _structure) -> int:
        return 0

    def _scheduled_unit(self, _unit) -> int:
        return 0

    def _scheduled_entity(self, _entity) -> int:
        return 0

    def _action_value(self, action: StrategyAction, field: str):
        return DeclarativeBot._action_value(self, action, field)

    def _combine_units(self, _unit_types) -> FakeGroup:
        if self.action_type == ActionType.ATTACK:
            return FakeGroup(FakeUnit() for _ in range(10))
        return FakeGroup()

    def choose_attack_target(self, _army):
        return Point2((10, 10))

    def worker_en_route_to_build(self, _structure) -> int:
        return 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_type", "spec"),
    list(ACTION_SPECS.items()),
    ids=lambda value: value.value if isinstance(value, ActionType) else None,
)
async def test_every_contract_handler_is_callable_and_returns_an_outcome(
    action_type,
    spec,
):
    assert hasattr(DeclarativeBot, spec.handler)
    action = StrategyAction.model_validate(
        spec.defaults_by_race[RaceName.PROTOSS]
    )
    harness = HandlerHarness(action_type)

    outcome = await getattr(DeclarativeBot, spec.handler)(harness, action)

    assert isinstance(outcome, ActionOutcome)
    assert outcome.reason


def test_once_rule_requires_satisfied_not_merely_progressed_actions():
    rule = StrategyRule(
        id="once",
        name="Once",
        execution=ExecutionPolicy.ONCE,
        actions=[StrategyAction(type=ActionType.DISTRIBUTE_WORKERS)],
    )

    assert not once_rule_completed(
        rule,
        [ActionOutcome.progressed("command scheduled")],
    )
    assert not once_rule_completed(
        rule,
        [
            ActionOutcome.satisfied("first goal met"),
            ActionOutcome.blocked("second goal blocked"),
        ],
    )
    assert once_rule_completed(
        rule,
        [
            ActionOutcome.satisfied("first goal met"),
            ActionOutcome.satisfied("second goal met"),
        ],
    )


def test_rule_policy_is_deterministic_for_once_and_cooldown():
    once = StrategyRule(
        id="once",
        name="Once",
        execution="once",
        actions=[StrategyAction(type="distribute_workers")],
    )
    cooldown = StrategyRule(
        id="cooldown",
        name="Cooldown",
        execution="cooldown",
        cooldown_seconds=5,
        actions=[StrategyAction(type="distribute_workers")],
    )

    assert not rule_eligibility(
        once,
        now=10,
        completed_rules={"once"},
        last_execution={},
    ).allowed
    assert not rule_eligibility(
        cooldown,
        now=12,
        completed_rules=set(),
        last_execution={"cooldown": 10},
    ).allowed
    assert rule_eligibility(
        cooldown,
        now=15,
        completed_rules=set(),
        last_execution={"cooldown": 10},
    ).allowed


def test_nested_condition_evaluation_uses_a_pure_metric_reader():
    condition = Condition.model_validate(
        {
            "kind": "all",
            "children": [
                {
                    "kind": "metric",
                    "metric": "minerals",
                    "comparator": "gte",
                    "value": 100,
                },
                {
                    "kind": "not",
                    "children": [
                        {
                            "kind": "metric",
                            "metric": "vespene",
                            "comparator": "gt",
                            "value": 50,
                        }
                    ],
                },
            ],
        }
    )
    values = {"minerals": 125, "vespene": 25}

    assert condition_matches(
        condition,
        lambda item: values[item.metric.value],
    )


def test_total_structure_metric_does_not_double_count_pending_structures():
    condition = Condition.model_validate(
        {
            "kind": "metric",
            "metric": "structure_count",
            "subject": "GATEWAY",
            "status": "total",
            "value": 3,
        }
    )
    structures = SimpleNamespace(
        amount=3,
        ready=SimpleNamespace(amount=2),
    )
    harness = SimpleNamespace(
        structures=lambda _structure: structures,
        already_pending=lambda _structure: 1,
    )

    assert DeclarativeBot.metric_value(harness, condition) == 3


@pytest.mark.asyncio
async def test_scout_uses_its_contract_default_target_and_issues_a_move():
    action = StrategyAction(type="scout", unit="PROBE")
    scout = FakeUnit()
    target = Point2((20, 20))
    harness = HandlerHarness(ActionType.SCOUT)
    harness.units = lambda _unit: FakeGroup([scout])
    harness.resolve_target_position = lambda name, _reference: (
        target if name == TargetLocation.ENEMY_START else None
    )

    outcome = await DeclarativeBot._scout(harness, action)

    assert outcome.status == ActionStatus.SATISFIED
    assert scout.commands == [("move", target)]


@pytest.mark.asyncio
async def test_defend_engages_a_threat_near_the_selected_anchor():
    action = StrategyAction(
        type="defend",
        units=["ZEALOT"],
        min_size=2,
        target="main",
        distance=20,
    )
    defenders = [FakeUnit(), FakeUnit()]
    threat = FakeUnit()
    anchor = Point2((5, 5))
    harness = HandlerHarness(ActionType.DEFEND)
    harness._combine_units = lambda _types: FakeGroup(defenders)
    harness.enemy_units = FakeGroup([threat])
    harness.resolve_target_position = lambda _name, _reference: anchor

    outcome = await DeclarativeBot._defend(harness, action)

    assert outcome.status == ActionStatus.SATISFIED
    assert all(unit.commands == [("attack", threat)] for unit in defenders)


@pytest.mark.asyncio
async def test_retreat_moves_only_units_below_the_health_threshold():
    action = StrategyAction(
        type="retreat",
        units=["ZEALOT"],
        health_threshold=0.5,
        target="main",
    )
    wounded = FakeUnit(health=0.4)
    healthy = FakeUnit(health=0.8)
    anchor = Point2((5, 5))
    harness = HandlerHarness(ActionType.RETREAT)
    harness._combine_units = lambda _types: FakeGroup([wounded, healthy])
    harness.resolve_target_position = lambda _name, _reference: anchor

    outcome = await DeclarativeBot._retreat(harness, action)

    assert outcome.status == ActionStatus.SATISFIED
    assert wounded.commands == [("move", anchor)]
    assert healthy.commands == []


@pytest.mark.asyncio
async def test_research_reports_progress_after_eligible_producer_starts_it():
    action = StrategyAction(type="research", upgrade="WARPGATERESEARCH")
    producer = FakeUnit()
    harness = HandlerHarness(ActionType.RESEARCH)
    harness.already_pending_upgrade = lambda _upgrade: 0
    harness.can_afford = lambda _upgrade: True
    harness.structures = lambda _structure: FakeGroup([producer])

    outcome = await DeclarativeBot._research(harness, action)

    assert outcome.status == ActionStatus.PROGRESSED
    assert producer.commands[0][0] == "research"
    assert len(harness._scheduled_upgrades) == 1


@pytest.mark.asyncio
async def test_dispatch_errors_identify_the_action_that_failed():
    async def fail(_action):
        raise ValueError("adapter broke")

    harness = SimpleNamespace(_distribute_workers=fail)
    action = StrategyAction(type="distribute_workers")

    with pytest.raises(
        RuntimeError,
        match="distribute_workers action failed: adapter broke",
    ):
        await DeclarativeBot.execute_action(harness, action)
