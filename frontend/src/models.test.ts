import { describe, expect, it } from "vitest";
import { blankStrategy, summarizeAction, summarizeCondition } from "./models";

describe("strategy helpers", () => {
  it("creates a valid visual-editor starting point", () => {
    const strategy = blankStrategy("terran");
    expect(strategy.race).toBe("terran");
    expect(strategy.phases).toHaveLength(1);
    expect(strategy.phases[0].rules[0].actions[0].type).toBe("distribute_workers");
  });

  it("renders readable trigger and action summaries", () => {
    expect(
      summarizeCondition({
        kind: "metric",
        metric: "workers",
        comparator: "gte",
        value: 22,
      }),
    ).toContain("workers gte 22");
    expect(summarizeAction({ type: "attack", units: ["ZEALOT"], min_size: 8 })).toContain(
      "ZEALOT",
    );
  });
});
