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
- `requirements.txt`: Python dependency list
