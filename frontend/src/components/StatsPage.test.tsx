import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { BotStats, BotSummary, RegressionBatch } from "../models";
import StatsPage from "./StatsPage";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, api: vi.fn() };
});

const candidateDigest = "a".repeat(64);
const baselineDigest = "b".repeat(64);
const opponentDigest = "c".repeat(64);

const bot: BotSummary = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "candidate",
  name: "Candidate",
  description: "",
  race: "terran",
  tags: [],
  isBuiltin: false,
  forkedFrom: null,
  deletedAt: null,
  currentRevision: 2,
  currentRevisionId: "22222222-2222-4222-8222-222222222222",
  currentRevisionDigest: candidateDigest,
  createdAt: "",
  updatedAt: "",
};

const revisions = [
  {
    id: bot.currentRevisionId,
    number: 2,
    content_digest: candidateDigest,
    summary: "Current",
    created_at: "",
  },
  {
    id: "11111111-2222-4222-8222-222222222222",
    number: 1,
    content_digest: baselineDigest,
    summary: "Baseline",
    created_at: "",
  },
];

const stats: BotStats = {
  botId: bot.id,
  totalRuns: 0,
  completedMatches: 0,
  wins: 0,
  losses: 0,
  ties: 0,
  undecided: 0,
  stopped: 0,
  failed: 0,
  winRate: null,
  averageGameTimeSeconds: null,
  breakdown: [],
};

const emptyRegressionSummary = {
  wins: 0,
  losses: 0,
  ties: 0,
  undecided: 0,
  winRate: null,
  averageGameTimeSeconds: null,
};

const runningBatch: RegressionBatch = {
  id: "33333333-3333-4333-8333-333333333333",
  botId: bot.id,
  candidateRevision: 2,
  baselineRevision: 1,
  candidateRevisionId: bot.currentRevisionId,
  candidateRevisionDigest: candidateDigest,
  baselineRevisionId: revisions[1].id,
  baselineRevisionDigest: baselineDigest,
  suiteId: "44444444-4444-4444-8444-444444444444",
  suiteName: "Core",
  gamesPerScenario: 1,
  concurrency: 1,
  status: "running",
  totalGames: 2,
  completedGames: 0,
  pairedSamples: 0,
  createdAt: "",
  startedAt: "",
  finishedAt: null,
  games: [],
  comparison: {
    candidate: emptyRegressionSummary,
    baseline: emptyRegressionSummary,
    winRateDelta: null,
  },
  scenarioComparisons: [],
};

const completedWithFailures: RegressionBatch = {
  ...runningBatch,
  status: "completed_with_failures",
  completedGames: 2,
  finishedAt: "",
};

class FakeEventSource {
  static latest: FakeEventSource | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn();

  constructor(readonly url: string) {
    FakeEventSource.latest = this;
  }
}

describe("StatsPage", () => {
  beforeEach(() => {
    FakeEventSource.latest = null;
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path.includes("/stats")) return stats as never;
      if (path.includes("/revisions")) return revisions as never;
      if (path === "/benchmarks" || path.includes("/regressions")) return [] as never;
      if (path === "/runtime/maps") return { maps: ["TestMap"] } as never;
      if (path === "/bots") return [bot] as never;
      if (path.includes("/matches")) {
        return { items: [], total: 0, limit: 100, offset: 0 } as never;
      }
      throw new Error(`Unexpected API path: ${path}`);
    });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("shows empty analytics and opens a reusable benchmark editor", async () => {
    render(<StatsPage bot={bot} onBack={() => undefined} onRun={() => undefined} />);

    await screen.findByText("Track results. Catch regressions.");
    expect(screen.getByText("Candidate · current v2@aaaaaaaa")).toBeInTheDocument();
    expect(screen.getByText("0–0")).toBeInTheDocument();
    expect(screen.getByText("0 total matches")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "+ New suite" }));
    expect(await screen.findByText("Create benchmark suite")).toBeInTheDocument();
    expect(screen.getByLabelText("Map")).toHaveValue("TestMap");
  });

  it("polls a regression until completed with failures after its event stream fails", async () => {
    let statusRequests = 0;
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path.includes("/stats")) return stats as never;
      if (path.includes("/revisions")) return revisions as never;
      if (path === `/bots/${bot.id}/regressions`) return [runningBatch] as never;
      if (path === `/regressions/${runningBatch.id}`) {
        statusRequests += 1;
        return (statusRequests === 1 ? runningBatch : completedWithFailures) as never;
      }
      if (path === "/benchmarks") return [] as never;
      if (path === "/runtime/maps") return { maps: ["TestMap"] } as never;
      if (path === "/bots") return [bot] as never;
      if (path.includes("/matches")) {
        return { items: [], total: 0, limit: 100, offset: 0 } as never;
      }
      throw new Error(`Unexpected API path: ${path}`);
    });

    render(<StatsPage bot={bot} onBack={() => undefined} onRun={() => undefined} />);
    fireEvent.click(
      await screen.findByText("v2@aaaaaaaa vs v1@bbbbbbbb · Core"),
    );
    await waitFor(() => expect(FakeEventSource.latest).not.toBeNull());

    vi.useFakeTimers();
    await act(async () => {
      FakeEventSource.latest?.onerror?.();
      await Promise.resolve();
    });
    expect(statusRequests).toBe(1);
    expect(screen.getByText("Core · running")).toBeInTheDocument();
    expect(screen.getByText("v2@aaaaaaaa vs v1@bbbbbbbb")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });

    expect(statusRequests).toBe(2);
    expect(screen.getByText("Core · completed with failures")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Finished with failed matches; comparisons use completed pairs only.",
    );
    expect(screen.queryByRole("button", { name: "Stop batch" })).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(statusRequests).toBe(2);
  });

  it("shows immutable revision provenance for both sides of a recorded match", async () => {
    const candidateParticipant = {
      slot: 1,
      participantType: "bot" as const,
      botId: bot.id,
      botRevision: 2,
      botRevisionId: bot.currentRevisionId,
      botRevisionDigest: candidateDigest,
      name: bot.name,
      requestedRace: "terran",
      resolvedRace: "terran",
      difficulty: null,
      result: "victory" as const,
    };
    const opponentParticipant = {
      slot: 2,
      participantType: "bot" as const,
      botId: "99999999-9999-4999-8999-999999999999",
      botRevision: 7,
      botRevisionId: "77777777-7777-4777-8777-777777777777",
      botRevisionDigest: opponentDigest,
      name: "Opponent",
      requestedRace: "protoss",
      resolvedRace: "protoss",
      difficulty: null,
      result: "defeat" as const,
    };

    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path.includes("/stats")) return stats as never;
      if (path.includes("/revisions")) return revisions as never;
      if (path === "/benchmarks" || path.includes("/regressions")) return [] as never;
      if (path === "/runtime/maps") return { maps: ["TestMap"] } as never;
      if (path === "/bots") return [bot] as never;
      if (path.includes("/matches")) {
        return {
          items: [
            {
              id: "88888888-8888-4888-8888-888888888888",
              source: "single",
              mapName: "TestMap",
              status: "completed",
              gameTimeSeconds: 123,
              returnCode: 0,
              failureReason: null,
              regressionBatchId: null,
              createdAt: "2026-07-28T12:00:00Z",
              startedAt: "2026-07-28T12:00:00Z",
              finishedAt: "2026-07-28T12:03:00Z",
              participants: [candidateParticipant, opponentParticipant],
              perspectiveResult: "victory",
              opponent: opponentParticipant,
            },
          ],
          total: 1,
        } as never;
      }
      throw new Error(`Unexpected API path: ${path}`);
    });

    render(<StatsPage bot={bot} onBack={() => undefined} onRun={() => undefined} />);

    expect(
      await screen.findByText(/TestMap · protoss · opponent v7@cccccccc/),
    ).toBeInTheDocument();
    expect(screen.getByText(/tested v2@aaaaaaaa/)).toBeInTheDocument();
  });
});
