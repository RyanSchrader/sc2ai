from __future__ import annotations

from collections.abc import Iterable
import re

from sc2.bot_ai import BotAI
from sc2.ids.unit_typeid import UnitTypeId
from sc2.units import Units
from s2clientprotocol import debug_pb2 as debug_pb
from s2clientprotocol import sc2api_pb2 as sc_pb

from .catalog import GAS_BY_RACE, PRODUCER_BY_UNIT, SUPPLY_BY_RACE, WORKER_BY_RACE
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


def _unit_type(name: str) -> UnitTypeId:
    return getattr(UnitTypeId, name)


def compare(left: float, comparator: Comparator, right: float) -> bool:
    return {
        Comparator.LT: left < right,
        Comparator.LTE: left <= right,
        Comparator.EQ: left == right,
        Comparator.GTE: left >= right,
        Comparator.GT: left > right,
    }[comparator]


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
            completed = True
            for action in rule.actions:
                completed = await self.execute_action(action) and completed
            self._last_rule_execution[rule.id] = self.time
            if completed and rule.execution == ExecutionPolicy.ONCE:
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
        if rule.execution == ExecutionPolicy.ONCE:
            return rule.id not in self._completed_rules
        if rule.execution == ExecutionPolicy.COOLDOWN:
            last_execution = self._last_rule_execution.get(rule.id)
            return last_execution is None or self.time - last_execution >= rule.cooldown_seconds
        return True

    def evaluate_condition(self, condition: Condition) -> bool:
        if condition.kind == ConditionKind.ALWAYS:
            return True
        if condition.kind == ConditionKind.ALL:
            return all(self.evaluate_condition(child) for child in condition.children)
        if condition.kind == ConditionKind.ANY:
            return any(self.evaluate_condition(child) for child in condition.children)
        if condition.kind == ConditionKind.NOT:
            return not self.evaluate_condition(condition.children[0])

        value = self.metric_value(condition)
        return compare(value, condition.comparator, condition.value)

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
            return self.structures(structure).amount + self.already_pending(structure)
        return 0

    async def execute_action(self, action: StrategyAction) -> bool:
        handlers = {
            ActionType.DISTRIBUTE_WORKERS: self._distribute_workers,
            ActionType.TRAIN_WORKERS: self._train_workers,
            ActionType.MAINTAIN_SUPPLY: self._maintain_supply,
            ActionType.BUILD_STRUCTURE: self._build_structure,
            ActionType.MAINTAIN_GAS: self._maintain_gas,
            ActionType.TRAIN_UNITS: self._train_units,
            ActionType.EXPAND: self._expand,
            ActionType.ATTACK: self._attack,
            ActionType.BUILD_FORWARD: self._build_forward,
            ActionType.EMERGENCY_WORKER_ATTACK: self._emergency_worker_attack,
        }
        return await handlers[action.type](action)

    async def _distribute_workers(self, _action: StrategyAction) -> bool:
        await self.distribute_workers()
        return True

    async def _train_workers(self, action: StrategyAction) -> bool:
        assert action.amount is not None
        if self.supply_workers >= action.amount:
            return True
        worker = _unit_type(WORKER_BY_RACE[self.strategy.race].value)
        trained = False
        if self.strategy.race.value == "zerg":
            for larva in self.larva:
                if self.supply_workers >= action.amount:
                    break
                if self.can_afford(worker) and self.supply_left > 0:
                    larva.train(worker)
                    trained = True
            return trained
        for townhall in self.townhalls.ready.idle:
            if self.can_afford(worker) and self.supply_left > 0:
                townhall.train(worker)
                trained = True
        return trained

    async def _maintain_supply(self, action: StrategyAction) -> bool:
        assert action.buffer is not None
        if self.supply_cap >= self.strategy.settings.max_supply or self.supply_left >= action.buffer:
            return True
        provider = SUPPLY_BY_RACE[self.strategy.race]
        provider_type = _unit_type(provider.value)
        if self.already_pending(provider_type) + self._scheduled(provider_type) > 0:
            return True
        if not self.can_afford(provider_type):
            return False
        if self.strategy.race.value == "zerg":
            if self.larva:
                self.larva.first.train(provider_type)
                self._mark_scheduled(provider_type)
                return True
            return False
        built = await self.build_structure_near_main(provider_type, action.distance)
        if built:
            self._mark_scheduled(provider_type)
        return built

    async def _build_structure(self, action: StrategyAction) -> bool:
        assert action.structure is not None and action.amount is not None
        structure = _unit_type(action.structure.value)
        current = (
            self.structures(structure).amount
            + self.already_pending(structure)
            + self._scheduled(structure)
        )
        if current >= action.amount:
            return True
        if not self.can_afford(structure):
            return False
        built = await self.build_structure_near_main(structure, action.distance)
        if built:
            self._mark_scheduled(structure)
        return built

    async def _maintain_gas(self, action: StrategyAction) -> bool:
        assert action.amount is not None
        structure_name = action.structure or GAS_BY_RACE[self.strategy.race]
        structure = _unit_type(structure_name.value)
        current = (
            self.gas_buildings.amount
            + self.already_pending(structure)
            + self._scheduled(structure)
        )
        if current >= action.amount:
            return True
        for townhall in self.townhalls.ready:
            for geyser in self.vespene_geyser.closer_than(10, townhall):
                if self.gas_buildings.closer_than(1, geyser):
                    continue
                if not self.can_afford(structure):
                    return False
                worker = self.select_build_worker(geyser.position)
                if worker:
                    worker.build_gas(geyser)
                    self._mark_scheduled(structure)
                    return True
        return False

    async def _train_units(self, action: StrategyAction) -> bool:
        requested = ([action.unit] if action.unit else action.units) + action.fallback_units
        for unit_name in requested:
            if unit_name is None:
                continue
            unit = _unit_type(unit_name.value)
            if not self.can_afford(unit) or self.supply_left <= 0:
                continue
            try:
                if self.tech_requirement_progress(unit) < 1:
                    continue
            except Exception:
                pass
            producer_name = PRODUCER_BY_UNIT[unit_name]
            if producer_name is None:
                trained = False
                for larva in self.larva:
                    if self.can_afford(unit) and self.supply_left > 0:
                        larva.train(unit)
                        trained = True
                if trained:
                    return True
                continue
            producer = _unit_type(producer_name.value)
            trained = False
            for building in self.structures(producer).ready.idle:
                if self.can_afford(unit) and self.supply_left > 0:
                    building.train(unit)
                    trained = True
            if trained:
                return True
        return False

    async def _expand(self, action: StrategyAction) -> bool:
        assert action.structure is not None and action.amount is not None
        townhall = _unit_type(action.structure.value)
        if (
            self.townhalls.amount
            + self.already_pending(townhall)
            + self._scheduled(townhall)
            >= action.amount
        ):
            return True
        if not self.can_afford(townhall):
            return False
        await self.expand_now()
        self._mark_scheduled(townhall)
        return True

    async def _attack(self, action: StrategyAction) -> bool:
        assert action.min_size is not None
        army = self._combine_units(_unit_type(unit.value) for unit in action.units)
        if action.required_unit and action.required_amount:
            required = self.units(_unit_type(action.required_unit.value)).amount
            if required < action.required_amount:
                return False
        if army.amount < action.min_size:
            return False
        target = self.choose_attack_target(army)
        for unit in army.idle:
            unit.attack(target)
        return True

    async def _build_forward(self, action: StrategyAction) -> bool:
        assert action.structure is not None and action.amount is not None
        structure = _unit_type(action.structure.value)
        enemy_start = self.enemy_start_locations[0]
        forward_structures = self.structures(structure).closer_than(35, enemy_start)
        if (
            forward_structures.amount
            + self.already_pending(structure)
            + self._scheduled(structure)
            >= action.amount
        ):
            return True
        if not self.can_afford(structure):
            return False

        anchor = enemy_start.towards(self.game_info.map_center, action.distance)
        if action.structure.value == "PHOTONCANNON":
            pylons = self.structures(UnitTypeId.PYLON).ready.closer_than(35, enemy_start)
            if not pylons:
                return False
            anchor = pylons.closest_to(enemy_start).position
        elif action.structure.value == "BUNKER":
            depots = self.structures(UnitTypeId.SUPPLYDEPOT).ready.closer_than(35, enemy_start)
            if not depots:
                return False
            anchor = depots.closest_to(enemy_start).position
        built = await self.build_structure_near_position(structure, anchor)
        if built:
            self._mark_scheduled(structure)
        return built

    async def _emergency_worker_attack(self, _action: StrategyAction) -> bool:
        if self.townhalls:
            return False
        target = self.choose_attack_target(self.workers)
        for worker in self.workers:
            worker.attack(target)
        return True

    def _combine_units(self, unit_types: Iterable[UnitTypeId]) -> Units:
        army = Units([], self)
        for unit_type in unit_types:
            army = army | self.units(unit_type)
        return army

    def _scheduled(self, structure: UnitTypeId) -> int:
        return self._scheduled_structures.get(structure, 0)

    def _mark_scheduled(self, structure: UnitTypeId) -> None:
        self._scheduled_structures[structure] = self._scheduled(structure) + 1

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
