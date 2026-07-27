import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { BotStats, BotSummary } from "../models";
import StatsPage from "./StatsPage";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, api: vi.fn() };
});

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
  createdAt: "",
  updatedAt: "",
};

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

describe("StatsPage", () => {
  beforeEach(() => {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path.includes("/stats")) return stats as never;
      if (path.includes("/revisions")) {
        return [
          { id: "r2", number: 2, summary: "Current", created_at: "" },
          { id: "r1", number: 1, summary: "Baseline", created_at: "" },
        ] as never;
      }
      if (path === "/benchmarks" || path.includes("/regressions")) return [] as never;
      if (path === "/runtime/maps") return { maps: ["TestMap"] } as never;
      if (path === "/bots") return [bot] as never;
      if (path.includes("/matches")) {
        return { items: [], total: 0, limit: 100, offset: 0 } as never;
      }
      throw new Error(`Unexpected API path: ${path}`);
    });
  });

  it("shows empty analytics and opens a reusable benchmark editor", async () => {
    render(<StatsPage bot={bot} onBack={() => undefined} onRun={() => undefined} />);

    await screen.findByText("Track results. Catch regressions.");
    expect(screen.getByText("0–0")).toBeInTheDocument();
    expect(screen.getByText("0 total matches")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "+ New suite" }));
    expect(await screen.findByText("Create benchmark suite")).toBeInTheDocument();
    expect(screen.getByLabelText("Map")).toHaveValue("TestMap");
  });
});
