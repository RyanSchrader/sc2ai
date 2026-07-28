import { useState } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type {
  ActionFieldSpec,
  ActionSpec,
  Catalog,
  Condition,
  Race,
  StrategyAction,
} from "../models";
import { ActionEditor, ConditionEditor } from "./BotEditor";

function actionSpec(
  label: string,
  races: Race[],
  fields: ActionFieldSpec[],
  defaultsByRace: ActionSpec["defaultsByRace"],
): ActionSpec {
  return {
    label,
    description: `${label} description.`,
    requiredFields: fields.filter((field) => field.required).map((field) => field.name),
    optionalFields: fields.filter((field) => !field.required).map((field) => field.name),
    races,
    fields,
    defaultsByRace,
  };
}

const allRaces: Race[] = ["terran", "protoss", "zerg"];
const catalog: Catalog = {
  schemaVersions: [1],
  races: allRaces,
  units: {
    terran: ["SCV", "MARINE"],
    protoss: ["PROBE", "ZEALOT", "STALKER"],
    zerg: ["DRONE", "OVERLORD", "ZERGLING"],
  },
  structures: {
    terran: ["COMMANDCENTER", "SUPPLYDEPOT", "BARRACKS", "REFINERY"],
    protoss: [
      "NEXUS",
      "PYLON",
      "GATEWAY",
      "CYBERNETICSCORE",
      "ASSIMILATOR",
      "PHOTONCANNON",
    ],
    zerg: ["HATCHERY", "SPAWNINGPOOL", "EXTRACTOR"],
  },
  upgrades: {
    terran: ["TERRANINFANTRYWEAPONSLEVEL1"],
    protoss: ["WARPGATERESEARCH"],
    zerg: ["ZERGLINGMOVEMENTSPEED"],
  },
  unitMetadata: {
    SCV: { race: "terran", roles: ["worker"], producer: "COMMANDCENTER" },
    MARINE: { race: "terran", roles: ["combat"], producer: "BARRACKS" },
    PROBE: { race: "protoss", roles: ["worker"], producer: "NEXUS" },
    ZEALOT: { race: "protoss", roles: ["combat"], producer: "GATEWAY" },
    STALKER: { race: "protoss", roles: ["combat"], producer: "GATEWAY" },
    DRONE: { race: "zerg", roles: ["worker"], producer: null },
    OVERLORD: { race: "zerg", roles: ["supply"], producer: null },
    ZERGLING: { race: "zerg", roles: ["combat"], producer: null },
  },
  structureMetadata: {
    COMMANDCENTER: { race: "terran", roles: ["townhall"] },
    SUPPLYDEPOT: { race: "terran", roles: ["supply", "forward"] },
    BARRACKS: { race: "terran", roles: ["production", "forward"] },
    REFINERY: { race: "terran", roles: ["gas"] },
    NEXUS: { race: "protoss", roles: ["townhall"] },
    PYLON: { race: "protoss", roles: ["supply", "forward"] },
    GATEWAY: { race: "protoss", roles: ["production", "forward"] },
    CYBERNETICSCORE: { race: "protoss", roles: ["tech"] },
    ASSIMILATOR: { race: "protoss", roles: ["gas"] },
    PHOTONCANNON: { race: "protoss", roles: ["defense", "forward"] },
    HATCHERY: { race: "zerg", roles: ["townhall"] },
    SPAWNINGPOOL: { race: "zerg", roles: ["tech"] },
    EXTRACTOR: { race: "zerg", roles: ["gas"] },
  },
  upgradeMetadata: {
    TERRANINFANTRYWEAPONSLEVEL1: {
      race: "terran",
      producer: "BARRACKS",
    },
    WARPGATERESEARCH: { race: "protoss", producer: "CYBERNETICSCORE" },
    ZERGLINGMOVEMENTSPEED: { race: "zerg", producer: "SPAWNINGPOOL" },
  },
  entityDefaults: {
    terran: {
      worker: "SCV",
      combatUnit: "MARINE",
      townhall: "COMMANDCENTER",
      gas: "REFINERY",
      supply: "SUPPLYDEPOT",
      upgrade: "TERRANINFANTRYWEAPONSLEVEL1",
    },
    protoss: {
      worker: "PROBE",
      combatUnit: "ZEALOT",
      townhall: "NEXUS",
      gas: "ASSIMILATOR",
      supply: "PYLON",
      upgrade: "WARPGATERESEARCH",
    },
    zerg: {
      worker: "DRONE",
      combatUnit: "ZERGLING",
      townhall: "HATCHERY",
      gas: "EXTRACTOR",
      supply: "OVERLORD",
      upgrade: "ZERGLINGMOVEMENTSPEED",
    },
  },
  conditionKinds: ["always", "all", "any", "not", "metric"],
  metrics: [
    "workers",
    "minerals",
    "unit_count",
    "structure_count",
    "enemy_unit_count",
  ],
  comparators: ["lt", "lte", "eq", "gte", "gt"],
  actionTypes: [
    "distribute_workers",
    "build_structure",
    "build_forward",
    "train_units",
    "scout",
    "defend",
    "retreat",
    "research",
  ],
  actionSpecs: {
    distribute_workers: actionSpec(
      "Distribute workers",
      allRaces,
      [],
      Object.fromEntries(
        allRaces.map((race) => [race, { type: "distribute_workers" }]),
      ),
    ),
    build_structure: actionSpec(
      "Build structure",
      allRaces,
      [
        {
          name: "structure",
          kind: "structure",
          required: true,
          structureRoles: ["supply", "production", "tech", "defense"],
        },
        { name: "amount", kind: "integer", required: true },
        { name: "distance", kind: "number", required: false },
      ],
      {
        protoss: {
          type: "build_structure",
          structure: "GATEWAY",
          amount: 1,
          distance: 7,
        },
        terran: { type: "build_structure", structure: "BARRACKS", amount: 1 },
        zerg: { type: "build_structure", structure: "SPAWNINGPOOL", amount: 1 },
      },
    ),
    build_forward: actionSpec(
      "Build forward",
      ["terran", "protoss"],
      [
        {
          name: "structure",
          kind: "structure",
          required: true,
          structureRoles: ["forward"],
        },
      ],
      {
        protoss: { type: "build_forward", structure: "PYLON" },
        terran: { type: "build_forward", structure: "SUPPLYDEPOT" },
      },
    ),
    train_units: actionSpec(
      "Train units",
      allRaces,
      [
        { name: "unit", kind: "unit", required: false, unitRoles: ["combat"] },
        { name: "units", kind: "unit_list", required: false, unitRoles: ["combat"] },
        {
          name: "fallback_units",
          kind: "unit_list",
          required: false,
          unitRoles: ["combat"],
        },
      ],
      {
        protoss: { type: "train_units", unit: "ZEALOT" },
        terran: { type: "train_units", unit: "MARINE" },
        zerg: { type: "train_units", unit: "ZERGLING" },
      },
    ),
    scout: actionSpec(
      "Scout",
      allRaces,
      [
        { name: "unit", kind: "unit", required: true },
        {
          name: "target",
          kind: "target",
          required: false,
          targets: ["enemy_start", "map_center", "least_scouted_expansion"],
        },
      ],
      {
        protoss: {
          type: "scout",
          unit: "PROBE",
          target: "enemy_start",
        },
        terran: { type: "scout", unit: "SCV", target: "enemy_start" },
        zerg: { type: "scout", unit: "DRONE", target: "enemy_start" },
      },
    ),
    defend: actionSpec(
      "Defend",
      allRaces,
      [
        { name: "units", kind: "unit_list", required: true, unitRoles: ["combat"] },
        { name: "min_size", kind: "integer", required: true },
        {
          name: "target",
          kind: "target",
          required: false,
          targets: ["main", "map_center"],
        },
        { name: "distance", kind: "number", required: false },
      ],
      {
        protoss: {
          type: "defend",
          units: ["ZEALOT"],
          min_size: 1,
          target: "main",
          distance: 30,
        },
        terran: { type: "defend", units: ["MARINE"], min_size: 1 },
        zerg: { type: "defend", units: ["ZERGLING"], min_size: 1 },
      },
    ),
    retreat: actionSpec(
      "Retreat wounded units",
      allRaces,
      [
        { name: "units", kind: "unit_list", required: true, unitRoles: ["combat"] },
        { name: "health_threshold", kind: "ratio", required: true },
        {
          name: "target",
          kind: "target",
          required: false,
          targets: ["main", "map_center"],
        },
      ],
      {
        protoss: {
          type: "retreat",
          units: ["ZEALOT"],
          health_threshold: 0.35,
          target: "main",
        },
        terran: {
          type: "retreat",
          units: ["MARINE"],
          health_threshold: 0.35,
        },
        zerg: {
          type: "retreat",
          units: ["ZERGLING"],
          health_threshold: 0.35,
        },
      },
    ),
    research: actionSpec(
      "Research upgrade",
      allRaces,
      [{ name: "upgrade", kind: "upgrade", required: true }],
      {
        protoss: { type: "research", upgrade: "WARPGATERESEARCH" },
        terran: {
          type: "research",
          upgrade: "TERRANINFANTRYWEAPONSLEVEL1",
        },
        zerg: { type: "research", upgrade: "ZERGLINGMOVEMENTSPEED" },
      },
    ),
  },
  executionPolicies: ["continuous", "once", "cooldown"],
  targets: ["main", "map_center", "enemy_start", "least_scouted_expansion"],
};

function Harness({ race = "protoss" }: { race?: Race }) {
  const [action, setAction] = useState<StrategyAction>({
    type: "distribute_workers",
  });
  return (
    <>
      <ActionEditor
        action={action}
        catalog={catalog}
        race={race}
        onChange={setAction}
        onDelete={() => undefined}
      />
      <output data-testid="action-state">{JSON.stringify(action)}</output>
    </>
  );
}

function ConditionHarness() {
  const [condition, setCondition] = useState<Condition>({ kind: "always" });
  return (
    <>
      <ConditionEditor
        condition={condition}
        catalog={catalog}
        race="protoss"
        onChange={setCondition}
      />
      <output data-testid="condition-state">{JSON.stringify(condition)}</output>
    </>
  );
}

describe("ActionEditor", () => {
  afterEach(cleanup);

  it("builds new action controls from race-specific catalog defaults", () => {
    render(<Harness />);
    const actionType = screen.getByLabelText("Action type");

    fireEvent.change(actionType, { target: { value: "scout" } });
    expect(screen.getByLabelText("Primary unit")).toHaveValue("PROBE");
    expect(screen.getByLabelText("Destination")).toHaveValue("enemy_start");
    expect(screen.queryByLabelText("Distance")).not.toBeInTheDocument();

    fireEvent.change(actionType, { target: { value: "retreat" } });
    expect(screen.getByLabelText("Health threshold")).toHaveValue(0.35);
    expect(screen.getByLabelText("Destination")).toHaveValue("main");

    fireEvent.change(actionType, { target: { value: "research" } });
    expect(screen.getByLabelText("Upgrade")).toHaveValue("WARPGATERESEARCH");
  });

  it("filters entity choices by declared roles and filters actions by race", () => {
    const { unmount } = render(<Harness />);
    const actionType = screen.getByLabelText("Action type");

    fireEvent.change(actionType, { target: { value: "build_structure" } });
    const structureOptions = within(screen.getByLabelText("Structure"))
      .getAllByRole("option")
      .map((option) => (option as HTMLOptionElement).value);
    expect(structureOptions).toEqual([
      "",
      "PYLON",
      "GATEWAY",
      "CYBERNETICSCORE",
      "PHOTONCANNON",
    ]);
    expect(structureOptions).not.toContain("NEXUS");
    expect(structureOptions).not.toContain("ASSIMILATOR");

    fireEvent.change(actionType, { target: { value: "defend" } });
    const army = screen.getByRole("group", { name: "Army units" });
    expect(within(army).getByRole("checkbox", { name: "ZEALOT" })).toBeChecked();
    expect(within(army).queryByRole("checkbox", { name: "PROBE" })).not.toBeInTheDocument();

    unmount();
    render(<Harness race="zerg" />);
    expect(
      within(screen.getByLabelText("Action type")).queryByRole("option", {
        name: "Build forward",
      }),
    ).not.toBeInTheDocument();
  });

  it("keeps train_units singular and list primary forms mutually exclusive", () => {
    render(<Harness />);
    fireEvent.change(screen.getByLabelText("Action type"), {
      target: { value: "train_units" },
    });
    expect(screen.getByLabelText("Primary unit")).toHaveValue("ZEALOT");

    const army = screen.getByRole("group", { name: "Army units" });
    fireEvent.click(within(army).getByRole("checkbox", { name: "STALKER" }));

    expect(screen.getByLabelText("Primary unit")).toHaveValue("");
    expect(screen.getByTestId("action-state")).toHaveTextContent(
      '{"type":"train_units","units":["STALKER"],"fallback_units":[]}',
    );
    expect(
      within(screen.getByRole("group", { name: "Fallback units" })).queryByRole(
        "checkbox",
        { name: "STALKER" },
      ),
    ).not.toBeInTheDocument();
  });
});

describe("ConditionEditor", () => {
  afterEach(cleanup);

  it("emits lean condition shapes and clears metric-specific fields", () => {
    render(<ConditionHarness />);
    expect(screen.getByTestId("condition-state")).toHaveTextContent(
      '{"kind":"always"}',
    );

    fireEvent.change(screen.getByLabelText("Condition kind"), {
      target: { value: "metric" },
    });
    expect(screen.getByTestId("condition-state")).toHaveTextContent(
      '{"kind":"metric","metric":"workers","comparator":"gte","value":0}',
    );

    fireEvent.change(screen.getByLabelText("Metric"), {
      target: { value: "structure_count" },
    });
    fireEvent.change(screen.getByLabelText("Subject"), {
      target: { value: "PYLON" },
    });
    expect(screen.getByTestId("condition-state")).toHaveTextContent(
      '{"kind":"metric","metric":"structure_count","comparator":"gte","value":0,"status":"total","subject":"PYLON"}',
    );

    fireEvent.change(screen.getByLabelText("Metric"), {
      target: { value: "minerals" },
    });
    expect(screen.queryByLabelText("Subject")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Structure status")).not.toBeInTheDocument();
    expect(screen.getByTestId("condition-state")).toHaveTextContent(
      '{"kind":"metric","metric":"minerals","comparator":"gte","value":0}',
    );

    fireEvent.change(screen.getByLabelText("Condition kind"), {
      target: { value: "all" },
    });
    expect(screen.getByTestId("condition-state")).toHaveTextContent(
      '{"kind":"all","children":[{"kind":"always"},{"kind":"always"}]}',
    );
  });

  it("offers all known races for enemy unit subjects", () => {
    render(<ConditionHarness />);
    fireEvent.change(screen.getByLabelText("Condition kind"), {
      target: { value: "metric" },
    });
    fireEvent.change(screen.getByLabelText("Metric"), {
      target: { value: "enemy_unit_count" },
    });

    const subjects = within(screen.getByLabelText("Subject"))
      .getAllByRole("option")
      .map((option) => (option as HTMLOptionElement).value);
    expect(subjects).toEqual([
      "",
      "DRONE",
      "MARINE",
      "OVERLORD",
      "PROBE",
      "SCV",
      "STALKER",
      "ZEALOT",
      "ZERGLING",
    ]);
  });
});
