import random
import sys
import argparse

import sc2
from sc2 import Race, Difficulty
from sc2.constants import *
from sc2.ids.buff_id import BuffId
from sc2.player import Bot, Computer

# A one-base 4-gate zealot all-in
class Ryan_ZealotRush(sc2.BotAI):

    def select_target(self, state):
        return self.enemy_start_locations[0]
    
    async def on_step(self, iteration):
        if iteration == 0:
            await self.chat_send("(glhf)")
        
        await self.distribute_workers()

        # Send workers to attack if our nexus is non-existent
        if not self.units(NEXUS).ready.exists:
            for worker in self.workers:
                await self.do(worker.attack(self.enemy_start_locations[0]))
            return
        else:
            nexus = self.units(NEXUS).ready.random

        # Build a pylon when we begin running out of supply
        if self.supply_left < 4 and not self.already_pending(PYLON):
            if self.can_afford(PYLON):
                await self.build(PYLON, near=nexus)
            return

        # Build probes until first base minerals are saturated 
        if self.workers.amount < self.units(NEXUS).amount*16 and nexus.noqueue:
            if self.can_afford(PROBE):
                await self.do(nexus.train(PROBE))

        # Build 4 gateways
        if self.units(PYLON).ready.exists: 
            pylon = self.units(PYLON).ready.random
            if self.can_afford(GATEWAY) and self.units(GATEWAY).amount < 4:  # TODO: this is bugged - .amount only accounts for fully built buildings, so there's a window where we may end up building even more gateways
                await self.build(GATEWAY, near=pylon)

        # Build zealots continously 
        for gw in self.units(GATEWAY).ready.noqueue:
            if self.can_afford(ZEALOT):
                await self.do(gw.train(ZEALOT))

        # Send zealots to attack once the first 8 are completed
        if self.units(ZEALOT).amount >= 8:
            for zealot in self.units(ZEALOT).ready.idle:
                await self.do(zealot.attack(self.select_target(self.state)))

def GetComputerRace(x):
    return {
        'z':       Race.Zerg,
        'zerg':    Race.Zerg,
        'p':       Race.Protoss,
        'protoss': Race.Protoss,
        't':       Race.Terran,
        'terran':  Race.Terran,
        'r':       Race.Random,
        'random':  Race.Random
    }.get(x, Race.Random)

def main():
    parser = argparse.ArgumentParser()    
    parser.add_argument('computerRace', type=str, nargs='?', default='r', help='the computer\'s race (one of t/p/z/r)')
    args = parser.parse_args()
    compRace = GetComputerRace(args.computerRace)

    sc2.run_game(sc2.maps.get("(2)CatalystLE"), [
    #sc2.run_game(sc2.maps.get("TritonLE"), [
        Bot(Race.Protoss, Ryan_ZealotRush()),
        Computer(compRace, Difficulty.Hard)
    ], realtime=False)

if __name__ == '__main__':
    main()