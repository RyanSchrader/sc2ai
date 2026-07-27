import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, jsonOptions } from "./api";
import AssistantPanel from "./components/AssistantPanel";
import { ForkIcon, TrashIcon } from "./components/ActionIcons";
import BotEditor from "./components/BotEditor";
import RunConsole from "./components/RunConsole";
import StatsPage from "./components/StatsPage";
import {
  blankStrategy,
  type BotRecord,
  type BotSummary,
  type Catalog,
  type Race,
} from "./models";

type Screen =
  | { name: "library"; trash: boolean }
  | { name: "editor"; botId: string }
  | { name: "run"; bot: BotSummary }
  | { name: "stats"; bot: BotSummary };

export default function App() {
  const [screen, setScreen] = useState<Screen>({ name: "library", trash: false });
  const [bots, setBots] = useState<BotSummary[]>([]);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [raceFilter, setRaceFilter] = useState("");
  const [createMode, setCreateMode] = useState<"blank" | "describe" | null>(null);

  const refreshBots = useCallback(async () => {
    try {
      setError(null);
      const result = await api<BotSummary[]>("/bots?includeDeleted=true");
      setBots(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load bots.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshBots();
    void api<Catalog>("/catalog").then(setCatalog).catch(() => undefined);
  }, [refreshBots]);

  const visibleBots = useMemo(() => {
    if (screen.name !== "library") return [];
    const query = search.trim().toLowerCase();
    return bots.filter((bot) => {
      const deletedMatch = screen.trash ? Boolean(bot.deletedAt) : !bot.deletedAt;
      const searchMatch =
        !query ||
        bot.name.toLowerCase().includes(query) ||
        bot.description.toLowerCase().includes(query) ||
        bot.slug.toLowerCase().includes(query) ||
        bot.tags.some((tag) => tag.toLowerCase().includes(query));
      return deletedMatch && searchMatch && (!raceFilter || bot.race === raceFilter);
    });
  }, [bots, raceFilter, screen, search]);

  const editBot = (bot: BotSummary) => setScreen({ name: "editor", botId: bot.id });

  const forkBot = async (bot: BotSummary) => {
    try {
      const fork = await api<BotRecord>(`/bots/${bot.id}/fork`, jsonOptions("POST", {}));
      await refreshBots();
      setScreen({ name: "editor", botId: fork.id });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not fork bot.");
    }
  };

  const trashBot = async (bot: BotSummary) => {
    if (!window.confirm(`Move “${bot.name}” to Trash?`)) return;
    await api(`/bots/${bot.id}`, jsonOptions("DELETE"));
    await refreshBots();
  };

  const restoreBot = async (bot: BotSummary) => {
    await api(`/bots/${bot.id}/restore`, jsonOptions("POST"));
    await refreshBots();
  };

  if (screen.name === "editor") {
    return (
      <BotEditor
        botId={screen.botId}
        catalog={catalog}
        onBack={async () => {
          await refreshBots();
          setScreen({ name: "library", trash: false });
        }}
        onRun={(bot) => setScreen({ name: "run", bot })}
      />
    );
  }

  if (screen.name === "run") {
    return (
      <RunConsole
        bot={screen.bot}
        onBack={() => setScreen({ name: "library", trash: false })}
      />
    );
  }

  if (screen.name === "stats") {
    return (
      <StatsPage
        bot={screen.bot}
        onBack={async () => {
          await refreshBots();
          setScreen({ name: "library", trash: false });
        }}
        onRun={() => setScreen({ name: "run", bot: screen.bot })}
      />
    );
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="brand" onClick={() => setScreen({ name: "library", trash: false })}>
          <span className="brand-mark">S2</span>
          <span>
            <strong>Bot Studio</strong>
            <small>Local strategy workshop</small>
          </span>
        </button>
        <nav>
          <button
            className={!screen.trash ? "nav-active" : ""}
            onClick={() => setScreen({ name: "library", trash: false })}
          >
            Library
          </button>
          <button
            className={screen.trash ? "nav-active" : ""}
            onClick={() => setScreen({ name: "library", trash: true })}
          >
            Trash
          </button>
        </nav>
        <OllamaStatus />
      </header>

      <main className="library-page">
        <section className="hero">
          <div>
            <span className="eyebrow">{screen.trash ? "RECOVERY" : "STRATEGY LIBRARY"}</span>
            <h1>{screen.trash ? "Deleted bots" : "Build, fork, and test your next strategy."}</h1>
            <p>
              {screen.trash
                ? "Restore a strategy to return it to your library."
                : "Every bot is a readable set of phases, triggers, and safe in-game actions."}
            </p>
          </div>
          {!screen.trash && (
            <div className="hero-actions">
              <button className="button secondary" onClick={() => setCreateMode("blank")}>
                + New blank bot
              </button>
              <button className="button primary" onClick={() => setCreateMode("describe")}>
                ✦ Describe a bot
              </button>
            </div>
          )}
        </section>

        {error && <div className="alert error">{error}</div>}

        <section className="toolbar">
          <label className="search-box">
            <span>⌕</span>
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search names, tags, or strategies"
            />
          </label>
          <select value={raceFilter} onChange={(event) => setRaceFilter(event.target.value)}>
            <option value="">All races</option>
            <option value="terran">Terran</option>
            <option value="protoss">Protoss</option>
            <option value="zerg">Zerg</option>
          </select>
          <span className="result-count">{visibleBots.length} bots</span>
        </section>

        {loading ? (
          <div className="empty-state">Loading strategy library…</div>
        ) : visibleBots.length === 0 ? (
          <div className="empty-state">
            <strong>{screen.trash ? "Trash is empty." : "No bots match these filters."}</strong>
          </div>
        ) : (
          <section className="bot-grid">
            {visibleBots.map((bot) => (
              <article className={`bot-card race-${bot.race}`} key={bot.id}>
                <div className="bot-card-top">
                  <span className={`race-badge ${bot.race}`}>{bot.race[0].toUpperCase()}</span>
                  <div className="bot-title">
                    <div className="tag-row">
                      {bot.isBuiltin && <span className="pill">Built-in</span>}
                      {bot.forkedFrom && <span className="pill">Fork</span>}
                      <span className="pill muted">v{bot.currentRevision}</span>
                    </div>
                    <h2>{bot.name}</h2>
                    <code>{bot.slug}</code>
                  </div>
                </div>
                <p>{bot.description || "No description yet."}</p>
                <div className="tags">
                  {bot.tags.slice(0, 4).map((tag) => (
                    <span key={tag}>#{tag}</span>
                  ))}
                </div>
                {!screen.trash && (
                  <div className="card-record">
                    {bot.stats && bot.stats.totalRuns > 0 ? (
                      <>
                        <strong>
                          {bot.stats.wins}–{bot.stats.losses}
                        </strong>
                        <span>
                          {bot.stats.winRate == null
                            ? "No decisive games"
                            : `${Math.round(bot.stats.winRate * 100)}% win rate`}
                        </span>
                      </>
                    ) : (
                      <span>No match history</span>
                    )}
                  </div>
                )}
                <div className="card-actions">
                  {screen.trash ? (
                    <button className="button primary small" onClick={() => void restoreBot(bot)}>
                      Restore
                    </button>
                  ) : (
                    <>
                      <button className="button primary small" onClick={() => editBot(bot)}>
                        Edit
                      </button>
                      <button
                        className="button secondary small"
                        onClick={() => setScreen({ name: "run", bot })}
                      >
                        Run
                      </button>
                      <button
                        className="button secondary small"
                        onClick={() => setScreen({ name: "stats", bot })}
                      >
                        Stats
                      </button>
                      <button
                        aria-label={`Fork ${bot.name}`}
                        className="icon-button"
                        title="Fork"
                        onClick={() => void forkBot(bot)}
                      >
                        <ForkIcon />
                      </button>
                      <button
                        aria-label={`Move ${bot.name} to Trash`}
                        className="icon-button danger"
                        title="Move to Trash"
                        onClick={() => void trashBot(bot)}
                      >
                        <TrashIcon />
                      </button>
                    </>
                  )}
                </div>
              </article>
            ))}
          </section>
        )}
      </main>

      {createMode === "blank" && catalog && (
        <BlankBotModal
          catalog={catalog}
          onClose={() => setCreateMode(null)}
          onCreated={async (bot) => {
            setCreateMode(null);
            await refreshBots();
            setScreen({ name: "editor", botId: bot.id });
          }}
        />
      )}
      {createMode === "describe" && (
        <DescribeBotModal
          onClose={() => setCreateMode(null)}
          onCreated={async (bot) => {
            setCreateMode(null);
            await refreshBots();
            setScreen({ name: "editor", botId: bot.id });
          }}
        />
      )}
    </div>
  );
}

function OllamaStatus() {
  const [status, setStatus] = useState<{
    available: boolean;
    modelInstalled: boolean;
    model: string;
  } | null>(null);

  useEffect(() => {
    void api<typeof status>("/assistant/health").then(setStatus).catch(() => undefined);
  }, []);
  const ready = status?.available && status.modelInstalled;
  return (
    <div className={`status-chip ${ready ? "ready" : "offline"}`}>
      <span />
      {ready ? status?.model : "Ollama offline"}
    </div>
  );
}

function BlankBotModal({
  catalog,
  onClose,
  onCreated,
}: {
  catalog: Catalog;
  onClose: () => void;
  onCreated: (bot: BotRecord) => void;
}) {
  const [name, setName] = useState("Untitled Strategy");
  const [race, setRace] = useState<Race>("protoss");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const bot = await api<BotRecord>(
        "/bots",
        jsonOptions("POST", {
          name,
          description: "",
          race,
          tags: [],
          strategy: blankStrategy(race),
        }),
      );
      onCreated(bot);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create bot.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Create a blank bot" onClose={onClose}>
      <form onSubmit={(event) => void submit(event)} className="stack">
        <label>
          Bot name
          <input value={name} onChange={(event) => setName(event.target.value)} autoFocus />
        </label>
        <label>
          Race
          <select value={race} onChange={(event) => setRace(event.target.value as Race)}>
            {catalog.races.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
        {error && <div className="alert error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="button secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="button primary" disabled={busy || !name.trim()}>
            {busy ? "Creating…" : "Create bot"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function DescribeBotModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (bot: BotRecord) => void;
}) {
  return (
    <Modal title="Describe a new bot" onClose={onClose} wide>
      <p className="muted-copy">
        Describe the build order, timings, army, and triggers. Nothing is saved until you review
        and apply the proposal.
      </p>
      <AssistantPanel mode="create" onApplied={onCreated} />
    </Modal>
  );
}

export function Modal({
  title,
  onClose,
  wide = false,
  children,
}: {
  title: string;
  onClose: () => void;
  wide?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal="true">
        <header>
          <h2>{title}</h2>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>
        {children}
      </section>
    </div>
  );
}
