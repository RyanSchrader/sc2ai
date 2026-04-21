from sc2.bot_ai import BotAI
from sc2.ids.unit_typeid import UnitTypeId
from sc2.units import Units


class MacroCombatBot(BotAI):
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
        self, structure_type: UnitTypeId, distance: int, placement_step: int = 2
    ) -> bool:
        if not self.townhalls:
            return False

        near_position = self.townhalls.first.position.towards(self.game_info.map_center, distance)
        return await self.build_structure_near_position(
            structure_type=structure_type,
            near_position=near_position,
            placement_step=placement_step,
        )

    async def build_gas_buildings(self, gas_structure: UnitTypeId, max_total: int) -> None:
        if self.gas_buildings.amount + self.already_pending(gas_structure) >= max_total:
            return

        for townhall in self.townhalls.ready:
            for geyser in self.vespene_geyser.closer_than(10, townhall):
                if self.gas_buildings.closer_than(1, geyser):
                    continue
                if not self.can_afford(gas_structure):
                    return
                worker = self.select_build_worker(geyser.position)
                if worker:
                    worker.build_gas(geyser)
                    return

    def choose_attack_target(self, army: Units):
        if self.enemy_units:
            return self.enemy_units.closest_to(army.center)
        if self.enemy_structures:
            return self.enemy_structures.closest_to(army.center)
        return self.enemy_start_locations[0]

    def attack_with_idle_army(self, army: Units, min_size: int) -> None:
        if army.amount < min_size:
            return

        target = self.choose_attack_target(army)
        for unit in army.idle:
            unit.attack(target)


class ProtossBasicBot(MacroCombatBot):
    async def on_step(self, iteration: int) -> None:
        if iteration == 0:
            await self.chat_send("GL HF - ProtossBasicBot online.")

        await self.distribute_workers()
        await self.build_probes()
        await self.build_pylons()
        await self.build_gateways()
        await self.build_assimilators()
        await self.train_zealots()
        await self.expand()
        self.attack_with_idle_army(self.units(UnitTypeId.ZEALOT), min_size=12)

    async def build_probes(self) -> None:
        if self.supply_workers >= 50:
            return
        for nexus in self.townhalls.ready.idle:
            if self.can_afford(UnitTypeId.PROBE):
                nexus.train(UnitTypeId.PROBE)

    async def build_pylons(self) -> None:
        if self.supply_left >= 5:
            return
        if self.already_pending(UnitTypeId.PYLON) > 0:
            return
        if not self.can_afford(UnitTypeId.PYLON):
            return
        await self.build_structure_near_main(UnitTypeId.PYLON, distance=6)

    async def build_gateways(self) -> None:
        if not self.structures(UnitTypeId.PYLON).ready:
            return
        if self.structures(UnitTypeId.GATEWAY).amount + self.already_pending(UnitTypeId.GATEWAY) >= 2:
            return
        if not self.can_afford(UnitTypeId.GATEWAY):
            return
        await self.build_structure_near_main(UnitTypeId.GATEWAY, distance=8, placement_step=3)

    async def build_assimilators(self) -> None:
        desired_assimilators = 2 if self.supply_workers >= 24 else 1
        await self.build_gas_buildings(UnitTypeId.ASSIMILATOR, max_total=desired_assimilators)

    async def train_zealots(self) -> None:
        for gateway in self.structures(UnitTypeId.GATEWAY).ready.idle:
            if self.can_afford(UnitTypeId.ZEALOT) and self.supply_left > 0:
                gateway.train(UnitTypeId.ZEALOT)

    async def expand(self) -> None:
        desired_bases = 2 if self.supply_workers >= 22 else 1
        if self.townhalls.amount + self.already_pending(UnitTypeId.NEXUS) >= desired_bases:
            return
        if self.can_afford(UnitTypeId.NEXUS):
            await self.expand_now()


class ProtossIntermediateBot(MacroCombatBot):
    async def on_step(self, iteration: int) -> None:
        if iteration == 0:
            await self.chat_send("GL HF - ProtossIntermediateBot online.")

        await self.distribute_workers()
        await self.build_probes()
        await self.build_pylons()
        await self.build_gateways()
        await self.build_cybernetics_core()
        await self.build_assimilators()
        await self.train_gateway_army()
        await self.expand()
        self.attack_with_idle_army(
            self.units(UnitTypeId.ZEALOT) | self.units(UnitTypeId.STALKER),
            min_size=20,
        )

    async def build_probes(self) -> None:
        if self.supply_workers >= 65:
            return
        for nexus in self.townhalls.ready.idle:
            if self.can_afford(UnitTypeId.PROBE):
                nexus.train(UnitTypeId.PROBE)

    async def build_pylons(self) -> None:
        if self.supply_left >= 7 or self.supply_cap >= 200:
            return
        if self.already_pending(UnitTypeId.PYLON) > 0:
            return
        if not self.can_afford(UnitTypeId.PYLON):
            return
        await self.build_structure_near_main(UnitTypeId.PYLON, distance=6)

    async def build_gateways(self) -> None:
        if not self.structures(UnitTypeId.PYLON).ready:
            return

        desired_gateways = 2
        if self.townhalls.amount >= 2:
            desired_gateways = 4
        if self.supply_workers >= 46:
            desired_gateways = 5

        total_gateways = self.structures(UnitTypeId.GATEWAY).amount + self.already_pending(UnitTypeId.GATEWAY)
        if total_gateways >= desired_gateways:
            return
        if not self.can_afford(UnitTypeId.GATEWAY):
            return
        await self.build_structure_near_main(UnitTypeId.GATEWAY, distance=9, placement_step=3)

    async def build_cybernetics_core(self) -> None:
        if not self.structures(UnitTypeId.GATEWAY).ready:
            return
        if self.structures(UnitTypeId.CYBERNETICSCORE) or self.already_pending(UnitTypeId.CYBERNETICSCORE):
            return
        if not self.can_afford(UnitTypeId.CYBERNETICSCORE):
            return
        await self.build_structure_near_main(UnitTypeId.CYBERNETICSCORE, distance=9, placement_step=3)

    async def build_assimilators(self) -> None:
        desired_assimilators = 2
        if self.townhalls.amount >= 2:
            desired_assimilators = 4
        await self.build_gas_buildings(UnitTypeId.ASSIMILATOR, max_total=desired_assimilators)

    async def train_gateway_army(self) -> None:
        core_ready = self.structures(UnitTypeId.CYBERNETICSCORE).ready
        for gateway in self.structures(UnitTypeId.GATEWAY).ready.idle:
            if core_ready and self.can_afford(UnitTypeId.STALKER) and self.supply_left > 0:
                gateway.train(UnitTypeId.STALKER)
            elif self.can_afford(UnitTypeId.ZEALOT) and self.supply_left > 0:
                gateway.train(UnitTypeId.ZEALOT)

    async def expand(self) -> None:
        desired_bases = 1
        if self.supply_workers >= 22:
            desired_bases = 2
        if self.supply_workers >= 48:
            desired_bases = 3
        if self.townhalls.amount + self.already_pending(UnitTypeId.NEXUS) >= desired_bases:
            return
        if self.can_afford(UnitTypeId.NEXUS):
            await self.expand_now()


class ZergBasicSwarmBot(MacroCombatBot):
    async def on_step(self, iteration: int) -> None:
        if iteration == 0:
            await self.chat_send("GL HF - ZergBasicSwarmBot online.")

        await self.distribute_workers()
        await self.train_overlords()
        await self.train_drones()
        await self.build_spawning_pool()
        await self.build_extractors()
        await self.train_zerglings()
        await self.expand()
        self.attack_with_idle_army(self.units(UnitTypeId.ZERGLING), min_size=35)

    async def train_overlords(self) -> None:
        if self.supply_left >= 3 or self.supply_cap >= 200:
            return
        if self.already_pending(UnitTypeId.OVERLORD) > 0:
            return
        for larva in self.larva:
            if self.can_afford(UnitTypeId.OVERLORD):
                larva.train(UnitTypeId.OVERLORD)
                return

    async def train_drones(self) -> None:
        if self.supply_workers >= 55:
            return
        for larva in self.larva:
            if self.can_afford(UnitTypeId.DRONE) and self.supply_left > 0:
                larva.train(UnitTypeId.DRONE)

    async def build_spawning_pool(self) -> None:
        if self.structures(UnitTypeId.SPAWNINGPOOL) or self.already_pending(UnitTypeId.SPAWNINGPOOL):
            return
        if not self.can_afford(UnitTypeId.SPAWNINGPOOL):
            return
        await self.build_structure_near_main(UnitTypeId.SPAWNINGPOOL, distance=5)

    async def build_extractors(self) -> None:
        await self.build_gas_buildings(UnitTypeId.EXTRACTOR, max_total=1)

    async def train_zerglings(self) -> None:
        if not self.structures(UnitTypeId.SPAWNINGPOOL).ready:
            return
        for larva in self.larva:
            if self.can_afford(UnitTypeId.ZERGLING) and self.supply_left >= 2:
                larva.train(UnitTypeId.ZERGLING)

    async def expand(self) -> None:
        desired_bases = 1
        if self.supply_workers >= 18:
            desired_bases = 2
        if self.supply_workers >= 35:
            desired_bases = 3
        if self.townhalls.amount + self.already_pending(UnitTypeId.HATCHERY) >= desired_bases:
            return
        if self.can_afford(UnitTypeId.HATCHERY):
            await self.expand_now()


class ZergBasicRoachBot(MacroCombatBot):
    async def on_step(self, iteration: int) -> None:
        if iteration == 0:
            await self.chat_send("GL HF - ZergBasicRoachBot online.")

        await self.distribute_workers()
        await self.train_overlords()
        await self.train_drones()
        await self.build_spawning_pool()
        await self.build_extractors()
        await self.build_roach_warren()
        await self.train_roaches()
        await self.expand()
        self.attack_army()

    async def train_overlords(self) -> None:
        if self.supply_left >= 3 or self.supply_cap >= 200:
            return
        if self.already_pending(UnitTypeId.OVERLORD) > 0:
            return
        for larva in self.larva:
            if self.can_afford(UnitTypeId.OVERLORD):
                larva.train(UnitTypeId.OVERLORD)
                return

    async def train_drones(self) -> None:
        if self.supply_workers >= 60:
            return
        for larva in self.larva:
            if self.can_afford(UnitTypeId.DRONE) and self.supply_left > 0:
                larva.train(UnitTypeId.DRONE)

    async def build_spawning_pool(self) -> None:
        if self.structures(UnitTypeId.SPAWNINGPOOL) or self.already_pending(UnitTypeId.SPAWNINGPOOL):
            return
        if not self.can_afford(UnitTypeId.SPAWNINGPOOL):
            return
        await self.build_structure_near_main(UnitTypeId.SPAWNINGPOOL, distance=5)

    async def build_extractors(self) -> None:
        desired_extractors = 2 if self.townhalls.amount == 1 else 4
        await self.build_gas_buildings(UnitTypeId.EXTRACTOR, max_total=desired_extractors)

    async def build_roach_warren(self) -> None:
        if not self.structures(UnitTypeId.SPAWNINGPOOL).ready:
            return
        if self.structures(UnitTypeId.ROACHWARREN) or self.already_pending(UnitTypeId.ROACHWARREN):
            return
        if not self.can_afford(UnitTypeId.ROACHWARREN):
            return
        await self.build_structure_near_main(UnitTypeId.ROACHWARREN, distance=6)

    async def train_roaches(self) -> None:
        spawning_pool_ready = self.structures(UnitTypeId.SPAWNINGPOOL).ready
        roach_warren_ready = self.structures(UnitTypeId.ROACHWARREN).ready
        for larva in self.larva:
            if roach_warren_ready and self.can_afford(UnitTypeId.ROACH) and self.supply_left > 0:
                larva.train(UnitTypeId.ROACH)
            elif spawning_pool_ready and self.can_afford(UnitTypeId.ZERGLING) and self.supply_left >= 2:
                larva.train(UnitTypeId.ZERGLING)

    async def expand(self) -> None:
        desired_bases = 1
        if self.supply_workers >= 20:
            desired_bases = 2
        if self.supply_workers >= 45:
            desired_bases = 3
        if self.townhalls.amount + self.already_pending(UnitTypeId.HATCHERY) >= desired_bases:
            return
        if self.can_afford(UnitTypeId.HATCHERY):
            await self.expand_now()

    def attack_army(self) -> None:
        roaches = self.units(UnitTypeId.ROACH)
        army = roaches | self.units(UnitTypeId.ZERGLING)
        if roaches.amount < 10:
            return
        self.attack_with_idle_army(army, min_size=20)


class TerranBasicBot(MacroCombatBot):
    async def on_step(self, iteration: int) -> None:
        if iteration == 0:
            await self.chat_send("GL HF - TerranBasicBot online.")

        await self.distribute_workers()
        await self.build_scvs()
        await self.build_supply_depots()
        await self.build_barracks()
        await self.build_refineries()
        await self.train_marines()
        await self.expand()
        self.attack_with_idle_army(self.units(UnitTypeId.MARINE), min_size=16)

    async def build_scvs(self) -> None:
        if self.supply_workers >= 70:
            return
        for command_center in self.townhalls.ready.idle:
            if self.can_afford(UnitTypeId.SCV):
                command_center.train(UnitTypeId.SCV)

    async def build_supply_depots(self) -> None:
        if self.supply_left >= 6 or self.supply_cap >= 200:
            return
        if self.already_pending(UnitTypeId.SUPPLYDEPOT) > 0:
            return
        if not self.can_afford(UnitTypeId.SUPPLYDEPOT):
            return
        await self.build_structure_near_main(UnitTypeId.SUPPLYDEPOT, distance=6)

    async def build_barracks(self) -> None:
        if not self.structures(UnitTypeId.SUPPLYDEPOT).ready:
            return
        desired_barracks = 2 if self.townhalls.amount == 1 else 4
        total_barracks = self.structures(UnitTypeId.BARRACKS).amount + self.already_pending(UnitTypeId.BARRACKS)
        if total_barracks >= desired_barracks:
            return
        if not self.can_afford(UnitTypeId.BARRACKS):
            return
        await self.build_structure_near_main(UnitTypeId.BARRACKS, distance=11, placement_step=3)

    async def build_refineries(self) -> None:
        desired_refineries = 2 if self.townhalls.amount == 1 else 4
        await self.build_gas_buildings(UnitTypeId.REFINERY, max_total=desired_refineries)

    async def train_marines(self) -> None:
        for barracks in self.structures(UnitTypeId.BARRACKS).ready.idle:
            if self.can_afford(UnitTypeId.MARINE) and self.supply_left > 0:
                barracks.train(UnitTypeId.MARINE)

    async def expand(self) -> None:
        desired_bases = 1
        if self.supply_workers >= 20:
            desired_bases = 2
        if self.supply_workers >= 45:
            desired_bases = 3
        if self.townhalls.amount + self.already_pending(UnitTypeId.COMMANDCENTER) >= desired_bases:
            return
        if self.can_afford(UnitTypeId.COMMANDCENTER):
            await self.expand_now()


class ProtossTowerRushBot(MacroCombatBot):
    async def on_step(self, iteration: int) -> None:
        if iteration == 0:
            await self.chat_send("GL HF - ProtossTowerRushBot online.")

        await self.distribute_workers()
        await self.build_probes()
        await self.build_pylons_for_supply()
        await self.build_gateway()
        await self.train_zealots()
        await self.build_forward_pylon()
        await self.build_photon_cannons()
        self.attack_with_idle_army(self.units(UnitTypeId.ZEALOT), min_size=6)

    async def build_probes(self) -> None:
        if self.supply_workers >= 32:
            return
        for nexus in self.townhalls.ready.idle:
            if self.can_afford(UnitTypeId.PROBE):
                nexus.train(UnitTypeId.PROBE)

    async def build_pylons_for_supply(self) -> None:
        if self.supply_left >= 4 or self.supply_cap >= 200:
            return
        if self.already_pending(UnitTypeId.PYLON) > 0:
            return
        if not self.can_afford(UnitTypeId.PYLON):
            return
        await self.build_structure_near_main(UnitTypeId.PYLON, distance=6)

    async def build_gateway(self) -> None:
        if not self.structures(UnitTypeId.PYLON).ready:
            return
        if self.structures(UnitTypeId.GATEWAY) or self.already_pending(UnitTypeId.GATEWAY):
            return
        if not self.can_afford(UnitTypeId.GATEWAY):
            return
        await self.build_structure_near_main(UnitTypeId.GATEWAY, distance=8, placement_step=3)

    async def train_zealots(self) -> None:
        for gateway in self.structures(UnitTypeId.GATEWAY).ready.idle:
            if self.can_afford(UnitTypeId.ZEALOT) and self.supply_left > 0:
                gateway.train(UnitTypeId.ZEALOT)

    async def build_forward_pylon(self) -> None:
        enemy_start = self.enemy_start_locations[0]
        if self.structures(UnitTypeId.PYLON).closer_than(30, enemy_start).amount >= 1:
            return
        if self.already_pending(UnitTypeId.PYLON) > 0:
            return
        if not self.can_afford(UnitTypeId.PYLON):
            return

        forward_position = enemy_start.towards(self.game_info.map_center, 8)
        await self.build_structure_near_position(UnitTypeId.PYLON, forward_position, placement_step=2)

    async def build_photon_cannons(self) -> None:
        enemy_start = self.enemy_start_locations[0]
        forward_pylons = self.structures(UnitTypeId.PYLON).ready.closer_than(30, enemy_start)
        if not forward_pylons:
            return

        desired_cannons = 3 if self.supply_workers < 24 else 6
        current_cannons = self.structures(UnitTypeId.PHOTONCANNON).amount + self.already_pending(
            UnitTypeId.PHOTONCANNON
        )
        if current_cannons >= desired_cannons:
            return
        if not self.can_afford(UnitTypeId.PHOTONCANNON):
            return

        anchor = forward_pylons.closest_to(enemy_start).position
        await self.build_structure_near_position(UnitTypeId.PHOTONCANNON, anchor, placement_step=2)


class TerranBunkerRushBot(MacroCombatBot):
    async def on_step(self, iteration: int) -> None:
        if iteration == 0:
            await self.chat_send("GL HF - TerranBunkerRushBot online.")

        await self.distribute_workers()
        await self.build_scvs()
        await self.build_supply_depots()
        await self.build_barracks()
        await self.train_marines()
        await self.build_forward_supply_depot()
        await self.build_forward_bunkers()
        self.attack_with_idle_army(self.units(UnitTypeId.MARINE), min_size=10)

    async def build_scvs(self) -> None:
        if self.supply_workers >= 30:
            return
        for command_center in self.townhalls.ready.idle:
            if self.can_afford(UnitTypeId.SCV):
                command_center.train(UnitTypeId.SCV)

    async def build_supply_depots(self) -> None:
        if self.supply_left >= 4 or self.supply_cap >= 200:
            return
        if self.already_pending(UnitTypeId.SUPPLYDEPOT) > 0:
            return
        if not self.can_afford(UnitTypeId.SUPPLYDEPOT):
            return
        await self.build_structure_near_main(UnitTypeId.SUPPLYDEPOT, distance=6)

    async def build_barracks(self) -> None:
        if not self.structures(UnitTypeId.SUPPLYDEPOT).ready:
            return
        desired_barracks = 2
        total_barracks = self.structures(UnitTypeId.BARRACKS).amount + self.already_pending(UnitTypeId.BARRACKS)
        if total_barracks >= desired_barracks:
            return
        if not self.can_afford(UnitTypeId.BARRACKS):
            return
        await self.build_structure_near_main(UnitTypeId.BARRACKS, distance=9, placement_step=3)

    async def train_marines(self) -> None:
        for barracks in self.structures(UnitTypeId.BARRACKS).ready.idle:
            if self.can_afford(UnitTypeId.MARINE) and self.supply_left > 0:
                barracks.train(UnitTypeId.MARINE)

    async def build_forward_supply_depot(self) -> None:
        if not self.structures(UnitTypeId.BARRACKS).ready:
            return
        enemy_start = self.enemy_start_locations[0]
        if self.structures(UnitTypeId.SUPPLYDEPOT).closer_than(35, enemy_start).ready:
            return
        if self.already_pending(UnitTypeId.SUPPLYDEPOT) > 0:
            return
        if not self.can_afford(UnitTypeId.SUPPLYDEPOT):
            return

        forward_position = enemy_start.towards(self.game_info.map_center, 10)
        await self.build_structure_near_position(UnitTypeId.SUPPLYDEPOT, forward_position, placement_step=2)

    async def build_forward_bunkers(self) -> None:
        if not self.structures(UnitTypeId.BARRACKS).ready:
            return

        enemy_start = self.enemy_start_locations[0]
        has_forward_depot = self.structures(UnitTypeId.SUPPLYDEPOT).ready.closer_than(35, enemy_start)
        if not has_forward_depot:
            return

        desired_bunkers = 2 if self.units(UnitTypeId.MARINE).amount < 12 else 3
        current_bunkers = self.structures(UnitTypeId.BUNKER).amount + self.already_pending(UnitTypeId.BUNKER)
        if current_bunkers >= desired_bunkers:
            return
        if not self.can_afford(UnitTypeId.BUNKER):
            return

        forward_anchor = has_forward_depot.closest_to(enemy_start).position
        await self.build_structure_near_position(UnitTypeId.BUNKER, forward_anchor, placement_step=2)
