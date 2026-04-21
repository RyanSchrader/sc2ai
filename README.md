# StarCraft II Multi-Bot Starter (Python)

This project now includes seven custom bots built with `burnysc2`:

- `terran-basic`: Basic Terran marine macro bot
- `terran-bunker-rush`: Terran bunker-rush bot (forward Depot + Bunkers + Marines)
- `protoss-basic`: Simple Protoss macro + Zealot attack bot
- `protoss-intermediate`: Stronger Protoss macro with Cybernetics Core + Stalker/Zealot mix
- `zerg-basic-1`: Basic Zergling swarm bot
- `zerg-basic-2`: Basic Roach-focused Zerg bot
- `protoss-tower-rush`: Protoss tower rush bot (forward Pylon + Photon Cannons)

## 1. Prerequisites

- StarCraft II installed
- Python 3.10+ recommended

## 2. Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Run

Default run:

```bash
python run_bot.py
```

Run a specific bot:

```bash
python run_bot.py --bot terran-basic
python run_bot.py --bot terran-bunker-rush
python run_bot.py --bot protoss-basic
python run_bot.py --bot protoss-intermediate
python run_bot.py --bot zerg-basic-1
python run_bot.py --bot zerg-basic-2
python run_bot.py --bot protoss-tower-rush
```

Optional opponent settings:

```bash
python run_bot.py --bot protoss-intermediate --enemy-race terran --difficulty medium_hard
```

Optional map override:

```bash
python run_bot.py --bot zerg-basic-2 --map AbyssalReefLE
```

If map loading fails, edit `MAP_CANDIDATES` in `run_bot.py` to match maps on your machine.

## 4. Files

- `bot.py`: Bot behaviors (`TerranBasicBot`, `TerranBunkerRushBot`, `ProtossBasicBot`, `ProtossIntermediateBot`, `ZergBasicSwarmBot`, `ZergBasicRoachBot`, `ProtossTowerRushBot`)
- `run_bot.py`: Match setup + CLI bot/opponent selection
- `Ryan_ZealotRush.py`: Legacy one-base 4-gate Zealot rush bot (kept for historical reference)
- `requirements.txt`: Python dependency list

## 5. Legacy Reference Bot

`Ryan_ZealotRush.py` is your original manually-written Protoss all-in bot and is intentionally kept in this repo for reference.

Behavior summary:
- One-base 4-Gate Zealot all-in
- Builds probes to 16 workers per Nexus
- Builds up to 4 Gateways, then continuously trains Zealots
- Attacks once 8 Zealots are ready
- Pulls workers to attack if the Nexus dies

How to run it directly:

```bash
python Ryan_ZealotRush.py
python Ryan_ZealotRush.py zerg
python Ryan_ZealotRush.py terran
python Ryan_ZealotRush.py protoss
python Ryan_ZealotRush.py random
```

Notes:
- This script is not wired into `run_bot.py` on purpose, so modern bot selection stays clean.
- It uses older `burnysc2` idioms (`from sc2.constants import *`, `self.do(...)`) and a fixed map string (`"(2)CatalystLE"`), so behavior can differ depending on your local SC2/maps setup.
- The script already includes a TODO noting a gateway overbuild window due to counting only fully completed Gateways.
