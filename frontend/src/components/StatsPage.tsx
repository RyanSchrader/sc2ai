import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, jsonOptions } from "../api";
import type {
  BenchmarkScenario,
  BenchmarkSuite,
  BotStats,
  BotSummary,
  MatchRecord,
  RegressionBatch,
} from "../models";

interface Revision {
  id: string;
  number: number;
  summary: string;
  created_at: string;
}

interface MatchPage {
  items: MatchRecord[];
  total: number;
}

interface SuiteDraft {
  id: string | null;
  name: string;
  description: string;
  scenarios: BenchmarkScenario[];
}

const emptyScenario = (mapName = ""): BenchmarkScenario => ({
  name: "Computer benchmark",
  mapName,
  opponentType: "computer",
  enemyRace: "zerg",
  difficulty: "medium",
  opponentBotId: null,
  opponentRevision: null,
});

export default function StatsPage({
  bot,
  onBack,
  onRun,
}: {
  bot: BotSummary;
  onBack: () => void;
  onRun: () => void;
}) {
  const [stats, setStats] = useState<BotStats | null>(null);
  const [matches, setMatches] = useState<MatchRecord[]>([]);
  const [matchTotal, setMatchTotal] = useState(0);
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [suites, setSuites] = useState<BenchmarkSuite[]>([]);
  const [regressions, setRegressions] = useState<RegressionBatch[]>([]);
  const [maps, setMaps] = useState<string[]>([]);
  const [bots, setBots] = useState<BotSummary[]>([]);
  const [includeRegression, setIncludeRegression] = useState(true);
  const [opponentFilter, setOpponentFilter] = useState("");
  const [resultFilter, setResultFilter] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [suiteDraft, setSuiteDraft] = useState<SuiteDraft | null>(null);
  const [baselineRevision, setBaselineRevision] = useState<number | null>(null);
  const [suiteId, setSuiteId] = useState("");
  const [gamesPerScenario, setGamesPerScenario] = useState(3);
  const [concurrency, setConcurrency] = useState(1);
  const [activeBatch, setActiveBatch] = useState<RegressionBatch | null>(null);
  const regressionEvents = useRef<EventSource | null>(null);

  const loadCore = useCallback(async () => {
    const [statsResult, revisionResult, suiteResult, regressionResult, mapResult, botResult] =
      await Promise.all([
        api<BotStats>(
          `/bots/${bot.id}/stats?includeRegression=${includeRegression}`,
        ),
        api<Revision[]>(`/bots/${bot.id}/revisions`),
        api<BenchmarkSuite[]>("/benchmarks"),
        api<RegressionBatch[]>(`/bots/${bot.id}/regressions`),
        api<{ maps: string[] }>("/runtime/maps"),
        api<BotSummary[]>("/bots"),
      ]);
    setStats(statsResult);
    setRevisions(revisionResult);
    setSuites(suiteResult);
    setRegressions(regressionResult);
    setMaps(mapResult.maps);
    setBots(botResult.filter((item) => item.id !== bot.id));
    setBaselineRevision(
      (current) =>
        current ??
        revisionResult.find((revision) => revision.number < bot.currentRevision)?.number ??
        null,
    );
    setSuiteId((current) => current || suiteResult[0]?.id || "");
  }, [bot.currentRevision, bot.id, includeRegression]);

  const loadMatches = useCallback(async () => {
    const query = new URLSearchParams({
      includeRegression: String(includeRegression),
      limit: "100",
    });
    if (opponentFilter) query.set("opponentType", opponentFilter);
    if (resultFilter) query.set("result", resultFilter);
    const result = await api<MatchPage>(`/bots/${bot.id}/matches?${query}`);
    setMatches(result.items);
    setMatchTotal(result.total);
  }, [bot.id, includeRegression, opponentFilter, resultFilter]);

  const reload = useCallback(async () => {
    try {
      setError(null);
      await Promise.all([loadCore(), loadMatches()]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load statistics.");
    }
  }, [loadCore, loadMatches]);

  useEffect(() => {
    void reload();
    return () => regressionEvents.current?.close();
  }, [reload]);

  const saveSuite = async () => {
    if (!suiteDraft) return;
    setBusy(true);
    setError(null);
    const body = {
      name: suiteDraft.name,
      description: suiteDraft.description,
      scenarios: suiteDraft.scenarios.map((scenario) => ({
        name: scenario.name,
        map_name: scenario.mapName,
        opponent_type: scenario.opponentType,
        enemy_race: scenario.enemyRace ?? "zerg",
        difficulty: scenario.difficulty ?? "easy",
        opponent_bot_id: scenario.opponentBotId,
        opponent_revision: scenario.opponentRevision,
      })),
    };
    try {
      const saved = suiteDraft.id
        ? await api<BenchmarkSuite>(
            `/benchmarks/${suiteDraft.id}`,
            jsonOptions("PUT", body),
          )
        : await api<BenchmarkSuite>("/benchmarks", jsonOptions("POST", body));
      setSuiteDraft(null);
      setNotice(`Saved benchmark suite “${saved.name}”.`);
      await loadCore();
      setSuiteId(saved.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save suite.");
    } finally {
      setBusy(false);
    }
  };

  const duplicateSuite = async (suite: BenchmarkSuite) => {
    setBusy(true);
    try {
      const copy = await api<BenchmarkSuite>(
        `/benchmarks/${suite.id}/duplicate`,
        jsonOptions("POST"),
      );
      await loadCore();
      setNotice(`Created “${copy.name}”.`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not duplicate suite.");
    } finally {
      setBusy(false);
    }
  };

  const archiveSuite = async (suite: BenchmarkSuite) => {
    if (!window.confirm(`Archive “${suite.name}”? Existing regression results remain.`)) {
      return;
    }
    setBusy(true);
    try {
      await api(`/benchmarks/${suite.id}`, jsonOptions("DELETE"));
      await loadCore();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not archive suite.");
    } finally {
      setBusy(false);
    }
  };

  const startRegression = async () => {
    if (!baselineRevision || !suiteId) return;
    setBusy(true);
    setError(null);
    try {
      const batch = await api<RegressionBatch>(
        "/regressions",
        jsonOptions("POST", {
          bot_id: bot.id,
          baseline_revision: baselineRevision,
          suite_id: suiteId,
          games_per_scenario: gamesPerScenario,
          concurrency,
        }),
      );
      setActiveBatch(batch);
      watchBatch(batch.id);
      await loadCore();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start regression.");
    } finally {
      setBusy(false);
    }
  };

  const watchBatch = (batchId: string) => {
    regressionEvents.current?.close();
    const source = new EventSource(`/api/regressions/${batchId}/events`);
    regressionEvents.current = source;
    source.onmessage = (event) => {
      const payload = JSON.parse(event.data) as RegressionBatch & { type: string };
      setActiveBatch(payload);
      if (["completed", "cancelled", "failed", "interrupted"].includes(payload.status)) {
        source.close();
        void reload();
      }
    };
    source.onerror = () => {
      source.close();
      void api<RegressionBatch>(`/regressions/${batchId}`).then(setActiveBatch);
    };
  };

  const cancelRegression = async () => {
    if (!activeBatch) return;
    setBusy(true);
    try {
      const result = await api<RegressionBatch>(
        `/regressions/${activeBatch.id}/cancel`,
        jsonOptions("POST"),
      );
      setActiveBatch(result);
      regressionEvents.current?.close();
      await reload();
    } finally {
      setBusy(false);
    }
  };

  const selectedSuite = suites.find((suite) => suite.id === suiteId);
  const estimatedGames = (selectedSuite?.scenarios.length ?? 0) * gamesPerScenario * 2;
  const decisive = stats ? stats.wins + stats.losses : 0;

  return (
    <div className="stats-page">
      <header className="editor-topbar">
        <button className="button ghost" onClick={onBack}>
          ← Library
        </button>
        <div className="editor-title">
          <strong>Performance</strong>
          <span>
            {bot.name} · current v{bot.currentRevision}
          </span>
        </div>
        <button className="button primary" onClick={onRun}>
          Run match
        </button>
      </header>

      <main className="stats-layout">
        <section className="stats-hero">
          <div>
            <span className="eyebrow">MATCH ANALYTICS</span>
            <h1>Track results. Catch regressions.</h1>
            <p>
              Win rate uses decisive games only. Ties, stopped runs, and technical failures
              remain visible without being counted as losses.
            </p>
          </div>
          <label className="check-row">
            <input
              type="checkbox"
              checked={includeRegression}
              onChange={(event) => setIncludeRegression(event.target.checked)}
            />
            Include regression matches
          </label>
        </section>

        {error && <div className="alert error">{error}</div>}
        {notice && <div className="alert success">{notice}</div>}

        <section className="metric-grid">
          <Metric label="Decisive win rate" value={formatPercent(stats?.winRate)} />
          <Metric label="Record" value={stats ? `${stats.wins}–${stats.losses}` : "—"} />
          <Metric label="Average game" value={formatDuration(stats?.averageGameTimeSeconds)} />
          <Metric label="Tracked launches" value={String(stats?.totalRuns ?? 0)} />
        </section>

        <section className="stats-grid">
          <div className="panel analytics-panel">
            <header className="section-heading">
              <div>
                <span className="eyebrow">BREAKDOWN</span>
                <h2>Opponent performance</h2>
              </div>
              <span>{decisive} decisive games</span>
            </header>
            {stats?.breakdown.length ? (
              <div className="data-table">
                <div className="data-row data-head">
                  <span>Opponent</span>
                  <span>Race / difficulty</span>
                  <span>Record</span>
                  <span>Win rate</span>
                </div>
                {stats.breakdown.map((item) => {
                  const groupDecisive = item.wins + item.losses;
                  return (
                    <div
                      className="data-row"
                      key={`${item.opponentType}-${item.opponentName}-${item.enemyRace}-${item.difficulty}`}
                    >
                      <strong>{item.opponentName}</strong>
                      <span>
                        {item.enemyRace}
                        {item.difficulty ? ` · ${item.difficulty.replaceAll("_", " ")}` : ""}
                      </span>
                      <span>
                        {item.wins}–{item.losses}
                        {item.ties ? `–${item.ties}` : ""}
                      </span>
                      <span>
                        {groupDecisive
                          ? `${Math.round((item.wins / groupDecisive) * 100)}%`
                          : "—"}
                      </span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="compact-empty">Run a match to establish a baseline.</div>
            )}
          </div>

          <div className="panel analytics-panel">
            <header className="section-heading">
              <div>
                <span className="eyebrow">HISTORY</span>
                <h2>{matchTotal} recorded matches</h2>
              </div>
              <div className="inline-filters">
                <select
                  value={opponentFilter}
                  onChange={(event) => setOpponentFilter(event.target.value)}
                >
                  <option value="">All opponents</option>
                  <option value="computer">Computer</option>
                  <option value="bot">Studio bot</option>
                </select>
                <select
                  value={resultFilter}
                  onChange={(event) => setResultFilter(event.target.value)}
                >
                  <option value="">All results</option>
                  <option value="victory">Victory</option>
                  <option value="defeat">Defeat</option>
                  <option value="tie">Tie</option>
                  <option value="undecided">Undecided</option>
                </select>
              </div>
            </header>
            <div className="match-list">
              {matches.map((match) => (
                <article className="match-row" key={match.id}>
                  <span className={`result-badge ${match.perspectiveResult ?? match.status}`}>
                    {match.perspectiveResult ?? match.status}
                  </span>
                  <div>
                    <strong>{match.opponent.name}</strong>
                    <span>
                      {match.mapName} · {match.opponent.resolvedRace ?? match.opponent.requestedRace}
                      {match.opponent.difficulty
                        ? ` · ${match.opponent.difficulty.replaceAll("_", " ")}`
                        : ""}
                    </span>
                  </div>
                  <div>
                    <strong>{formatDuration(match.gameTimeSeconds)}</strong>
                    <span>
                      {match.source} · {new Date(match.createdAt).toLocaleDateString()}
                    </span>
                  </div>
                </article>
              ))}
              {!matches.length && <div className="compact-empty">No matches match the filters.</div>}
            </div>
          </div>
        </section>

        <section className="panel benchmark-section">
          <header className="section-heading">
            <div>
              <span className="eyebrow">REUSABLE BENCHMARKS</span>
              <h2>Benchmark suites</h2>
            </div>
            <button
              className="button secondary"
              onClick={() =>
                setSuiteDraft({
                  id: null,
                  name: "Core benchmark",
                  description: "",
                  scenarios: [emptyScenario(maps[0] ?? "")],
                })
              }
            >
              + New suite
            </button>
          </header>
          <div className="suite-grid">
            {suites.map((suite) => (
              <article className="suite-card" key={suite.id}>
                <div>
                  <strong>{suite.name}</strong>
                  <span>
                    {suite.scenarios.length} scenario
                    {suite.scenarios.length === 1 ? "" : "s"}
                  </span>
                </div>
                <p>{suite.description || "No description."}</p>
                <div className="suite-actions">
                  <button
                    className="button secondary small"
                    onClick={() => setSuiteDraft(toDraft(suite))}
                  >
                    Edit
                  </button>
                  <button
                    className="button ghost small"
                    disabled={busy}
                    onClick={() => void duplicateSuite(suite)}
                  >
                    Duplicate
                  </button>
                  <button
                    className="button ghost small"
                    disabled={busy}
                    onClick={() => void archiveSuite(suite)}
                  >
                    Archive
                  </button>
                </div>
              </article>
            ))}
            {!suites.length && (
              <div className="compact-empty">
                Create a suite before launching a regression comparison.
              </div>
            )}
          </div>
        </section>

        {suiteDraft && (
          <SuiteEditor
            draft={suiteDraft}
            maps={maps}
            bots={bots}
            busy={busy}
            onChange={setSuiteDraft}
            onCancel={() => setSuiteDraft(null)}
            onSave={() => void saveSuite()}
          />
        )}

        <section className="panel regression-section">
          <header className="section-heading">
            <div>
              <span className="eyebrow">MANUAL REGRESSION</span>
              <h2>Current revision vs a baseline</h2>
            </div>
            <span className="sample-warning">Small samples are directional, not conclusive.</span>
          </header>
          <div className="regression-config">
            <label>
              Candidate
              <div className="readonly-field">Current revision v{bot.currentRevision}</div>
            </label>
            <label>
              Baseline revision
              <select
                value={baselineRevision ?? ""}
                onChange={(event) => setBaselineRevision(Number(event.target.value))}
              >
                {revisions
                  .filter((revision) => revision.number < bot.currentRevision)
                  .map((revision) => (
                    <option key={revision.id} value={revision.number}>
                      v{revision.number} · {revision.summary}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              Benchmark suite
              <select value={suiteId} onChange={(event) => setSuiteId(event.target.value)}>
                {suites.map((suite) => (
                  <option key={suite.id} value={suite.id}>
                    {suite.name} · {suite.scenarios.length} scenarios
                  </option>
                ))}
              </select>
            </label>
            <label>
              Games per scenario
              <select
                value={gamesPerScenario}
                onChange={(event) => setGamesPerScenario(Number(event.target.value))}
              >
                {Array.from({ length: 10 }, (_, index) => index + 1).map((amount) => (
                  <option value={amount} key={amount}>
                    {amount}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Parallel games
              <select
                value={concurrency}
                onChange={(event) => setConcurrency(Number(event.target.value))}
              >
                <option value={1}>1 · conservative</option>
                <option value={2}>2 · faster, more SC2 processes</option>
              </select>
            </label>
          </div>
          <div className="batch-estimate">
            <strong>{estimatedGames} total matches</strong>
            <span>
              2 revisions × {selectedSuite?.scenarios.length ?? 0} scenarios ×{" "}
              {gamesPerScenario} games
            </span>
            {concurrency === 2 && (
              <span>
                Bot-v-bot scenarios may run four SC2 processes simultaneously.
              </span>
            )}
          </div>
          <button
            className="button primary large"
            disabled={busy || !baselineRevision || !suiteId || Boolean(activeBatch && isActive(activeBatch))}
            onClick={() => void startRegression()}
          >
            Launch regression batch
          </button>

          {activeBatch && (
            <RegressionProgress
              batch={activeBatch}
              busy={busy}
              onCancel={() => void cancelRegression()}
              onResume={async () => {
                const resumed = await api<RegressionBatch>(
                  `/regressions/${activeBatch.id}/resume`,
                  jsonOptions("POST"),
                );
                setActiveBatch(resumed);
                watchBatch(resumed.id);
              }}
            />
          )}

          {regressions.length > 0 && (
            <div className="previous-batches">
              <h3>Previous comparisons</h3>
              {regressions.slice(0, 8).map((batch) => (
                <button
                  key={batch.id}
                  onClick={() => {
                    setActiveBatch(batch);
                    if (isActive(batch)) watchBatch(batch.id);
                  }}
                >
                  <span>
                    v{batch.candidateRevision} vs v{batch.baselineRevision} · {batch.suiteName}
                  </span>
                  <strong>{batch.status}</strong>
                </button>
              ))}
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function SuiteEditor({
  draft,
  maps,
  bots,
  busy,
  onChange,
  onCancel,
  onSave,
}: {
  draft: SuiteDraft;
  maps: string[];
  bots: BotSummary[];
  busy: boolean;
  onChange: (draft: SuiteDraft) => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  const updateScenario = (index: number, change: Partial<BenchmarkScenario>) => {
    const scenarios = [...draft.scenarios];
    scenarios[index] = { ...scenarios[index], ...change };
    onChange({ ...draft, scenarios });
  };

  return (
    <section className="panel suite-editor">
      <header className="section-heading">
        <div>
          <span className="eyebrow">SUITE EDITOR</span>
          <h2>{draft.id ? "Edit benchmark suite" : "Create benchmark suite"}</h2>
        </div>
      </header>
      <div className="form-grid">
        <label>
          Name
          <input
            value={draft.name}
            onChange={(event) => onChange({ ...draft, name: event.target.value })}
          />
        </label>
        <label>
          Description
          <input
            value={draft.description}
            onChange={(event) => onChange({ ...draft, description: event.target.value })}
          />
        </label>
      </div>
      <div className="scenario-list">
        {draft.scenarios.map((scenario, index) => {
          const opponent = bots.find((item) => item.id === scenario.opponentBotId);
          return (
            <article className="scenario-card" key={`${index}-${scenario.id ?? "new"}`}>
              <header>
                <strong>Scenario {index + 1}</strong>
                {draft.scenarios.length > 1 && (
                  <button
                    className="button ghost small"
                    onClick={() =>
                      onChange({
                        ...draft,
                        scenarios: draft.scenarios.filter((_, itemIndex) => itemIndex !== index),
                      })
                    }
                  >
                    Remove
                  </button>
                )}
              </header>
              <div className="scenario-fields">
                <label>
                  Label
                  <input
                    value={scenario.name}
                    onChange={(event) => updateScenario(index, { name: event.target.value })}
                  />
                </label>
                <label>
                  Map
                  <select
                    value={scenario.mapName}
                    onChange={(event) => updateScenario(index, { mapName: event.target.value })}
                  >
                    {maps.map((map) => (
                      <option value={map} key={map}>
                        {map}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Opponent type
                  <select
                    value={scenario.opponentType}
                    onChange={(event) => {
                      const type = event.target.value as "computer" | "bot";
                      const firstBot = bots[0];
                      updateScenario(index, {
                        opponentType: type,
                        opponentBotId: type === "bot" ? firstBot?.id ?? null : null,
                        opponentRevision:
                          type === "bot" ? firstBot?.currentRevision ?? null : null,
                      });
                    }}
                  >
                    <option value="computer">SC2 Computer</option>
                    <option value="bot">Studio bot</option>
                  </select>
                </label>
                {scenario.opponentType === "computer" ? (
                  <>
                    <label>
                      Race
                      <select
                        value={scenario.enemyRace ?? "zerg"}
                        onChange={(event) =>
                          updateScenario(index, { enemyRace: event.target.value })
                        }
                      >
                        <option value="terran">Terran</option>
                        <option value="protoss">Protoss</option>
                        <option value="zerg">Zerg</option>
                        <option value="random">Random</option>
                      </select>
                    </label>
                    <label>
                      Difficulty
                      <select
                        value={scenario.difficulty ?? "medium"}
                        onChange={(event) =>
                          updateScenario(index, { difficulty: event.target.value })
                        }
                      >
                        <option value="very_easy">Very easy</option>
                        <option value="easy">Easy</option>
                        <option value="medium">Medium</option>
                        <option value="medium_hard">Medium hard</option>
                        <option value="hard">Hard</option>
                        <option value="harder">Harder</option>
                        <option value="very_hard">Very hard</option>
                      </select>
                    </label>
                  </>
                ) : (
                  <>
                    <label>
                      Studio bot
                      <select
                        value={scenario.opponentBotId ?? ""}
                        onChange={(event) => {
                          const selected = bots.find(
                            (item) => item.id === event.target.value,
                          );
                          updateScenario(index, {
                            opponentBotId: event.target.value,
                            opponentRevision: selected?.currentRevision ?? null,
                          });
                        }}
                      >
                        {bots.map((item) => (
                          <option value={item.id} key={item.id}>
                            {item.name} · current v{item.currentRevision}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      Pinned revision
                      <div className="pinned-revision">
                        <input
                          type="number"
                          min={1}
                          value={scenario.opponentRevision ?? ""}
                          onChange={(event) =>
                            updateScenario(index, {
                              opponentRevision: Number(event.target.value),
                            })
                          }
                        />
                        {opponent && (
                          <button
                            className="button ghost small"
                            onClick={() =>
                              updateScenario(index, {
                                opponentRevision: opponent.currentRevision,
                              })
                            }
                          >
                            Use latest v{opponent.currentRevision}
                          </button>
                        )}
                      </div>
                    </label>
                  </>
                )}
              </div>
            </article>
          );
        })}
      </div>
      <div className="suite-editor-actions">
        <button
          className="button secondary"
          onClick={() =>
            onChange({
              ...draft,
              scenarios: [...draft.scenarios, emptyScenario(maps[0] ?? "")],
            })
          }
        >
          + Add scenario
        </button>
        <div>
          <button className="button ghost" onClick={onCancel}>
            Cancel
          </button>
          <button
            className="button primary"
            disabled={
              busy ||
              !draft.name.trim() ||
              draft.scenarios.some((scenario) => !scenario.name || !scenario.mapName)
            }
            onClick={onSave}
          >
            Save suite
          </button>
        </div>
      </div>
    </section>
  );
}

function RegressionProgress({
  batch,
  busy,
  onCancel,
  onResume,
}: {
  batch: RegressionBatch;
  busy: boolean;
  onCancel: () => void;
  onResume: () => void;
}) {
  const progress = batch.totalGames
    ? Math.round((batch.completedGames / batch.totalGames) * 100)
    : 0;
  return (
    <div className="regression-progress">
      <header>
        <div>
          <strong>
            v{batch.candidateRevision} vs v{batch.baselineRevision}
          </strong>
          <span>
            {batch.suiteName} · {batch.status}
          </span>
        </div>
        {isActive(batch) && (
          <button className="button danger-outline small" disabled={busy} onClick={onCancel}>
            Stop batch
          </button>
        )}
        {batch.status === "interrupted" && (
          <button className="button primary small" disabled={busy} onClick={onResume}>
            Resume remaining
          </button>
        )}
      </header>
      <div className="progress-track">
        <span style={{ width: `${progress}%` }} />
      </div>
      <span>
        {batch.completedGames} / {batch.totalGames} matches · {progress}% ·{" "}
        {batch.pairedSamples} paired sample{batch.pairedSamples === 1 ? "" : "s"}
      </span>
      <div className="comparison-grid">
        <ComparisonCard label={`Current v${batch.candidateRevision}`} data={batch.comparison.candidate} />
        <ComparisonCard label={`Baseline v${batch.baselineRevision}`} data={batch.comparison.baseline} />
        <div className="comparison-card delta">
          <span>Win-rate delta</span>
          <strong>
            {batch.comparison.winRateDelta == null
              ? "—"
              : `${batch.comparison.winRateDelta >= 0 ? "+" : ""}${Math.round(
                  batch.comparison.winRateDelta * 100,
                )} pts`}
          </strong>
        </div>
      </div>
      {batch.scenarioComparisons?.length > 0 && (
        <div className="scenario-comparisons">
          {batch.scenarioComparisons.map((scenario) => (
            <div key={scenario.position}>
              <strong>{scenario.name}</strong>
              <span>
                Current {scenario.candidate.wins}–{scenario.candidate.losses} · Baseline{" "}
                {scenario.baseline.wins}–{scenario.baseline.losses}
              </span>
              <span>
                {scenario.pairedSamples} paired ·{" "}
                {scenario.winRateDelta == null
                  ? "No decisive comparison yet"
                  : `${scenario.winRateDelta >= 0 ? "+" : ""}${Math.round(
                      scenario.winRateDelta * 100,
                    )} pts`}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ComparisonCard({
  label,
  data,
}: {
  label: string;
  data: RegressionBatch["comparison"]["candidate"];
}) {
  return (
    <div className="comparison-card">
      <span>{label}</span>
      <strong>{formatPercent(data.winRate)}</strong>
      <small>
        {data.wins}–{data.losses}
        {data.ties ? ` · ${data.ties} ties` : ""}
      </small>
    </div>
  );
}

function toDraft(suite: BenchmarkSuite): SuiteDraft {
  return {
    id: suite.id,
    name: suite.name,
    description: suite.description,
    scenarios: suite.scenarios.map((scenario) => ({ ...scenario })),
  };
}

function isActive(batch: RegressionBatch): boolean {
  return ["queued", "starting", "running", "cancelling"].includes(batch.status);
}

function formatPercent(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

function formatDuration(value: number | null | undefined): string {
  if (value == null) return "—";
  const minutes = Math.floor(value / 60);
  return `${minutes}:${Math.floor(value % 60).toString().padStart(2, "0")}`;
}
