import { useEffect, useRef, useState } from "react";
import { api, jsonOptions } from "../api";
import type { BotSummary } from "../models";

interface RunRecord {
  id: string;
  botId: string;
  botName: string;
  mapName: string;
  enemyRace: string;
  difficulty: string | null;
  opponentType: "computer" | "bot";
  opponentBotId: string | null;
  opponentBotName: string | null;
  opponentRevision: number | null;
  status: string;
  result: "victory" | "defeat" | "tie" | "undecided" | null;
  gameTimeSeconds: number | null;
  failureReason: string | null;
  returnCode: number | null;
  logCount?: number;
  firstLogSequence?: number;
}

interface RunLogEvent {
  type: "log";
  sequence?: number;
  line: string;
}

interface RunLogGapEvent {
  type: "log_gap";
  after?: number;
  firstAvailable?: number;
  lastDropped?: number;
}

const TERMINAL_RUN_STATUSES = new Set(["completed", "failed", "stopped"]);
const RUN_POLL_INTERVAL_MS = 1_000;
const MAX_RUN_POLL_ATTEMPTS = 1_800;
export const MAX_RETAINED_CLIENT_LOGS = 5_000;
export const RUN_STREAM_RECONNECT_INTERVAL_MS = 500;
export const MAX_RUN_STREAM_RECONNECT_ATTEMPTS = 20;

export function retainClientLogs(current: string[], entries: string[]): string[] {
  const next = [...current, ...entries];
  return next.length > MAX_RETAINED_CLIENT_LOGS
    ? next.slice(-MAX_RETAINED_CLIENT_LOGS)
    : next;
}

export default function RunConsole({
  bot,
  onBack,
  onMatchFinished,
}: {
  bot: BotSummary;
  onBack: () => void;
  onMatchFinished: () => void | Promise<void>;
}) {
  const [maps, setMaps] = useState<string[]>([]);
  const [mapName, setMapName] = useState("");
  const [enemyRace, setEnemyRace] = useState("zerg");
  const [difficulty, setDifficulty] = useState("easy");
  const [opponentType, setOpponentType] = useState<"computer" | "bot">("computer");
  const [opponentBotId, setOpponentBotId] = useState("");
  const [bots, setBots] = useState<BotSummary[]>([]);
  const [run, setRun] = useState<RunRecord | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const consoleRef = useRef<HTMLPreElement>(null);
  const eventSource = useRef<EventSource | null>(null);
  const pollTimer = useRef<number | null>(null);
  const pollGeneration = useRef(0);
  const pollingRunId = useRef<string | null>(null);
  const reconnectTimer = useRef<number | null>(null);
  const streamGeneration = useRef(0);
  const streamReconnectAttempts = useRef(0);
  const streamRecoveryExhausted = useRef(false);
  const lastLogSequence = useRef(0);
  const terminalRun = useRef<RunRecord | null>(null);
  const reportedRunIds = useRef(new Set<string>());

  useEffect(() => {
    void Promise.all([
      api<{ maps: string[] }>("/runtime/maps"),
      api<BotSummary[]>("/bots"),
    ])
      .then(([mapResult, botResult]) => {
        setMaps(mapResult.maps);
        setMapName(
          mapResult.maps.includes("AcropolisLE")
            ? "AcropolisLE"
            : mapResult.maps[0] ?? "",
        );
        const opponents = botResult.filter((item) => item.id !== bot.id);
        setBots(opponents);
        setOpponentBotId(opponents[0]?.id ?? "");
      })
      .catch((reason) =>
        setError(reason instanceof Error ? reason.message : "Could not load match options."),
      );
    return () => {
      streamGeneration.current += 1;
      pollGeneration.current += 1;
      eventSource.current?.close();
      eventSource.current = null;
      if (pollTimer.current !== null) {
        window.clearTimeout(pollTimer.current);
        pollTimer.current = null;
      }
      if (reconnectTimer.current !== null) {
        window.clearTimeout(reconnectTimer.current);
        reconnectTimer.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (consoleRef.current) {
      consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
    }
  }, [logs]);

  const reportMatchFinished = (record: RunRecord) => {
    if (
      !TERMINAL_RUN_STATUSES.has(record.status) ||
      reportedRunIds.current.has(record.id)
    ) {
      return;
    }
    reportedRunIds.current.add(record.id);
    void onMatchFinished();
  };

  const clearPollTimer = () => {
    if (pollTimer.current !== null) {
      window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  };

  const clearReconnectTimer = () => {
    if (reconnectTimer.current !== null) {
      window.clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
  };

  const closeEventStream = () => {
    eventSource.current?.close();
    eventSource.current = null;
  };

  const stopPolling = () => {
    pollGeneration.current += 1;
    pollingRunId.current = null;
    clearPollTimer();
  };

  const stopWatchingRun = () => {
    streamGeneration.current += 1;
    stopPolling();
    clearReconnectTimer();
    closeEventStream();
    terminalRun.current = null;
  };

  const updateRun = (record: RunRecord) => {
    setRun(record);
    reportMatchFinished(record);
    return TERMINAL_RUN_STATUSES.has(record.status);
  };

  const appendLogEntries = (entries: string[]) => {
    if (!entries.length) return;
    setLogs((current) => retainClientLogs(current, entries));
  };

  const unrecoveredLogCount = (record: RunRecord) =>
    typeof record.logCount === "number"
      ? Math.max(0, record.logCount - lastLogSequence.current)
      : 0;

  const settleTerminalRun = (record: RunRecord) => {
    updateRun(record);
    terminalRun.current = record;
    const missing = unrecoveredLogCount(record);
    if (missing > 0 && !streamRecoveryExhausted.current) return false;
    if (missing > 0) {
      appendLogEntries([
        `[Could not recover ${missing} final match output line${missing === 1 ? "" : "s"} after repeated stream disconnects.]`,
      ]);
    }
    stopWatchingRun();
    return true;
  };

  const exhaustStreamRecovery = () => {
    if (streamRecoveryExhausted.current) return;
    streamRecoveryExhausted.current = true;
    clearReconnectTimer();
    if (terminalRun.current) {
      settleTerminalRun(terminalRun.current);
    } else {
      appendLogEntries([
        "[Live match output could not reconnect; status monitoring will continue.]",
      ]);
    }
  };

  const pollRunUntilTerminal = (runId: string) => {
    if (pollingRunId.current === runId) return;
    clearPollTimer();
    pollingRunId.current = runId;
    const generation = ++pollGeneration.current;

    const poll = async (attempt: number) => {
      if (pollGeneration.current !== generation) return;
      pollTimer.current = null;

      try {
        const refreshed = await api<RunRecord>(`/runs/${runId}`);
        if (pollGeneration.current !== generation) return;
        if (TERMINAL_RUN_STATUSES.has(refreshed.status)) {
          pollingRunId.current = null;
          settleTerminalRun(refreshed);
          return;
        }
        updateRun(refreshed);
      } catch {
        // A transient polling failure should not abandon recovery.
      }

      if (pollGeneration.current !== generation) return;
      if (attempt + 1 >= MAX_RUN_POLL_ATTEMPTS) {
        pollingRunId.current = null;
        setError("Could not confirm the final match status. Check that the backend is running.");
        return;
      }
      pollTimer.current = window.setTimeout(
        () => void poll(attempt + 1),
        RUN_POLL_INTERVAL_MS,
      );
    };

    void poll(0);
  };

  const recordLogEvent = (payload: RunLogEvent) => {
    if (typeof payload.sequence !== "number") {
      appendLogEntries([payload.line]);
      return;
    }
    if (payload.sequence <= lastLogSequence.current) return;

    const additions: string[] = [];
    if (payload.sequence > lastLogSequence.current + 1) {
      additions.push(
        `[Match output lines ${lastLogSequence.current + 1}–${payload.sequence - 1} were unavailable.]`,
      );
    }
    lastLogSequence.current = payload.sequence;
    additions.push(payload.line);
    appendLogEntries(additions);
  };

  const recordLogGap = (payload: RunLogGapEvent) => {
    const lastDropped =
      typeof payload.lastDropped === "number"
        ? payload.lastDropped
        : typeof payload.firstAvailable === "number"
          ? payload.firstAvailable - 1
          : null;
    if (lastDropped == null || lastDropped <= lastLogSequence.current) return;

    lastLogSequence.current = lastDropped;
    const resumeAt =
      typeof payload.firstAvailable === "number"
        ? ` Resuming at line ${payload.firstAvailable}.`
        : "";
    appendLogEntries([`[Earlier match output was trimmed.${resumeAt}]`]);
  };

  function scheduleEventStreamReconnect(
    record: RunRecord,
    generation: number,
  ) {
    if (
      streamGeneration.current !== generation ||
      reconnectTimer.current !== null
    ) {
      return;
    }
    if (streamReconnectAttempts.current >= MAX_RUN_STREAM_RECONNECT_ATTEMPTS) {
      exhaustStreamRecovery();
      return;
    }

    reconnectTimer.current = window.setTimeout(() => {
      reconnectTimer.current = null;
      if (streamGeneration.current !== generation) return;
      streamReconnectAttempts.current += 1;
      openEventStream(record, generation);
    }, RUN_STREAM_RECONNECT_INTERVAL_MS);
  }

  function openEventStream(record: RunRecord, generation: number) {
    if (streamGeneration.current !== generation) return;
    closeEventStream();
    const source = new EventSource(
      `/api/runs/${record.id}/events?after=${lastLogSequence.current}`,
    );
    eventSource.current = source;

    source.onmessage = (event) => {
      if (
        streamGeneration.current !== generation ||
        eventSource.current !== source
      ) {
        return;
      }
      let payload: Record<string, unknown>;
      try {
        payload = JSON.parse(event.data) as Record<string, unknown>;
      } catch {
        return;
      }
      if (payload.type === "log") {
        recordLogEvent(payload as unknown as RunLogEvent);
      } else if (payload.type === "log_gap") {
        recordLogGap(payload as unknown as RunLogGapEvent);
      } else if (payload.type === "status") {
        const refreshed = { ...record, ...payload } as RunRecord;
        if (TERMINAL_RUN_STATUSES.has(refreshed.status)) {
          settleTerminalRun(refreshed);
        } else {
          updateRun(refreshed);
        }
      }
    };
    source.onerror = () => {
      if (
        streamGeneration.current !== generation ||
        eventSource.current !== source
      ) {
        source.close();
        return;
      }
      closeEventStream();
      if (!terminalRun.current) {
        pollRunUntilTerminal(record.id);
      }
      scheduleEventStreamReconnect(record, generation);
    };
  }

  const start = async () => {
    stopWatchingRun();
    setBusy(true);
    setError(null);
    setLogs([]);
    try {
      const created = await api<RunRecord>(
        "/runs",
        jsonOptions("POST", {
          bot_id: bot.id,
          map_name: mapName,
          enemy_race: enemyRace,
          difficulty,
          opponent_type: opponentType,
          opponent_bot_id: opponentType === "bot" ? opponentBotId : null,
        }),
      );
      setRun(created);
      lastLogSequence.current = 0;
      streamReconnectAttempts.current = 0;
      streamRecoveryExhausted.current = false;
      terminalRun.current = null;
      const generation = ++streamGeneration.current;
      openEventStream(created, generation);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start match.");
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    if (!run) return;
    setBusy(true);
    try {
      const stopping = await api<RunRecord>(`/runs/${run.id}/stop`, jsonOptions("POST"));
      if (TERMINAL_RUN_STATUSES.has(stopping.status)) {
        settleTerminalRun(stopping);
      } else {
        updateRun(stopping);
        pollRunUntilTerminal(run.id);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not stop match.");
    } finally {
      setBusy(false);
    }
  };

  const active = run && ["starting", "running", "stopping"].includes(run.status);

  return (
    <div className="run-page">
      <header className="editor-topbar">
        <button className="button ghost" onClick={onBack} disabled={Boolean(active)}>
          ← Library
        </button>
        <div className="editor-title">
          <strong>Test match</strong>
          <span>{bot.name}</span>
        </div>
        <div className="status-chip ready">
          <span />
          Local SC2
        </div>
      </header>

      <main className="run-layout">
        <section className="run-setup panel">
          <span className="eyebrow">MATCH SETUP</span>
          <h1>Launch a local test</h1>
          <p>
            Bot Studio starts one burnySC2 process and streams its output here. StarCraft II may
            open in a separate window.
          </p>
          <label>
            Your bot
            <div className={`selected-bot race-${bot.race}`}>
              <span className={`race-badge ${bot.race}`}>{bot.race[0].toUpperCase()}</span>
              <div>
                <strong>{bot.name}</strong>
                <small>{bot.slug}</small>
              </div>
            </div>
          </label>
          <label>
            Installed map
            <select value={mapName} onChange={(event) => setMapName(event.target.value)} disabled={Boolean(active)}>
              {maps.length === 0 && <option value="">No maps found</option>}
              {maps.map((map) => (
                <option key={map}>{map}</option>
              ))}
            </select>
          </label>
          <div className="form-grid">
            <label>
              Opponent
              <select
                value={opponentType}
                onChange={(event) =>
                  setOpponentType(event.target.value as "computer" | "bot")
                }
                disabled={Boolean(active)}
              >
                <option value="computer">SC2 Computer</option>
                <option value="bot">Studio Bot</option>
              </select>
            </label>
            {opponentType === "bot" ? (
              <label>
                Studio bot
                <select
                  value={opponentBotId}
                  onChange={(event) => setOpponentBotId(event.target.value)}
                  disabled={Boolean(active)}
                >
                  {bots.map((item) => (
                    <option value={item.id} key={item.id}>
                      {item.name} · {item.race} · v{item.currentRevision}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <label>
                Enemy race
                <select
                  value={enemyRace}
                  onChange={(event) => setEnemyRace(event.target.value)}
                  disabled={Boolean(active)}
                >
                  <option value="terran">Terran</option>
                  <option value="protoss">Protoss</option>
                  <option value="zerg">Zerg</option>
                  <option value="random">Random</option>
                </select>
              </label>
            )}
          </div>
          {opponentType === "computer" && (
            <label>
              Difficulty
              <select
                value={difficulty}
                onChange={(event) => setDifficulty(event.target.value)}
                disabled={Boolean(active)}
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
          )}
          {error && <div className="alert error">{error}</div>}
          {!active ? (
            <button
              className="button primary large"
              disabled={
                busy || !mapName || (opponentType === "bot" && !opponentBotId)
              }
              onClick={() => void start()}
            >
              ▶ Launch match
            </button>
          ) : (
            <button className="button danger-outline large" disabled={busy} onClick={() => void stop()}>
              ■ Stop match
            </button>
          )}
        </section>

        <section className="console-panel">
          <header>
            <div>
              <span className={`run-light ${run?.status ?? "idle"}`} />
              <strong>{run?.status ?? "Ready"}</strong>
            </div>
            {run && (
              <span>
                {run.mapName} · vs{" "}
                {run.opponentType === "bot"
                  ? `${run.opponentBotName} v${run.opponentRevision}`
                  : `${run.enemyRace} · ${run.difficulty}`}
              </span>
            )}
          </header>
          <pre ref={consoleRef}>
            {logs.length > 0
              ? logs.join("\n")
              : "Match output will appear here.\n\nChoose an installed map and launch when ready."}
          </pre>
          {run && !active && (
            <footer>
              <strong>
                {run.status === "completed"
                  ? `${formatResult(run.result)}${run.gameTimeSeconds == null ? "" : ` · ${formatDuration(run.gameTimeSeconds)}`}`
                  : run.status === "stopped"
                    ? "Match stopped."
                    : "Match process failed."}
              </strong>
              {run.failureReason && <span>{run.failureReason}</span>}
              {run.returnCode != null && <span>Exit code {run.returnCode}</span>}
            </footer>
          )}
        </section>
      </main>
    </div>
  );
}

function formatResult(result: RunRecord["result"]): string {
  if (!result) return "Match completed";
  return result[0].toUpperCase() + result.slice(1);
}

function formatDuration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${Math.floor(seconds % 60).toString().padStart(2, "0")}`;
}
