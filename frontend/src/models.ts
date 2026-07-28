export type Race = "terran" | "protoss" | "zerg";
export type Comparator = "lt" | "lte" | "eq" | "gte" | "gt";
export type ConditionKind = "always" | "all" | "any" | "not" | "metric";

export interface Condition {
  kind: ConditionKind;
  metric?: string | null;
  comparator?: Comparator;
  value?: number;
  subject?: string | null;
  status?: "total" | "ready" | "pending";
  children?: Condition[];
}

export interface StrategyAction {
  [key: string]: string | string[] | number | null | undefined;
  type: string;
  unit?: string | null;
  units?: string[];
  fallback_units?: string[];
  structure?: string | null;
  upgrade?: string | null;
  amount?: number | null;
  buffer?: number | null;
  distance?: number;
  min_size?: number | null;
  required_unit?: string | null;
  required_amount?: number | null;
  target?: string;
  health_threshold?: number | null;
}

export interface StrategyRule {
  id: string;
  name: string;
  enabled: boolean;
  priority: number;
  execution: "continuous" | "once" | "cooldown";
  cooldown_seconds?: number;
  trigger: Condition;
  actions: StrategyAction[];
}

export interface StrategyPhase {
  id: string;
  name: string;
  enabled: boolean;
  order: number;
  activation: Condition;
  rules: StrategyRule[];
}

export interface StrategyDocument {
  schema_version: number;
  race: Race;
  opening_chat?: string | null;
  settings: {
    max_supply: number;
    stalemate_detection: boolean;
    stalemate_grace_period_seconds: number;
    stalemate_timeout_seconds: number;
  };
  phases: StrategyPhase[];
}

export interface BotSummary {
  id: string;
  slug: string;
  name: string;
  description: string;
  race: Race;
  tags: string[];
  isBuiltin: boolean;
  forkedFrom: string | null;
  deletedAt: string | null;
  currentRevision: number;
  currentRevisionId?: string;
  currentRevisionDigest?: string;
  createdAt: string;
  updatedAt: string;
  stats?: {
    wins: number;
    losses: number;
    ties: number;
    winRate: number | null;
    totalRuns: number;
  };
}

export interface BotRecord extends BotSummary {
  strategy: StrategyDocument;
  revisionSummary: string;
}

export interface UnitMetadata {
  race: Race;
  roles: string[];
  producer: string | null;
  techRequirement?: string | null;
}

export interface StructureMetadata {
  race: Race;
  roles: string[];
}

export interface UpgradeMetadata {
  race: Race;
  producer: string;
}

export interface EntityDefaults {
  worker: string;
  combatUnit: string;
  townhall: string;
  gas: string;
  supply: string;
  upgrade: string;
}

export interface ActionFieldSpec {
  name: string;
  kind:
    | "integer"
    | "number"
    | "ratio"
    | "unit"
    | "unit_list"
    | "structure"
    | "target"
    | "upgrade";
  required: boolean;
  unitRoles?: string[];
  structureRoles?: string[];
  targets?: string[];
}

export interface ActionSpec {
  label: string;
  description: string;
  handler?: string;
  requiredFields: string[];
  optionalFields: string[];
  races: Race[];
  fields: ActionFieldSpec[];
  defaultsByRace: Partial<Record<Race, StrategyAction>>;
}

export interface Catalog {
  schemaVersions: number[];
  races: Race[];
  units: Record<Race, string[]>;
  structures: Record<Race, string[]>;
  upgrades: Record<Race, string[]>;
  unitMetadata: Record<string, UnitMetadata>;
  structureMetadata: Record<string, StructureMetadata>;
  upgradeMetadata: Record<string, UpgradeMetadata>;
  entityDefaults: Record<Race, EntityDefaults>;
  conditionKinds: ConditionKind[];
  metrics: string[];
  comparators: Comparator[];
  actionTypes: string[];
  actionSpecs: Record<string, ActionSpec>;
  executionPolicies: string[];
  targets: string[];
}

export interface ProposalRecord {
  id: string;
  baseBotId: string | null;
  status: string;
  proposal: {
    summary: string;
    suggested_name: string;
    suggested_slug: string;
    description: string;
    assumptions: string[];
    warnings: string[];
    strategy: StrategyDocument;
  };
}

export interface MatchParticipant {
  slot: number;
  participantType: "bot" | "computer";
  botId: string | null;
  botRevision: number | null;
  botRevisionId?: string | null;
  botRevisionDigest?: string | null;
  name: string;
  requestedRace: string;
  resolvedRace: string | null;
  difficulty: string | null;
  result: "victory" | "defeat" | "tie" | "undecided" | null;
}

export interface MatchRecord {
  id: string;
  source: "single" | "cli" | "regression";
  mapName: string;
  status: string;
  gameTimeSeconds: number | null;
  returnCode: number | null;
  failureReason: string | null;
  regressionBatchId: string | null;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  participants: MatchParticipant[];
  perspectiveResult: MatchParticipant["result"];
  opponent: MatchParticipant;
}

export interface BotStats {
  botId: string;
  totalRuns: number;
  completedMatches: number;
  wins: number;
  losses: number;
  ties: number;
  undecided: number;
  stopped: number;
  failed: number;
  winRate: number | null;
  averageGameTimeSeconds: number | null;
  breakdown: Array<{
    opponentType: string;
    opponentName: string;
    enemyRace: string;
    difficulty: string | null;
    wins: number;
    losses: number;
    ties: number;
    games: number;
  }>;
}

export interface BenchmarkScenario {
  id?: string;
  position?: number;
  name: string;
  mapName: string;
  opponentType: "computer" | "bot";
  enemyRace: string | null;
  difficulty: string | null;
  opponentBotId: string | null;
  opponentRevision: number | null;
  opponentRevisionId?: string | null;
  opponentRevisionDigest?: string | null;
}

export interface BenchmarkSuite {
  id: string;
  name: string;
  description: string;
  archivedAt: string | null;
  createdAt: string;
  updatedAt: string;
  scenarios: BenchmarkScenario[];
}

export interface RegressionGame {
  id: string;
  batchId: string;
  scenarioName: string;
  opponentRevisionId?: string | null;
  opponentRevisionDigest?: string | null;
  testedRole: "candidate" | "baseline";
  testedRevision: number;
  testedRevisionId?: string;
  testedRevisionDigest?: string;
  repetition: number;
  matchId: string | null;
  status: string;
}

export interface RegressionSummary {
  wins: number;
  losses: number;
  ties: number;
  undecided: number;
  winRate: number | null;
  averageGameTimeSeconds: number | null;
}

export interface RegressionBatch {
  id: string;
  botId: string;
  candidateRevision: number;
  baselineRevision: number;
  candidateRevisionId?: string;
  candidateRevisionDigest?: string;
  baselineRevisionId?: string;
  baselineRevisionDigest?: string;
  suiteId: string;
  suiteName: string;
  gamesPerScenario: number;
  concurrency: number;
  status: string;
  totalGames: number;
  completedGames: number;
  pairedSamples: number;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  games: RegressionGame[];
  comparison: {
    candidate: RegressionSummary;
    baseline: RegressionSummary;
    winRateDelta: number | null;
  };
  scenarioComparisons: Array<{
    position: number;
    name: string;
    pairedSamples: number;
    candidate: RegressionSummary;
    baseline: RegressionSummary;
    winRateDelta: number | null;
  }>;
}

export const alwaysCondition = (): Condition => ({ kind: "always" });

export const blankStrategy = (race: Race): StrategyDocument => ({
  schema_version: 1,
  race,
  opening_chat: null,
  settings: {
    max_supply: 200,
    stalemate_detection: true,
    stalemate_grace_period_seconds: 600,
    stalemate_timeout_seconds: 180,
  },
  phases: [
    {
      id: crypto.randomUUID(),
      name: "Opening",
      enabled: true,
      order: 0,
      activation: alwaysCondition(),
      rules: [
        {
          id: crypto.randomUUID(),
          name: "Distribute workers",
          enabled: true,
          priority: 10,
          execution: "continuous",
          trigger: alwaysCondition(),
          actions: [{ type: "distribute_workers" }],
        },
      ],
    },
  ],
});

export function summarizeCondition(condition: Condition): string {
  if (condition.kind === "always") return "Always";
  if (condition.kind === "not") {
    return `Not (${summarizeCondition(condition.children?.[0] ?? alwaysCondition())})`;
  }
  if (condition.kind === "all" || condition.kind === "any") {
    return (condition.children ?? [])
      .map(summarizeCondition)
      .join(condition.kind === "all" ? " and " : " or ");
  }
  const subject = condition.subject ? ` ${condition.subject}` : "";
  return `${condition.metric ?? "metric"}${subject} ${condition.comparator ?? "gte"} ${condition.value ?? 0}`;
}

export function summarizeAction(action: StrategyAction): string {
  const target =
    action.unit ??
    action.structure ??
    action.upgrade ??
    action.units?.join(" + ") ??
    action.target ??
    "";
  const amount =
    action.amount ?? action.buffer ?? action.min_size ?? action.health_threshold;
  return `${action.type.replaceAll("_", " ")}${target ? ` · ${target}` : ""}${amount != null ? ` · ${amount}` : ""}`;
}

export function shortDigest(digest: string | null | undefined, length = 8): string | null {
  return digest ? digest.slice(0, length) : null;
}
