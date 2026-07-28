import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { BotSummary } from "../models";
import RunConsole, {
  MAX_RETAINED_CLIENT_LOGS,
  MAX_RUN_STREAM_RECONNECT_ATTEMPTS,
  RUN_STREAM_RECONNECT_INTERVAL_MS,
  retainClientLogs,
} from "./RunConsole";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, api: vi.fn() };
});

const bot: BotSummary = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "primary",
  name: "Primary",
  description: "",
  race: "protoss",
  tags: [],
  isBuiltin: false,
  forkedFrom: null,
  deletedAt: null,
  currentRevision: 2,
  createdAt: "",
  updatedAt: "",
};

const opponent: BotSummary = {
  ...bot,
  id: "22222222-2222-4222-8222-222222222222",
  slug: "opponent",
  name: "Opponent",
  race: "zerg",
  currentRevision: 4,
};

const completedRun = {
  id: "33333333-3333-4333-8333-333333333333",
  botId: bot.id,
  botName: bot.name,
  mapName: "TestMap",
  enemyRace: "zerg",
  difficulty: "easy",
  opponentType: "computer",
  opponentBotId: null,
  opponentBotName: null,
  opponentRevision: null,
  status: "completed",
  result: "victory",
  gameTimeSeconds: 120,
  failureReason: null,
  returnCode: 0,
};

class FakeEventSource {
  static latest: FakeEventSource | null = null;
  static instances: FakeEventSource[] = [];
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn();

  constructor(readonly url: string) {
    FakeEventSource.latest = this;
    FakeEventSource.instances.push(this);
  }

  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
}

describe("RunConsole", () => {
  beforeEach(() => {
    FakeEventSource.latest = null;
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/runtime/maps") return { maps: ["TestMap"] } as never;
      if (path === "/bots") return [bot, opponent] as never;
      throw new Error(`Unexpected API path: ${path}`);
    });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("switches from computer settings to a Studio bot opponent", async () => {
    render(
      <RunConsole
        bot={bot}
        onBack={() => undefined}
        onMatchFinished={() => undefined}
      />,
    );
    await screen.findByRole("option", { name: "TestMap" });

    fireEvent.change(screen.getByLabelText("Opponent"), {
      target: { value: "bot" },
    });

    await waitFor(() => {
      expect(screen.getByLabelText("Studio bot")).toHaveValue(opponent.id);
    });
    expect(screen.queryByLabelText("Difficulty")).not.toBeInTheDocument();
  });

  it("reports a completed match exactly once", async () => {
    const onMatchFinished = vi.fn();
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/runtime/maps") return { maps: ["TestMap"] } as never;
      if (path === "/bots") return [bot, opponent] as never;
      if (path === "/runs") {
        return { ...completedRun, status: "starting", result: null } as never;
      }
      throw new Error(`Unexpected API path: ${path}`);
    });
    render(
      <RunConsole
        bot={bot}
        onBack={() => undefined}
        onMatchFinished={onMatchFinished}
      />,
    );
    await screen.findByRole("option", { name: "TestMap" });

    fireEvent.click(screen.getByRole("button", { name: "▶ Launch match" }));
    await waitFor(() => expect(FakeEventSource.latest).not.toBeNull());

    act(() => {
      FakeEventSource.latest?.emit({
        type: "log_gap",
        firstAvailable: 1001,
        lastDropped: 1000,
      });
      FakeEventSource.latest?.emit({ type: "status", ...completedRun });
      FakeEventSource.latest?.emit({ type: "status", ...completedRun });
    });

    await waitFor(() => expect(onMatchFinished).toHaveBeenCalledTimes(1));
    expect(
      screen.getByText("[Earlier match output was trimmed. Resuming at line 1001.]"),
    ).toBeInTheDocument();
    expect(await screen.findByText("Victory · 2:00")).toBeInTheDocument();
  });

  it("polls repeatedly after the event stream fails until the match is terminal", async () => {
    const onMatchFinished = vi.fn();
    let statusRequests = 0;
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/runtime/maps") return { maps: ["TestMap"] } as never;
      if (path === "/bots") return [bot, opponent] as never;
      if (path === "/runs") {
        return { ...completedRun, status: "starting", result: null } as never;
      }
      if (path === `/runs/${completedRun.id}`) {
        statusRequests += 1;
        return (
          statusRequests === 1
            ? { ...completedRun, status: "running", result: null, gameTimeSeconds: null }
            : completedRun
        ) as never;
      }
      throw new Error(`Unexpected API path: ${path}`);
    });
    render(
      <RunConsole
        bot={bot}
        onBack={() => undefined}
        onMatchFinished={onMatchFinished}
      />,
    );
    await screen.findByRole("option", { name: "TestMap" });

    fireEvent.click(screen.getByRole("button", { name: "▶ Launch match" }));
    await waitFor(() => expect(FakeEventSource.latest).not.toBeNull());
    vi.useFakeTimers();
    await act(async () => {
      FakeEventSource.latest?.onerror?.();
      await Promise.resolve();
    });

    expect(statusRequests).toBe(1);
    expect(onMatchFinished).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });

    expect(statusRequests).toBe(2);
    expect(onMatchFinished).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Victory · 2:00")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(statusRequests).toBe(2);
  });

  it("resumes from the absolute cursor and recovers final logs after polling sees terminal", async () => {
    const onMatchFinished = vi.fn();
    let statusRequests = 0;
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/runtime/maps") return { maps: ["TestMap"] } as never;
      if (path === "/bots") return [bot, opponent] as never;
      if (path === "/runs") {
        return {
          ...completedRun,
          status: "starting",
          result: null,
          logCount: 0,
          firstLogSequence: 1,
        } as never;
      }
      if (path === `/runs/${completedRun.id}`) {
        statusRequests += 1;
        return { ...completedRun, logCount: 3, firstLogSequence: 1 } as never;
      }
      throw new Error(`Unexpected API path: ${path}`);
    });
    const view = render(
      <RunConsole
        bot={bot}
        onBack={() => undefined}
        onMatchFinished={onMatchFinished}
      />,
    );
    await screen.findByRole("option", { name: "TestMap" });

    fireEvent.click(screen.getByRole("button", { name: "▶ Launch match" }));
    await waitFor(() => expect(FakeEventSource.latest).not.toBeNull());
    const firstSource = FakeEventSource.latest!;
    expect(firstSource.url).toBe(
      `/api/runs/${completedRun.id}/events?after=0`,
    );
    act(() => firstSource.emit({ type: "log", sequence: 1, line: "line one" }));

    vi.useFakeTimers();
    await act(async () => {
      firstSource.onerror?.();
      await Promise.resolve();
    });

    expect(statusRequests).toBe(1);
    expect(onMatchFinished).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Victory · 2:00")).toBeInTheDocument();
    expect(FakeEventSource.instances).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(RUN_STREAM_RECONNECT_INTERVAL_MS);
    });
    const resumedSource = FakeEventSource.latest!;
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(resumedSource.url).toBe(
      `/api/runs/${completedRun.id}/events?after=1`,
    );

    act(() => {
      resumedSource.emit({ type: "log", sequence: 1, line: "duplicate line one" });
      resumedSource.emit({ type: "log", sequence: 2, line: "line two" });
      resumedSource.emit({ type: "log", sequence: 3, line: "line three" });
      resumedSource.emit({
        type: "status",
        ...completedRun,
        logCount: 3,
        firstLogSequence: 1,
      });
    });

    const output = view.container.querySelector("pre")?.textContent?.split("\n");
    expect(output).toEqual(["line one", "line two", "line three"]);
    expect(resumedSource.close).toHaveBeenCalled();
    expect(onMatchFinished).toHaveBeenCalledTimes(1);
  });

  it("bounds stream reconnect attempts while status polling continues", async () => {
    let statusRequests = 0;
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/runtime/maps") return { maps: ["TestMap"] } as never;
      if (path === "/bots") return [bot, opponent] as never;
      if (path === "/runs") {
        return { ...completedRun, status: "starting", result: null } as never;
      }
      if (path === `/runs/${completedRun.id}`) {
        statusRequests += 1;
        return {
          ...completedRun,
          status: "running",
          result: null,
          gameTimeSeconds: null,
          logCount: 0,
        } as never;
      }
      throw new Error(`Unexpected API path: ${path}`);
    });
    const view = render(
      <RunConsole
        bot={bot}
        onBack={() => undefined}
        onMatchFinished={() => undefined}
      />,
    );
    await screen.findByRole("option", { name: "TestMap" });
    fireEvent.click(screen.getByRole("button", { name: "▶ Launch match" }));
    await waitFor(() => expect(FakeEventSource.latest).not.toBeNull());

    vi.useFakeTimers();
    await act(async () => {
      FakeEventSource.latest?.onerror?.();
      await Promise.resolve();
    });
    expect(statusRequests).toBe(1);

    for (let attempt = 0; attempt < MAX_RUN_STREAM_RECONNECT_ATTEMPTS; attempt += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(RUN_STREAM_RECONNECT_INTERVAL_MS);
      });
      expect(FakeEventSource.instances).toHaveLength(attempt + 2);
      act(() => {
        FakeEventSource.latest?.emit({
          type: "log",
          sequence: attempt + 1,
          line: `intermittent line ${attempt + 1}`,
        });
        FakeEventSource.latest?.onerror?.();
      });
    }

    const sourceCount = FakeEventSource.instances.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RUN_STREAM_RECONNECT_INTERVAL_MS * 4);
    });
    expect(FakeEventSource.instances).toHaveLength(sourceCount);
    expect(statusRequests).toBeGreaterThan(1);
    expect(view.container.querySelector("pre")).toHaveTextContent(
      "[Live match output could not reconnect; status monitoring will continue.]",
    );
    view.unmount();
  });

  it("retains only the server-sized client log window", () => {
    const input = Array.from(
      { length: MAX_RETAINED_CLIENT_LOGS + 3 },
      (_, index) => `line ${index + 1}`,
    );

    const retained = retainClientLogs([], input);

    expect(retained).toHaveLength(MAX_RETAINED_CLIENT_LOGS);
    expect(retained[0]).toBe("line 4");
    expect(retained.at(-1)).toBe(`line ${MAX_RETAINED_CLIENT_LOGS + 3}`);
  });

  it("cancels recovery polling when the console unmounts", async () => {
    let statusRequests = 0;
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/runtime/maps") return { maps: ["TestMap"] } as never;
      if (path === "/bots") return [bot, opponent] as never;
      if (path === "/runs") {
        return { ...completedRun, status: "starting", result: null } as never;
      }
      if (path === `/runs/${completedRun.id}`) {
        statusRequests += 1;
        return {
          ...completedRun,
          status: "running",
          result: null,
          gameTimeSeconds: null,
        } as never;
      }
      throw new Error(`Unexpected API path: ${path}`);
    });
    const view = render(
      <RunConsole
        bot={bot}
        onBack={() => undefined}
        onMatchFinished={() => undefined}
      />,
    );
    await screen.findByRole("option", { name: "TestMap" });

    fireEvent.click(screen.getByRole("button", { name: "▶ Launch match" }));
    await waitFor(() => expect(FakeEventSource.latest).not.toBeNull());
    vi.useFakeTimers();
    await act(async () => {
      FakeEventSource.latest?.onerror?.();
      await Promise.resolve();
    });
    expect(statusRequests).toBe(1);

    view.unmount();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(statusRequests).toBe(1);
  });
});
