import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { BotSummary } from "../models";
import RunConsole from "./RunConsole";

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

describe("RunConsole", () => {
  beforeEach(() => {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/runtime/maps") return { maps: ["TestMap"] } as never;
      if (path === "/bots") return [bot, opponent] as never;
      throw new Error(`Unexpected API path: ${path}`);
    });
  });

  it("switches from computer settings to a Studio bot opponent", async () => {
    render(<RunConsole bot={bot} onBack={() => undefined} />);
    await screen.findByRole("option", { name: "TestMap" });

    fireEvent.change(screen.getByLabelText("Opponent"), {
      target: { value: "bot" },
    });

    await waitFor(() => {
      expect(screen.getByLabelText("Studio bot")).toHaveValue(opponent.id);
    });
    expect(screen.queryByLabelText("Difficulty")).not.toBeInTheDocument();
  });
});
