# SC2 Bot Studio

SC2 Bot Studio is a local, desktop-first web application for creating, editing, forking, and testing StarCraft II bots without writing Python. Bot behavior is stored as a validated strategy document made of phases, triggers, and safe burnySC2 actions.

The repository includes eight editable built-in strategies:

- `terran-basic`
- `terran-bunker-rush`
- `protoss-basic`
- `protoss-intermediate`
- `protoss-tower-rush`
- `zerg-basic-1`
- `zerg-basic-2`
- `ryan-zealot-rush` (the migrated legacy one-base four-Gateway all-in)

The original implementations remain in `bot.py` and `Ryan_ZealotRush.py` as historical references. `run_bot.py` now runs the declarative strategies used by the web application.

## Features

- Searchable bot library with race filters and built-in/custom labels
- Blank, fork-based, and Ollama-assisted bot creation
- Drag-reorderable phases and rule cards
- Visual trigger and action editors
- Validated plaintext modifications with preview, assumptions, warnings, apply, and reject
- Immutable revision history with restoration
- Recoverable Trash
- Dynamic discovery of locally installed SC2 maps
- One local match at a time with live logs and stop controls
- Persistent win/loss, opponent, revision, map, and in-game duration history
- Studio bot versus Studio bot matches
- Automatic acceptance of unambiguous surrender messages from SC2 computer opponents
- Expansion-aware army scouting after known enemy targets are destroyed
- Configurable inactivity detection that records stalled games as ties
- Revision-pinned benchmark suites and manual regression comparisons
- Optional two-match parallelism for regression batches
- CLI compatibility for every database-backed bot

## Prerequisites

- macOS with StarCraft II installed
- Python 3.10+
- Node.js 24 LTS (automatically detected from Codex when run inside the app)
- Ollama only if you want plaintext creation/modification

The visual editor and match runner work when Ollama is not installed.

## Setup

```bash
make setup
```

You do not need `nvm` to run the project. The setup command checks for a
compatible Node runtime and uses the Node 24 runtime bundled with Codex when
available. Outside Codex, install Node.js 24 LTS (using `nvm` is optional).
Run `make doctor` to see which runtime will be used.

For the plaintext assistant, install Ollama separately and download the default local model:

```bash
ollama pull qwen3:8b
```

Optional settings can be copied from `.env.example` into `.env` or exported in your shell. The defaults use `http://127.0.0.1:11434` and `qwen3:8b`.

## Development

Run the FastAPI backend and Vite frontend together:

```bash
make dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173).

The API is available at [http://127.0.0.1:8000/api/health](http://127.0.0.1:8000/api/health), and interactive API documentation is available at `/docs`.

## Production-style local run

Build the frontend and serve it from the local FastAPI process:

```bash
make run
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Both modes bind only to the loopback interface.

## CLI

List all active bots:

```bash
.venv/bin/python run_bot.py --list-bots
```

Run a built-in or user-created strategy by slug or UUID:

```bash
.venv/bin/python run_bot.py \
  --bot protoss-intermediate \
  --map AcropolisLE \
  --enemy-race terran \
  --difficulty medium_hard
```

Run two Studio bots against each other:

```bash
.venv/bin/python run_bot.py \
  --bot protoss-intermediate \
  --opponent-bot zerg-basic-2 \
  --map AcropolisLE
```

The CLI and web application read the same SQLite database at `data/studio.db`. Override it with `SC2_STUDIO_DB` or `--database`.

Every declarative CLI and web match is recorded in SQLite. Win rate uses
decisive games only; ties, stopped matches, and technical failures are shown
separately.

## Regression testing

Open a bot's **Stats** screen to create reusable benchmark suites and compare
its current revision with an older revision. Studio bots used as benchmarks are
pinned to an exact revision. Candidate and baseline games use matching maps,
settings, and random seeds.

Regression games run one at a time by default. Selecting two parallel games can
substantially increase resource usage: a computer match launches one SC2
process, while a Studio bot versus Studio bot match launches two.

## Strategy model

A strategy contains ordered phases. Each phase has an activation condition and ordered rules. Rules have:

- a composable `always`, `all`, `any`, `not`, or metric trigger;
- a priority and `continuous`, `once`, or `cooldown` execution policy;
- one or more actions from the registered safe action catalog.

Supported actions cover worker distribution, worker/supply/gas targets, construction, unit production with fallbacks, expansion, forward construction, army attacks, and emergency worker attacks. The runtime never evaluates source code from the database or from a model.

Every assistant response is constrained to the same Pydantic JSON schema used by the visual editor and runtime. A proposal must validate and be explicitly applied before it creates a revision.

## Tests

```bash
make test
```

Backend tests validate all eight built-in strategies, race safety, persistence,
revisions, match telemetry, regression scheduling, forks, Trash, and API
operations. Frontend tests cover strategy helpers, bot-v-bot setup, benchmark
configuration, and regression controls.

## Local data

SQLite data, generated frontend files, virtual environments, and logs are ignored by Git. Deleting a bot in the UI only moves it to Trash. Built-in fixtures live in `strategies/builtin_bots.json` and seed idempotently when the database is first created.
