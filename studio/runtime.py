from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
import re
from typing import Callable

from sc2.bot_ai import BotAI
from sc2.ids.upgrade_id import UpgradeId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.units import Units
from s2clientprotocol import debug_pb2 as debug_pb
from s2clientprotocol import sc2api_pb2 as sc_pb

from .catalog import (
    ACTION_SPECS,
    PRODUCER_BY_UNIT,
    PRODUCER_BY_UPGRADE,
    SUPPLY_BY_RACE,
    WORKER_BY_RACE,
    TargetLocation,
)
from .models import (
    ActionType,
    Comparator,
    Condition,
    ConditionKind,
    ExecutionPolicy,
    MetricName,
    StrategyAction,
    StrategyDocument,
    StrategyRule,
)

DEFAULT_SUPPLY_BUILD_DISTANCE = 7


def _unit_type(name: str) -> UnitTypeId:
    return getattr(UnitTypeId, name)


def _upgrade_type(name: str) -> UpgradeId:
    return getattr(UpgradeId, name)


def compare(left: float, comparator: Comparator, right: float) -> bool:
    return {
        Comparator.LT: left < right,
        Comparator.LTE: left <= right,
        Comparator.EQ: left == right,
        Comparator.GTE: left >= right,
        Comparator.GT: left > right,
    }[comparator]


class ActionStatus(str, Enum):
    SATISFIED = "satisfied"
    PROGRESSED = "progressed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ActionOutcome:
    status: ActionStatus
    reason: str

    @property
    def completes_once(self) -> bool:
        """Only a fulfilled goal completes an ONCE rule.

        A command that merely starts a count-based action is progress, so the
        rule remains eligible until its target is observable in game state.
        """
        return self.status == ActionStatus.SATISFIED

    @classmethod
    def satisfied(cls, reason: str) -> "ActionOutcome":
        return cls(ActionStatus.SATISFIED, reason)

    @classmethod
    def progressed(cls, reason: str) -> "ActionOutcome":
        return cls(ActionStatus.PROGRESSED, reason)

    @classmethod
    def blocked(cls, reason: str) -> "ActionOutcome":
        return cls(ActionStatus.BLOCKED, reason)


@dataclass(frozen=True)
class RuleEligibility:
    allowed: bool
    reason: str


def rule_eligibility(
    rule: StrategyRule,
    *,
    now: float,
    completed_rules: set[str] | frozenset[str],
    last_execution: dict[str, float],
) -> RuleEligibility:
    """Pure execution-policy decision used by the game adapter and tests."""
    if rule.execution == ExecutionPolicy.ONCE and rule.id in completed_rules:
        return RuleEligibility(False, "once rule already completed")
    if rule.execution == ExecutionPolicy.COOLDOWN:
        previous = last_execution.get(rule.id)
        assert rule.cooldown_seconds is not None
        if previous is not None and now - previous < rule.cooldown_seconds:
            return RuleEligibility(False, "cooldown has not elapsed")
    return RuleEligibility(True, "eligible")


def condition_matches(
    condition: Condition,
    metric_reader: Callable[[Condition], float],
) -> bool:
    """Evaluate a validated condition without depending on a live SC2 client."""
    if condition.kind == ConditionKind.ALWAYS:
        return True
    if condition.kind == ConditionKind.ALL:
        return all(condition_matches(child, metric_reader) for child in condition.children)
    if condition.kind == ConditionKind.ANY:
        return any(condition_matches(child, metric_reader) for child in condition.children)
    if condition.kind == ConditionKind.NOT:
        return not condition_matches(condition.children[0], metric_reader)
    return compare(metric_reader(condition), condition.comparator, condition.value)


def target_count_outcome(current: float, target: int, noun: str) -> ActionOutcome | None:
    """Return a completed outcome when a count target is already met."""
    if current >= target:
        return ActionOutcome.satisfied(
            f"{noun} target met ({current:g}/{target})"
        )
    return None


def once_rule_completed(
    rule: StrategyRule,
    outcomes: Iterable[ActionOutcome],
) -> bool:
    """Return whether this attempt fulfills an ONCE rule's whole action set."""
    return (
        rule.execution == ExecutionPolicy.ONCE
        and all(outcome.completes_once for outcome in outcomes)
    )


def is_surrender_message(message: str) -> bool:
    """Return whether an opponent chat message is an unambiguous concession."""
    normalized = re.sub(r"[^a-z]+", " ", message.casefold()).strip()
    return normalized in {
        "gg",
        "gg wp",
        "good game",
        "i concede",
        "i surrender",
    }


def stalemate_expired(
    game_time: float,
    last_activity_time: float,
    grace_period_seconds: int,
    timeout_seconds: int,
) -> bool:
    return (
        game_time >= grace_period_seconds
        and game_time - last_activity_time >= timeout_seconds
    )


def least_recently_scouted_position(
    candidates,
    last_scouted: dict[tuple[float, float], float],
    reference,
):
    return min(
        candidates,
        key=lambda position: (
            last_scouted.get(
                (round(float(position.x), 1), round(float(position.y), 1)),
                -1,
            ),
            position.distance_to(reference),
        ),
    )


async def declare_victory(client) -> None:
    """End a local SC2 API game with victory for the requesting player."""
    await client._execute(
        debug=sc_pb.RequestDebug(
            debug=[
                debug_pb.DebugCommand(
                    end_game=debug_pb.DebugEndGame(
                        end_result=debug_pb.DebugEndGame.DeclareVictory
                    )
                )
            ]
        )
    )


class DeclarativeBot(BotAI):
    """A burnySC2 bot that executes a validated StrategyDocument."""

    def __init__(
        self,
        strategy: StrategyDocument,
        *,
        accept_computer_surrender: bool = False,
    ):
        super().__init__()
        self.strategy = strategy
        self.accept_computer_surrender = accept_computer_surrender
        self._completed_rules: set[str] = set()
        self._last_rule_execution: dict[str, float] = {}
        self._scheduled_structures: dict[UnitTypeId, int] = {}
        self._scheduled_units: dict[UnitTypeId, int] = {}
        self._scheduled_upgrades: set[UpgradeId] = set()
        self._scouted_expansions: dict[tuple[float, float], float] = {}
        self._last_activity_signature: tuple[float, ...] | None = None
        self._last_activity_time = 0.0
        self.accepted_opponent_surrender = False
        self.stalemate_detected = False

    async def on_step(self, iteration: int) -> None:
        if await self._accept_opponent_surrender():
            return
        self._refresh_scouting_memory()
        if await self._end_if_stalemate():
            return
        self._scheduled_structures = {}
        self._scheduled_units = {}
        self._scheduled_upgrades = set()
        if iteration == 0 and self.strategy.opening_chat:
            await self.chat_send(self.strategy.opening_chat)

        phases = sorted(
            (phase for phase in self.strategy.phases if phase.enabled),
            key=lambda phase: phase.order,
        )
        rules: list[StrategyRule] = []
        for phase in phases:
            if self.evaluate_condition(phase.activation):
                rules.extend(rule for rule in phase.rules if rule.enabled)

        for rule in sorted(rules, key=lambda item: item.priority):
            if not self.should_execute(rule) or not self.evaluate_condition(rule.trigger):
                continue
            outcomes: list[ActionOutcome] = []
            for action in rule.actions:
                outcomes.append(await self.execute_action(action))
            self._last_rule_execution[rule.id] = self.time
            if once_rule_completed(rule, outcomes):
                self._completed_rules.add(rule.id)

    async def _accept_opponent_surrender(self) -> bool:
        if not self.accept_computer_surrender:
            return False
        if self.accepted_opponent_surrender:
            return True
        for message in self.state.chat:
            if (
                message.player_id != self.player_id
                and is_surrender_message(message.message)
            ):
                self.accepted_opponent_surrender = True
                print(
                    f"Opponent offered surrender ({message.message!r}); "
                    "accepting and declaring victory.",
                    flush=True,
                )
                await declare_victory(self.client)
                return True
        return False

    def _refresh_scouting_memory(self) -> None:
        for position in self.expansion_locations_list:
            if not self.is_visible(position):
                continue
            key = self._position_key(position)
            if key not in self._scouted_expansions:
                self._last_activity_time = self.time
            self._scouted_expansions[key] = self.time

    async def _end_if_stalemate(self) -> bool:
        settings = self.strategy.settings
        if not settings.stalemate_detection:
            return False
        signature = self._activity_signature()
        if self._last_activity_signature != signature:
            self._last_activity_signature = signature
            self._last_activity_time = self.time
            return False
        if not stalemate_expired(
            self.time,
            self._last_activity_time,
            settings.stalemate_grace_period_seconds,
            settings.stalemate_timeout_seconds,
        ):
            return False
        self.stalemate_detected = True
        print(
            "No meaningful combat, production, construction, or scouting "
            f"progress for {settings.stalemate_timeout_seconds} in-game seconds; "
            "ending as a stalemate.",
            flush=True,
        )
        await self.client.leave()
        return True

    def _activity_signature(self) -> tuple[float, ...]:
        score = self.state.score
        return (
            float(score.spent_minerals),
            float(score.spent_vespene),
            float(score.killed_value_units),
            float(score.killed_value_structures),
            float(score.total_damage_dealt_life),
            float(score.total_damage_dealt_shields),
            float(self.units.amount),
            float(self.structures.amount),
        )

    def should_execute(self, rule: StrategyRule) -> bool:
        return rule_eligibility(
            rule,
            now=self.time,
            completed_rules=self._completed_rules,
            last_execution=self._last_rule_execution,
        ).allowed

    def evaluate_condition(self, condition: Condition) -> bool:
        return condition_matches(condition, self.metric_value)

    def metric_value(self, condition: Condition) -> float:
        metric = condition.metric
        if metric == MetricName.GAME_TIME:
            return self.time
        if metric == MetricName.SUPPLY_USED:
            return self.supply_used
        if metric == MetricName.SUPPLY_LEFT:
            return self.supply_left
        if metric == MetricName.WORKERS:
            return self.supply_workers
        if metric == MetricName.MINERALS:
            return self.minerals
        if metric == MetricName.VESPENE:
            return self.vespene
        if metric == MetricName.BASES:
            return self.townhalls.amount
        if metric == MetricName.UNIT_COUNT and condition.subject:
            return self.units(_unit_type(condition.subject)).amount
        if metric == MetricName.ENEMY_UNIT_COUNT and condition.subject:
            return self.enemy_units(_unit_type(condition.subject)).amount
        if metric == MetricName.STRUCTURE_COUNT and condition.subject:
            structure = _unit_type(condition.subject)
            if condition.status == "ready":
                return self.structures(structure).ready.amount
            if condition.status == "pending":
                return self.already_pending(structure)
            return (
                self.structures(structure).ready.amount
                + self.already_pending(structure)
            )
        return 0

    async def execute_action(self, action: StrategyAction) -> ActionOutcome:
        handler_name = ACTION_SPECS[action.type].handler
        handler = getattr(self, handler_name)
        try:
            return await handler(action)
        except Exception as exc:
            raise RuntimeError(
                f"{action.type.value} action failed: {exc}"
            ) from exc

    async def _distribute_workers(
        self,
        _action: StrategyAction,
    ) -> ActionOutcome:
        await self.distribute_workers()
        return ActionOutcome.satisfied("worker distribution command issued")

    async def _train_workers(self, action: StrategyAction) -> ActionOutcome:
        assert action.amount is not None
        worker = _unit_type(WORKER_BY_RACE[self.strategy.race].value)
        current = (
            self.supply_workers
            + self.already_pending(worker)
            + self._scheduled_unit(worker)
        )
        completed = target_count_outcome(current, action.amount, "worker")
        if completed:
            return completed
        remaining = max(1, int(action.amount - current))
        trained = 0
        if self.strategy.race.value == "zerg":
            for larva in self.larva:
                if trained >= remaining:
                    break
                if self.can_afford(worker) and self.supply_left > 0:
                    larva.train(worker)
                    self._mark_scheduled_unit(worker)
                    trained += 1
            if trained:
                return ActionOutcome.progressed(
                    f"scheduled {trained} worker(s)"
                )
            return ActionOutcome.blocked("no affordable larva is available for a worker")
        for townhall in self.townhalls.ready.idle:
            if trained >= remaining:
                break
            if self.can_afford(worker) and self.supply_left > 0:
                townhall.train(worker)
                self._mark_scheduled_unit(worker)
                trained += 1
        if trained:
            return ActionOutcome.progressed(f"scheduled {trained} worker(s)")
        return ActionOutcome.blocked("no affordable idle town hall can train a worker")

    async def _maintain_supply(self, action: StrategyAction) -> ActionOutcome:
        assert action.buffer is not None
        if self.supply_cap >= self.strategy.settings.max_supply:
            return ActionOutcome.satisfied("maximum supply reached")
        if self.supply_left >= action.buffer:
            return ActionOutcome.satisfied("free-supply buffer is healthy")
        provider = SUPPLY_BY_RACE[self.strategy.race]
        provider_type = _unit_type(provider.value)
        if (
            self.already_pending(provider_type)
            + self._scheduled_entity(provider_type)
            > 0
        ):
            return ActionOutcome.progressed("supply provider is already pending")
        if not self.can_afford(provider_type):
            return ActionOutcome.blocked("cannot afford a supply provider")
        if self.strategy.race.value == "zerg":
            if self.larva:
                self.larva.first.train(provider_type)
                self._mark_scheduled_unit(provider_type)
                return ActionOutcome.progressed("scheduled a supply provider")
            return ActionOutcome.blocked("no larva is available for a supply provider")
        built = await self.build_structure_near_main(
            provider_type,
            DEFAULT_SUPPLY_BUILD_DISTANCE,
        )
        if built:
            self._mark_scheduled_structure(provider_type)
            return ActionOutcome.progressed("scheduled a supply structure")
        return ActionOutcome.blocked("no valid worker or placement for supply")

    async def _build_structure(self, action: StrategyAction) -> ActionOutcome:
        assert action.structure is not None and action.amount is not None
        structure = _unit_type(action.structure.value)
        current = self._structure_total(structure)
        completed = target_count_outcome(
            current,
            action.amount,
            action.structure.value,
        )
        if completed:
            return completed
        if not self.can_afford(structure):
            return ActionOutcome.blocked(
                f"cannot afford {action.structure.value}"
            )
        distance = self._action_value(action, "distance")
        built = await self.build_structure_near_main(structure, distance)
        if built:
            self._mark_scheduled_structure(structure)
            return ActionOutcome.progressed(
                f"scheduled {action.structure.value}"
            )
        return ActionOutcome.blocked(
            f"no valid worker or placement for {action.structure.value}"
        )

    async def _maintain_gas(self, action: StrategyAction) -> ActionOutcome:
        assert action.structure is not None and action.amount is not None
        structure = _unit_type(action.structure.value)
        current = self._structure_total(structure)
        completed = target_count_outcome(
            current,
            action.amount,
            action.structure.value,
        )
        if completed:
            return completed
        for townhall in self.townhalls.ready:
            for geyser in self.vespene_geyser.closer_than(10, townhall):
                if self.gas_buildings.closer_than(1, geyser):
                    continue
                if not self.can_afford(structure):
                    return ActionOutcome.blocked(
                        f"cannot afford {action.structure.value}"
                    )
                worker = self.select_build_worker(geyser.position)
                if worker:
                    worker.build_gas(geyser)
                    self._mark_scheduled_structure(structure)
                    return ActionOutcome.progressed(
                        f"scheduled {action.structure.value}"
                    )
        return ActionOutcome.blocked("no free geyser and worker are available")

    async def _train_units(self, action: StrategyAction) -> ActionOutcome:
        requested = ([action.unit] if action.unit else action.units) + action.fallback_units
        for unit_name in requested:
            if unit_name is None:
                continue
            unit = _unit_type(unit_name.value)
            if not self.can_afford(unit) or self.supply_left <= 0:
                continue
            if self.tech_requirement_progress(unit) < 1:
                continue
            producer_name = PRODUCER_BY_UNIT[unit_name]
            if producer_name is None:
                trained = 0
                for larva in self.larva:
                    if self.can_afford(unit) and self.supply_left > 0:
                        larva.train(unit)
                        self._mark_scheduled_unit(unit)
                        trained += 1
                if trained:
                    return ActionOutcome.satisfied(
                        f"scheduled {trained} {unit_name.value}"
                    )
                continue
            producer = _unit_type(producer_name.value)
            trained = 0
            for building in self.structures(producer).ready.idle:
                if self.can_afford(unit) and self.supply_left > 0:
                    building.train(unit)
                    self._mark_scheduled_unit(unit)
                    trained += 1
            if trained:
                return ActionOutcome.satisfied(
                    f"scheduled {trained} {unit_name.value}"
                )
        return ActionOutcome.blocked(
            "no requested unit is affordable, eligible, and trainable"
        )

    async def _expand(self, action: StrategyAction) -> ActionOutcome:
        assert action.structure is not None and action.amount is not None
        townhall = _unit_type(action.structure.value)
        current = (
            self.townhalls.ready.amount
            + self.already_pending(townhall)
            + self._scheduled_structure(townhall)
        )
        completed = target_count_outcome(current, action.amount, "town hall")
        if completed:
            return completed
        if not self.can_afford(townhall):
            return ActionOutcome.blocked(
                f"cannot afford {action.structure.value}"
            )
        location = await self.get_next_expansion()
        if location is None:
            return ActionOutcome.blocked("no reachable expansion location remains")
        built = await self.build(
            townhall,
            near=location,
            max_distance=10,
            random_alternative=False,
            placement_step=1,
        )
        if not built:
            return ActionOutcome.blocked("could not place or schedule the expansion")
        self._mark_scheduled_structure(townhall)
        return ActionOutcome.progressed("scheduled a new town hall")

    async def _attack(self, action: StrategyAction) -> ActionOutcome:
        assert action.min_size is not None
        army = self._combine_units(_unit_type(unit.value) for unit in action.units)
        if action.required_unit and action.required_amount:
            required = self.units(_unit_type(action.required_unit.value)).amount
            if required < action.required_amount:
                return ActionOutcome.blocked(
                    f"requires {action.required_amount} {action.required_unit.value}"
                )
        if army.amount < action.min_size:
            return ActionOutcome.blocked(
                f"army size is {army.amount}/{action.min_size}"
            )
        target = self.choose_attack_target(army)
        for unit in army.idle:
            unit.attack(target)
        return ActionOutcome.satisfied("attack command issued")

    async def _build_forward(self, action: StrategyAction) -> ActionOutcome:
        assert action.structure is not None and action.amount is not None
        structure = _unit_type(action.structure.value)
        enemy_start = self.enemy_start_locations[0]
        forward_structures = self.structures(structure).closer_than(35, enemy_start)
        current = forward_structures.amount + self._scheduled_structure(structure)
        completed = target_count_outcome(
            current,
            action.amount,
            f"forward {action.structure.value}",
        )
        if completed:
            return completed
        if self.worker_en_route_to_build(structure) > 0:
            return ActionOutcome.progressed(
                f"a worker is en route to build {action.structure.value}"
            )
        if not self.can_afford(structure):
            return ActionOutcome.blocked(
                f"cannot afford {action.structure.value}"
            )

        distance = self._action_value(action, "distance")
        anchor = enemy_start.towards(self.game_info.map_center, distance)
        if action.structure.value in {"PHOTONCANNON", "GATEWAY"}:
            pylons = self.structures(UnitTypeId.PYLON).ready.closer_than(35, enemy_start)
            if not pylons:
                return ActionOutcome.blocked(
                    f"a forward PYLON is required for {action.structure.value}"
                )
            anchor = pylons.closest_to(enemy_start).position
        built = await self.build_structure_near_position(structure, anchor)
        if built:
            self._mark_scheduled_structure(structure)
            return ActionOutcome.progressed(
                f"scheduled forward {action.structure.value}"
            )
        return ActionOutcome.blocked(
            f"no valid forward placement for {action.structure.value}"
        )

    async def _emergency_worker_attack(
        self,
        _action: StrategyAction,
    ) -> ActionOutcome:
        if self.townhalls:
            return ActionOutcome.blocked("a town hall still survives")
        if not self.workers:
            return ActionOutcome.satisfied("no workers remain")
        target = self.choose_attack_target(self.workers)
        for worker in self.workers:
            worker.attack(target)
        return ActionOutcome.satisfied("emergency worker attack issued")

    async def _scout(self, action: StrategyAction) -> ActionOutcome:
        assert action.unit is not None
        scouts = self.units(_unit_type(action.unit.value))
        if not scouts:
            return ActionOutcome.blocked(f"no {action.unit.value} is available")
        target_name = self._action_value(action, "target")
        target = self.resolve_target_position(target_name, scouts.center)
        scout = scouts.closest_to(target)
        scout.move(target)
        return ActionOutcome.satisfied(
            f"{action.unit.value} scout sent to {target_name.value}"
        )

    async def _defend(self, action: StrategyAction) -> ActionOutcome:
        assert action.min_size is not None
        defenders = self._combine_units(
            _unit_type(unit.value) for unit in action.units
        )
        if defenders.amount < action.min_size:
            return ActionOutcome.blocked(
                f"defender count is {defenders.amount}/{action.min_size}"
            )
        target_name = self._action_value(action, "target")
        anchor = self.resolve_target_position(target_name, defenders.center)
        radius = self._action_value(action, "distance")
        threats = self.enemy_units.closer_than(radius, anchor)
        if threats:
            target = threats.closest_to(anchor)
            for defender in defenders:
                defender.attack(target)
            return ActionOutcome.satisfied("defenders engaged a nearby threat")
        for defender in defenders.idle.further_than(6, anchor):
            defender.move(anchor)
        return ActionOutcome.satisfied("defensive posture established")

    async def _retreat(self, action: StrategyAction) -> ActionOutcome:
        assert action.health_threshold is not None
        selected = self._combine_units(
            _unit_type(unit.value) for unit in action.units
        )
        wounded = selected.filter(
            lambda unit: unit.shield_health_percentage
            <= action.health_threshold
        )
        if not wounded:
            return ActionOutcome.satisfied("no selected unit needs to retreat")
        target_name = self._action_value(action, "target")
        anchor = self.resolve_target_position(target_name, wounded.center)
        for unit in wounded:
            unit.move(anchor)
        return ActionOutcome.satisfied(
            f"retreated {wounded.amount} wounded unit(s)"
        )

    async def _research(self, action: StrategyAction) -> ActionOutcome:
        assert action.upgrade is not None
        upgrade = _upgrade_type(action.upgrade.value)
        progress = self.already_pending_upgrade(upgrade)
        if progress >= 1:
            return ActionOutcome.satisfied(
                f"{action.upgrade.value} is complete"
            )
        if progress > 0 or upgrade in self._scheduled_upgrades:
            return ActionOutcome.progressed(
                f"{action.upgrade.value} is in progress"
            )
        if not self.can_afford(upgrade):
            return ActionOutcome.blocked(
                f"cannot afford {action.upgrade.value}"
            )
        producer_name = PRODUCER_BY_UPGRADE[action.upgrade]
        producers = self.structures(_unit_type(producer_name.value)).ready.idle
        if not producers:
            return ActionOutcome.blocked(
                f"an idle {producer_name.value} is required"
            )
        command = producers.first.research(upgrade)
        if not command:
            return ActionOutcome.blocked(
                f"{producer_name.value} cannot research {action.upgrade.value}"
            )
        self._scheduled_upgrades.add(upgrade)
        return ActionOutcome.progressed(
            f"started {action.upgrade.value} at {producer_name.value}"
        )

    def _combine_units(self, unit_types: Iterable[UnitTypeId]) -> Units:
        army = Units([], self)
        for unit_type in unit_types:
            army = army | self.units(unit_type)
        return army

    def _action_value(self, action: StrategyAction, field: str):
        value = getattr(action, field)
        if value is not None:
            return value
        defaults = ACTION_SPECS[action.type].defaults_by_race[self.strategy.race]
        if field not in defaults:
            raise RuntimeError(
                f"{action.type.value}.{field} has no value or contract default"
            )
        return defaults[field]

    def _scheduled_structure(self, structure: UnitTypeId) -> int:
        return self._scheduled_structures.get(structure, 0)

    def _mark_scheduled_structure(self, structure: UnitTypeId) -> None:
        self._scheduled_structures[structure] = (
            self._scheduled_structure(structure) + 1
        )

    def _scheduled_unit(self, unit: UnitTypeId) -> int:
        return self._scheduled_units.get(unit, 0)

    def _mark_scheduled_unit(self, unit: UnitTypeId) -> None:
        self._scheduled_units[unit] = self._scheduled_unit(unit) + 1

    def _scheduled_entity(self, entity: UnitTypeId) -> int:
        return self._scheduled_structure(entity) + self._scheduled_unit(entity)

    def _structure_total(self, structure: UnitTypeId) -> float:
        """Count completed, pending, and same-frame scheduled structures once."""
        return (
            self.structures(structure).ready.amount
            + self.already_pending(structure)
            + self._scheduled_structure(structure)
        )

    def choose_attack_target(self, army: Units):
        if self.enemy_units:
            return self.enemy_units.closest_to(army.center)
        if self.enemy_structures:
            return self.enemy_structures.closest_to(army.center)
        candidates = [
            position
            for position in self.expansion_locations_list
            if not self.townhalls.closer_than(8, position)
        ]
        if not candidates:
            return self.enemy_start_locations[0]
        return least_recently_scouted_position(
            candidates,
            self._scouted_expansions,
            army.center,
        )

    def resolve_target_position(self, target: TargetLocation, reference):
        if target == TargetLocation.MAP_CENTER:
            return self.game_info.map_center
        if target == TargetLocation.ENEMY_START:
            return self.enemy_start_locations[0]
        if target == TargetLocation.MAIN:
            if self.townhalls:
                return self.townhalls.first.position
            return self.game_info.player_start_location
        candidates = [
            position
            for position in self.expansion_locations_list
            if not self.townhalls.closer_than(8, position)
        ]
        if not candidates:
            return self.enemy_start_locations[0]
        return least_recently_scouted_position(
            candidates,
            self._scouted_expansions,
            reference,
        )

    @staticmethod
    def _position_key(position) -> tuple[float, float]:
        return (round(float(position.x), 1), round(float(position.y), 1))

    async def build_structure_near_position(
        self, structure_type: UnitTypeId, near_position, placement_step: int = 2
    ) -> bool:
        placement = await self.find_placement(
            structure_type,
            near=near_position,
            placement_step=placement_step,
        )
        if not placement:
            return False
        worker = self.select_build_worker(placement)
        if not worker:
            return False
        worker.build(structure_type, placement)
        return True

    async def build_structure_near_main(
        self, structure_type: UnitTypeId, distance: float, placement_step: int = 2
    ) -> bool:
        if not self.townhalls:
            return False
        near_position = self.townhalls.first.position.towards(self.game_info.map_center, distance)
        return await self.build_structure_near_position(
            structure_type,
            near_position,
            placement_step,
        )
