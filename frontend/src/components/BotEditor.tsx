import { useEffect, useMemo, useState } from "react";
import { api, jsonOptions } from "../api";
import {
  alwaysCondition,
  shortDigest,
  summarizeAction,
  summarizeCondition,
  type ActionFieldSpec,
  type BotRecord,
  type BotSummary,
  type Catalog,
  type Condition,
  type ConditionKind,
  type StrategyAction,
  type StrategyPhase,
  type StrategyRule,
} from "../models";
import AssistantPanel from "./AssistantPanel";

interface Props {
  botId: string;
  catalog: Catalog | null;
  onBack: () => void;
  onRun: (bot: BotSummary) => void;
}

interface Revision {
  id: string;
  number: number;
  content_digest?: string;
  summary: string;
  created_at: string;
}

export default function BotEditor({ botId, catalog, onBack, onRun }: Props) {
  const [bot, setBot] = useState<BotRecord | null>(null);
  const [draft, setDraft] = useState<BotRecord | null>(null);
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [selected, setSelected] = useState<{ phase: number; rule: number } | null>(null);
  const [dirty, setDirty] = useState(false);
  const [proposalPending, setProposalPending] = useState(false);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dragPhase, setDragPhase] = useState<number | null>(null);
  const [dragRule, setDragRule] = useState<{ phase: number; rule: number } | null>(null);

  const load = async () => {
    const [record, history] = await Promise.all([
      api<BotRecord>(`/bots/${botId}`),
      api<Revision[]>(`/bots/${botId}/revisions`),
    ]);
    setBot(record);
    setDraft(structuredClone(record));
    setRevisions(history);
    setDirty(false);
    setSelected(record.strategy.phases[0]?.rules[0] ? { phase: 0, rule: 0 } : null);
  };

  useEffect(() => {
    void load().catch((reason) =>
      setError(reason instanceof Error ? reason.message : "Could not load bot."),
    );
    // botId is the only identity input.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [botId]);

  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (!dirty && !proposalPending) return;
      event.preventDefault();
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty, proposalPending]);

  const updateDraft = (mutate: (next: BotRecord) => void) => {
    if (!draft) return;
    const next = structuredClone(draft);
    mutate(next);
    setDraft(next);
    setDirty(true);
    setNotice(null);
  };

  const safeBack = () => {
    if (
      (dirty || proposalPending) &&
      !window.confirm("Leave without saving visual edits or applying the assistant proposal?")
    ) {
      return;
    }
    onBack();
  };

  const save = async () => {
    if (!draft || !bot) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api("/strategies/validate", jsonOptions("POST", draft.strategy));
      const saved = await api<BotRecord>(
        `/bots/${draft.id}`,
        jsonOptions("PATCH", {
          name: draft.name,
          description: draft.description,
          tags: draft.tags,
          strategy: draft.strategy,
          change_summary: "Visual editor changes",
          expected_revision: bot.currentRevision,
        }),
      );
      setBot(saved);
      setDraft(structuredClone(saved));
      setDirty(false);
      setNotice(`Saved revision ${saved.currentRevision}.`);
      setRevisions(await api<Revision[]>(`/bots/${botId}/revisions`));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save strategy.");
    } finally {
      setBusy(false);
    }
  };

  const addPhase = () => {
    if (!draft) return;
    updateDraft((next) => {
      next.strategy.phases.push({
        id: crypto.randomUUID(),
        name: `Phase ${next.strategy.phases.length + 1}`,
        enabled: true,
        order: next.strategy.phases.length,
        activation: alwaysCondition(),
        rules: [],
      });
    });
  };

  const addRule = (phaseIndex: number) => {
    if (!catalog || !draft) return;
    updateDraft((next) => {
      const phase = next.strategy.phases[phaseIndex];
      const type = actionTypesForRace(catalog, next.race)[0] ?? "distribute_workers";
      phase.rules.push({
        id: crypto.randomUUID(),
        name: "New rule",
        enabled: true,
        priority: (phase.rules.length + 1) * 10,
        execution: "continuous",
        trigger: alwaysCondition(),
        actions: [defaultActionFor(catalog, next.race, type)],
      });
      setSelected({ phase: phaseIndex, rule: phase.rules.length - 1 });
    });
  };

  const deleteRule = (phaseIndex: number, ruleIndex: number) => {
    updateDraft((next) => next.strategy.phases[phaseIndex].rules.splice(ruleIndex, 1));
    setSelected(null);
  };

  const deletePhase = (phaseIndex: number) => {
    if (!window.confirm("Delete this phase and all of its rules?")) return;
    updateDraft((next) => {
      next.strategy.phases.splice(phaseIndex, 1);
      next.strategy.phases.forEach((phase, index) => (phase.order = index));
    });
    setSelected(null);
  };

  const dropPhase = (target: number) => {
    if (dragPhase == null || dragPhase === target) return;
    updateDraft((next) => {
      const [phase] = next.strategy.phases.splice(dragPhase, 1);
      next.strategy.phases.splice(target, 0, phase);
      next.strategy.phases.forEach((item, index) => (item.order = index));
    });
    setDragPhase(null);
    setSelected(null);
  };

  const dropRule = (targetPhase: number, targetRule: number) => {
    if (!dragRule || dragRule.phase !== targetPhase || dragRule.rule === targetRule) return;
    updateDraft((next) => {
      const rules = next.strategy.phases[targetPhase].rules;
      const [rule] = rules.splice(dragRule.rule, 1);
      rules.splice(targetRule, 0, rule);
      rules.forEach((item, index) => (item.priority = (index + 1) * 10));
    });
    setDragRule(null);
    setSelected({ phase: targetPhase, rule: targetRule });
  };

  const restoreRevision = async (revision: number) => {
    if (!bot || !window.confirm(`Restore revision ${revision} as a new revision?`)) return;
    const restored = await api<BotRecord>(
      `/bots/${bot.id}/revisions/${revision}/restore`,
      jsonOptions("POST", { change_summary: `Restored revision ${revision}` }),
    );
    setBot(restored);
    setDraft(structuredClone(restored));
    setDirty(false);
    setRevisions(await api<Revision[]>(`/bots/${botId}/revisions`));
  };

  const selectedRule = useMemo(() => {
    if (!draft || !selected) return null;
    return draft.strategy.phases[selected.phase]?.rules[selected.rule] ?? null;
  }, [draft, selected]);

  if (!draft || !bot || !catalog) {
    return (
      <div className="editor-loading">
        {error ? <div className="alert error">{error}</div> : "Loading strategy editor…"}
      </div>
    );
  }

  return (
    <div className="editor-shell">
      <header className="editor-topbar">
        <button className="button ghost" onClick={safeBack}>
          ← Library
        </button>
        <div className="editor-title">
          <input
            value={draft.name}
            onChange={(event) => updateDraft((next) => (next.name = event.target.value))}
          />
          <span>
            {draft.race} · revision {bot.currentRevision}
            {shortDigest(bot.currentRevisionDigest) && (
              <span title={`Content digest ${bot.currentRevisionDigest}`}>
                {" "}
                · {shortDigest(bot.currentRevisionDigest)}
              </span>
            )}
            {dirty && <b className="unsaved"> · unsaved</b>}
          </span>
        </div>
        <div className="editor-actions">
          <button
            className="button secondary"
            onClick={() => onRun(bot)}
            disabled={dirty}
            title={dirty ? "Save the strategy before launching a match." : undefined}
          >
            ▶ Test match
          </button>
          <button className="button ai" onClick={() => setAssistantOpen((value) => !value)}>
            ✦ Modify with text
          </button>
          <button className="button primary" onClick={() => void save()} disabled={busy || !dirty}>
            {busy ? "Saving…" : "Save revision"}
          </button>
        </div>
      </header>

      {(error || notice) && (
        <div className={`editor-banner ${error ? "error" : "success"}`}>
          {error ?? notice}
          <button onClick={() => (setError(null), setNotice(null))}>×</button>
        </div>
      )}

      <div className={`editor-layout ${assistantOpen ? "assistant-open" : ""}`}>
        <aside className="strategy-sidebar">
          <label>
            Description
            <textarea
              rows={4}
              value={draft.description}
              onChange={(event) =>
                updateDraft((next) => (next.description = event.target.value))
              }
            />
          </label>
          <label>
            Opening chat
            <input
              value={draft.strategy.opening_chat ?? ""}
              onChange={(event) =>
                updateDraft((next) => (next.strategy.opening_chat = event.target.value || null))
              }
            />
          </label>
          <label className="check-row">
            <input
              type="checkbox"
              checked={draft.strategy.settings.stalemate_detection}
              onChange={(event) =>
                updateDraft(
                  (next) =>
                    (next.strategy.settings.stalemate_detection =
                      event.target.checked),
                )
              }
            />
            End inactive games as stalemates
          </label>
          {draft.strategy.settings.stalemate_detection && (
            <div className="form-grid">
              <label>
                Stalemate grace period
                <input
                  type="number"
                  min={0}
                  max={7200}
                  step={30}
                  value={draft.strategy.settings.stalemate_grace_period_seconds}
                  onChange={(event) =>
                    updateDraft(
                      (next) =>
                        (next.strategy.settings.stalemate_grace_period_seconds =
                          Number(event.target.value)),
                    )
                  }
                />
                <small>In-game seconds before detection starts</small>
              </label>
              <label>
                Inactivity timeout
                <input
                  type="number"
                  min={60}
                  max={1800}
                  step={30}
                  value={draft.strategy.settings.stalemate_timeout_seconds}
                  onChange={(event) =>
                    updateDraft(
                      (next) =>
                        (next.strategy.settings.stalemate_timeout_seconds =
                          Number(event.target.value)),
                    )
                  }
                />
                <small>Seconds without meaningful progress</small>
              </label>
            </div>
          )}
          <label>
            Tags
            <input
              value={draft.tags.join(", ")}
              onChange={(event) =>
                updateDraft(
                  (next) =>
                    (next.tags = event.target.value
                      .split(",")
                      .map((value) => value.trim())
                      .filter(Boolean)),
                )
              }
            />
          </label>
          <details open>
            <summary>Revision history</summary>
            <div className="revision-list">
              {revisions.map((revision) => (
                <button
                  key={revision.id}
                  disabled={revision.number === bot.currentRevision}
                  onClick={() => void restoreRevision(revision.number)}
                >
                  <strong>v{revision.number}</strong>
                  <span title={revision.content_digest ?? undefined}>
                    {revision.summary}
                    {shortDigest(revision.content_digest)
                      ? ` · ${shortDigest(revision.content_digest)}`
                      : ""}
                  </span>
                </button>
              ))}
            </div>
          </details>
        </aside>

        <main className="flow-canvas">
          <div className="canvas-heading">
            <div>
              <span className="eyebrow">STRATEGY FLOW</span>
              <h2>Phases and rules</h2>
            </div>
            <button className="button secondary small" onClick={addPhase}>
              + Add phase
            </button>
          </div>

          <div className="phase-list">
            {draft.strategy.phases.map((phase, phaseIndex) => (
              <section
                className="phase-card"
                key={phase.id}
                draggable
                onDragStart={() => setDragPhase(phaseIndex)}
                onDragOver={(event) => event.preventDefault()}
                onDrop={() => dropPhase(phaseIndex)}
              >
                <header>
                  <span className="drag-handle">⠿</span>
                  <input
                    value={phase.name}
                    onChange={(event) =>
                      updateDraft(
                        (next) =>
                          (next.strategy.phases[phaseIndex].name = event.target.value),
                      )
                    }
                  />
                  <span className="phase-activation">
                    Starts: {summarizeCondition(phase.activation)}
                  </span>
                  <label className="toggle">
                    <input
                      type="checkbox"
                      checked={phase.enabled}
                      onChange={(event) =>
                        updateDraft(
                          (next) =>
                            (next.strategy.phases[phaseIndex].enabled = event.target.checked),
                        )
                      }
                    />
                    <span />
                  </label>
                  <button className="icon-button danger" onClick={() => deletePhase(phaseIndex)}>
                    ×
                  </button>
                </header>
                <div className="rule-list">
                  {phase.rules.map((rule, ruleIndex) => (
                    <button
                      key={rule.id}
                      className={`rule-card ${
                        selected?.phase === phaseIndex && selected.rule === ruleIndex
                          ? "selected"
                          : ""
                      }`}
                      draggable
                      onDragStart={(event) => {
                        event.stopPropagation();
                        setDragRule({ phase: phaseIndex, rule: ruleIndex });
                      }}
                      onDragOver={(event) => event.preventDefault()}
                      onDrop={(event) => {
                        event.stopPropagation();
                        dropRule(phaseIndex, ruleIndex);
                      }}
                      onClick={() => setSelected({ phase: phaseIndex, rule: ruleIndex })}
                    >
                      <span className="priority">{rule.priority}</span>
                      <span className="rule-copy">
                        <strong>{rule.name}</strong>
                        <small>When {summarizeCondition(rule.trigger)}</small>
                        <small>
                          Then {rule.actions.map(summarizeAction).join(", ")}
                        </small>
                      </span>
                      <span className={`rule-status ${rule.enabled ? "enabled" : ""}`} />
                    </button>
                  ))}
                  <button className="add-rule" onClick={() => addRule(phaseIndex)}>
                    + Add rule
                  </button>
                </div>
              </section>
            ))}
          </div>
        </main>

        <aside className="inspector">
          {selected && selectedRule ? (
            <RuleInspector
              rule={selectedRule}
              phase={draft.strategy.phases[selected.phase]}
              catalog={catalog}
              race={draft.race}
              onChange={(rule) =>
                updateDraft(
                  (next) =>
                    (next.strategy.phases[selected.phase].rules[selected.rule] = rule),
                )
              }
              onPhaseChange={(phase) =>
                updateDraft(
                  (next) => (next.strategy.phases[selected.phase] = phase),
                )
              }
              onDelete={() => deleteRule(selected.phase, selected.rule)}
            />
          ) : (
            <div className="inspector-empty">
              <span>↖</span>
              <strong>Select a rule</strong>
              <p>Choose a rule card to edit its trigger, actions, and execution policy.</p>
            </div>
          )}
        </aside>

        {assistantOpen && (
          <aside className="assistant-drawer">
            <header>
              <div>
                <span className="eyebrow">LOCAL ASSISTANT</span>
                <h2>Modify this strategy</h2>
              </div>
              <button className="icon-button" onClick={() => setAssistantOpen(false)}>
                ×
              </button>
            </header>
            <AssistantPanel
              mode="modify"
              bot={bot}
              strategy={draft.strategy}
              onProposalPendingChange={setProposalPending}
              onApplied={(saved) => {
                setBot(saved);
                setDraft(structuredClone(saved));
                setDirty(false);
                setProposalPending(false);
                setNotice(`Applied assistant proposal as revision ${saved.currentRevision}.`);
                void api<Revision[]>(`/bots/${botId}/revisions`).then(setRevisions);
              }}
            />
          </aside>
        )}
      </div>
    </div>
  );
}

function RuleInspector({
  rule,
  phase,
  catalog,
  race,
  onChange,
  onPhaseChange,
  onDelete,
}: {
  rule: StrategyRule;
  phase: StrategyPhase;
  catalog: Catalog;
  race: BotRecord["race"];
  onChange: (rule: StrategyRule) => void;
  onPhaseChange: (phase: StrategyPhase) => void;
  onDelete: () => void;
}) {
  const update = (mutate: (next: StrategyRule) => void) => {
    const next = structuredClone(rule);
    mutate(next);
    onChange(next);
  };

  return (
    <div className="inspector-content">
      <div className="inspector-heading">
        <span className="eyebrow">{phase.name.toUpperCase()}</span>
        <h2>Rule inspector</h2>
      </div>
      <label>
        Rule name
        <input value={rule.name} onChange={(event) => update((next) => (next.name = event.target.value))} />
      </label>
      <div className="form-grid">
        <label>
          Priority
          <input
            type="number"
            value={rule.priority}
            onChange={(event) => update((next) => (next.priority = Number(event.target.value)))}
          />
        </label>
        <label>
          Execution
          <select
            value={rule.execution}
            onChange={(event) =>
              update((next) => {
                const execution = event.target.value as StrategyRule["execution"];
                next.execution = execution;
                if (execution === "cooldown") {
                  next.cooldown_seconds = Math.max(0.1, next.cooldown_seconds ?? 1);
                } else {
                  delete next.cooldown_seconds;
                }
              })
            }
          >
            {catalog.executionPolicies.map((policy) => (
              <option key={policy}>{policy}</option>
            ))}
          </select>
        </label>
      </div>
      {rule.execution === "cooldown" && (
        <label>
          Cooldown seconds
          <input
            type="number"
            min="0.1"
            step="0.1"
            value={rule.cooldown_seconds ?? 1}
            onChange={(event) =>
              update((next) => (next.cooldown_seconds = Number(event.target.value)))
            }
          />
        </label>
      )}
      <label className="check-row">
        <input
          type="checkbox"
          checked={rule.enabled}
          onChange={(event) => update((next) => (next.enabled = event.target.checked))}
        />
        Rule enabled
      </label>

      <section className="inspector-section">
        <div className="section-title">
          <span>PHASE</span>
          <strong>Activation</strong>
        </div>
        <ConditionEditor
          condition={phase.activation}
          catalog={catalog}
          race={race}
          onChange={(activation) =>
            onPhaseChange({ ...structuredClone(phase), activation })
          }
        />
      </section>

      <section className="inspector-section">
        <div className="section-title">
          <span>WHEN</span>
          <strong>Trigger</strong>
        </div>
        <ConditionEditor
          condition={rule.trigger}
          catalog={catalog}
          race={race}
          onChange={(trigger) => update((next) => (next.trigger = trigger))}
        />
      </section>

      <section className="inspector-section">
        <div className="section-title">
          <span>THEN</span>
          <strong>Actions</strong>
        </div>
        <div className="action-list">
          {rule.actions.map((action, index) => (
            <ActionEditor
              key={index}
              action={action}
              catalog={catalog}
              race={race}
              onChange={(value) =>
                update((next) => {
                  next.actions[index] = value;
                })
              }
              onDelete={() =>
                update((next) => {
                  next.actions.splice(index, 1);
                })
              }
            />
          ))}
          <button
            className="add-action"
            onClick={() => {
              const type = actionTypesForRace(catalog, race)[0] ?? "distribute_workers";
              update((next) =>
                next.actions.push(defaultActionFor(catalog, race, type)),
              );
            }}
          >
            + Add action
          </button>
        </div>
      </section>
      <button className="button danger-outline" onClick={onDelete}>
        Delete rule
      </button>
    </div>
  );
}

export function ConditionEditor({
  condition,
  catalog,
  race,
  onChange,
  depth = 0,
}: {
  condition: Condition;
  catalog: Catalog;
  race: BotRecord["race"];
  onChange: (condition: Condition) => void;
  depth?: number;
}) {
  const update = (mutate: (next: Condition) => void) => {
    const next = structuredClone(condition);
    mutate(next);
    onChange(next);
  };
  const subjectOptions =
    condition.metric === "structure_count"
      ? catalog.structures[race]
      : condition.metric === "enemy_unit_count"
        ? [...new Set(Object.values(catalog.units).flat())].sort()
        : catalog.units[race];
  const needsSubject = ["unit_count", "structure_count", "enemy_unit_count"].includes(
    condition.metric ?? "",
  );

  const setKind = (kind: ConditionKind) => {
    if (kind === "always") {
      onChange(alwaysCondition());
    } else if (kind === "not") {
      onChange({ kind, children: [alwaysCondition()] });
    } else if (kind === "all" || kind === "any") {
      onChange({ kind, children: [alwaysCondition(), alwaysCondition()] });
    } else {
      onChange({
        kind,
        metric: "workers",
        comparator: "gte",
        value: 0,
      });
    }
  };

  return (
    <div className={`condition-editor depth-${Math.min(depth, 2)}`}>
      <select
        aria-label="Condition kind"
        value={condition.kind}
        onChange={(event) => setKind(event.target.value as ConditionKind)}
      >
        {catalog.conditionKinds.map((kind) => (
          <option key={kind}>{kind}</option>
        ))}
      </select>
      {condition.kind === "metric" && (
        <>
          <select
            aria-label="Metric"
            value={condition.metric ?? "workers"}
            onChange={(event) =>
              update((next) => {
                next.metric = event.target.value;
                delete next.subject;
                delete next.status;
                if (event.target.value === "structure_count") {
                  next.status = "total";
                }
              })
            }
          >
            {catalog.metrics.map((metric) => (
              <option key={metric}>{metric}</option>
            ))}
          </select>
          {needsSubject && (
            <select
              aria-label="Subject"
              value={condition.subject ?? ""}
              onChange={(event) =>
                update((next) => {
                  if (event.target.value) {
                    next.subject = event.target.value;
                  } else {
                    delete next.subject;
                  }
                })
              }
            >
              <option value="">Select subject</option>
              {subjectOptions.map((subject) => (
                <option key={subject}>{subject}</option>
              ))}
            </select>
          )}
          {condition.metric === "structure_count" && (
            <select
              aria-label="Structure status"
              value={condition.status ?? "total"}
              onChange={(event) =>
                update(
                  (next) =>
                    (next.status = event.target.value as Condition["status"]),
                )
              }
            >
              <option value="total">total + pending</option>
              <option value="ready">ready</option>
              <option value="pending">pending</option>
            </select>
          )}
          <div className="condition-comparison">
            <select
              aria-label="Comparator"
              value={condition.comparator ?? "gte"}
              onChange={(event) =>
                update(
                  (next) =>
                    (next.comparator = event.target.value as Condition["comparator"]),
                )
              }
            >
              {catalog.comparators.map((comparator) => (
                <option key={comparator}>{comparator}</option>
              ))}
            </select>
            <input
              aria-label="Comparison value"
              type="number"
              value={condition.value ?? 0}
              onChange={(event) => update((next) => (next.value = Number(event.target.value)))}
            />
          </div>
        </>
      )}
      {["all", "any", "not"].includes(condition.kind) && (
        <div className="nested-conditions">
          {(condition.children ?? []).map((child, index) => (
            <div className="nested-condition" key={index}>
              <ConditionEditor
                condition={child}
                catalog={catalog}
                race={race}
                depth={depth + 1}
                onChange={(value) =>
                  update((next) => {
                    (next.children ??= [])[index] = value;
                  })
                }
              />
              {condition.kind !== "not" && (
                <button
                  className="icon-button danger"
                  onClick={() =>
                    update((next) => {
                      next.children?.splice(index, 1);
                    })
                  }
                >
                  ×
                </button>
              )}
            </div>
          ))}
          {condition.kind !== "not" && (
            <button
              className="add-action"
              onClick={() =>
                update((next) => (next.children ??= []).push(alwaysCondition()))
              }
            >
              + Add condition
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function actionTypesForRace(catalog: Catalog, race: BotRecord["race"]): string[] {
  return catalog.actionTypes.filter((type) =>
    catalog.actionSpecs[type]?.races.includes(race),
  );
}

function defaultActionFor(
  catalog: Catalog,
  race: BotRecord["race"],
  type: string,
): StrategyAction {
  return structuredClone(catalog.actionSpecs[type]?.defaultsByRace[race] ?? { type });
}

function hasAllowedRole(entityRoles: string[], allowedRoles: string[] | undefined): boolean {
  return !allowedRoles?.length || allowedRoles.some((role) => entityRoles.includes(role));
}

function unitOptionsForField(
  catalog: Catalog,
  race: BotRecord["race"],
  field: ActionFieldSpec,
  action: StrategyAction,
): string[] {
  let options = catalog.units[race].filter((unit) => {
    const metadata = catalog.unitMetadata[unit];
    return (
      metadata?.race === race &&
      hasAllowedRole(metadata.roles, field.unitRoles)
    );
  });

  if (field.name === "fallback_units") {
    const primary = new Set([
      ...(action.unit ? [action.unit] : []),
      ...(action.units ?? []),
    ]);
    options = options.filter((unit) => !primary.has(unit));
  }

  if (field.name === "required_unit" && action.type === "attack") {
    const army = new Set(action.units ?? []);
    options = options.filter((unit) => army.has(unit));
  }

  return options;
}

function structureOptionsForField(
  catalog: Catalog,
  race: BotRecord["race"],
  field: ActionFieldSpec,
): string[] {
  return catalog.structures[race].filter((structure) => {
    const metadata = catalog.structureMetadata[structure];
    return (
      metadata?.race === race &&
      hasAllowedRole(metadata.roles, field.structureRoles)
    );
  });
}

const ACTION_FIELD_LABELS: Record<string, string> = {
  amount: "Target count",
  buffer: "Supply buffer",
  distance: "Distance",
  fallback_units: "Fallback units",
  health_threshold: "Health threshold",
  min_size: "Minimum army size",
  required_amount: "Required count",
  required_unit: "Required unit",
  structure: "Structure",
  target: "Destination",
  unit: "Primary unit",
  units: "Army units",
  upgrade: "Upgrade",
};

function fieldLabel(name: string): string {
  return ACTION_FIELD_LABELS[name] ?? formatCatalogValue(name);
}

function formatCatalogValue(value: string): string {
  const words = value.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function ActionEditor({
  action,
  catalog,
  race,
  onChange,
  onDelete,
}: {
  action: StrategyAction;
  catalog: Catalog;
  race: BotRecord["race"];
  onChange: (action: StrategyAction) => void;
  onDelete: () => void;
}) {
  const update = (mutate: (next: StrategyAction) => void) => {
    const next = structuredClone(action);
    mutate(next);
    onChange(next);
  };
  const availableTypes = actionTypesForRace(catalog, race);
  const spec = catalog.actionSpecs[action.type];
  const unsupportedCurrentType = !availableTypes.includes(action.type);

  return (
    <div className="action-editor">
      <div className="action-header">
        <select
          aria-label="Action type"
          value={action.type}
          onChange={(event) =>
            onChange(defaultActionFor(catalog, race, event.target.value))
          }
        >
          {unsupportedCurrentType && (
            <option value={action.type} disabled>
              {spec?.label ?? formatCatalogValue(action.type)} (unsupported for {race})
            </option>
          )}
          {availableTypes.map((type) => (
            <option key={type} value={type}>
              {catalog.actionSpecs[type].label}
            </option>
          ))}
        </select>
        <button
          aria-label={`Delete ${spec?.label ?? action.type} action`}
          className="icon-button danger"
          onClick={onDelete}
        >
          ×
        </button>
      </div>
      {spec?.description && <small>{spec.description}</small>}
      {spec?.fields.map((field) => {
        if (field.name === "required_amount" && !action.required_unit) return null;

        if (field.kind === "unit_list") {
          const options = unitOptionsForField(catalog, race, field, action);
          const selected = Array.isArray(action[field.name])
            ? (action[field.name] as string[])
            : [];
          return (
            <fieldset key={field.name}>
              <legend>{fieldLabel(field.name)}</legend>
              <div className="unit-checks">
                {options.map((unit) => (
                  <label key={unit}>
                    <input
                      type="checkbox"
                      checked={selected.includes(unit)}
                      onChange={(event) =>
                        update((next) => {
                          const values = new Set(
                            Array.isArray(next[field.name])
                              ? (next[field.name] as string[])
                              : [],
                          );
                          event.target.checked ? values.add(unit) : values.delete(unit);
                          next[field.name] = [...values];

                          if (field.name === "units") {
                            if (next.type === "train_units" && values.size) {
                              delete next.unit;
                            }
                            next.fallback_units = (next.fallback_units ?? []).filter(
                              (fallback) => !values.has(fallback),
                            );
                            if (
                              next.required_unit &&
                              !values.has(next.required_unit)
                            ) {
                              delete next.required_unit;
                              delete next.required_amount;
                            }
                          }
                        })
                      }
                    />
                    {unit}
                  </label>
                ))}
              </div>
              {field.required && selected.length === 0 && (
                <small>Choose at least one.</small>
              )}
            </fieldset>
          );
        }

        if (field.kind === "unit") {
          const options = unitOptionsForField(catalog, race, field, action);
          const currentValue = action[field.name];
          return (
            <label key={field.name}>
              {fieldLabel(field.name)}
              <select
                value={typeof currentValue === "string" ? currentValue : ""}
                onChange={(event) =>
                  update((next) => {
                    const value = event.target.value;
                    if (value) {
                      next[field.name] = value;
                    } else {
                      delete next[field.name];
                    }

                    if (field.name === "unit" && next.type === "train_units" && value) {
                      delete next.units;
                      next.fallback_units = (next.fallback_units ?? []).filter(
                        (fallback) => fallback !== value,
                      );
                    }
                    if (field.name === "required_unit") {
                      if (value) {
                        next.required_amount = Math.max(1, next.required_amount ?? 1);
                      } else {
                        delete next.required_amount;
                      }
                    }
                  })
                }
              >
                <option value="">
                  {field.required ? `Select ${fieldLabel(field.name).toLowerCase()}` : "None"}
                </option>
                {options.map((unit) => (
                  <option key={unit} value={unit}>
                    {unit}
                  </option>
                ))}
              </select>
            </label>
          );
        }

        if (field.kind === "structure") {
          const options = structureOptionsForField(catalog, race, field);
          const currentValue = action[field.name];
          return (
            <label key={field.name}>
              {fieldLabel(field.name)}
              <select
                value={typeof currentValue === "string" ? currentValue : ""}
                onChange={(event) =>
                  update((next) => {
                    if (event.target.value) {
                      next[field.name] = event.target.value;
                    } else {
                      delete next[field.name];
                    }
                  })
                }
              >
                <option value="">
                  {field.required ? `Select ${fieldLabel(field.name).toLowerCase()}` : "None"}
                </option>
                {options.map((structure) => (
                  <option key={structure} value={structure}>
                    {structure}
                  </option>
                ))}
              </select>
            </label>
          );
        }

        if (field.kind === "upgrade") {
          const options = catalog.upgrades[race].filter(
            (upgrade) => catalog.upgradeMetadata[upgrade]?.race === race,
          );
          const currentValue = action[field.name];
          return (
            <label key={field.name}>
              {fieldLabel(field.name)}
              <select
                value={typeof currentValue === "string" ? currentValue : ""}
                onChange={(event) =>
                  update((next) => {
                    if (event.target.value) {
                      next[field.name] = event.target.value;
                    } else {
                      delete next[field.name];
                    }
                  })
                }
              >
                <option value="">
                  {field.required ? `Select ${fieldLabel(field.name).toLowerCase()}` : "None"}
                </option>
                {options.map((upgrade) => (
                  <option key={upgrade} value={upgrade}>
                    {formatCatalogValue(upgrade)}
                  </option>
                ))}
              </select>
            </label>
          );
        }

        if (field.kind === "target") {
          const currentValue = action[field.name];
          return (
            <label key={field.name}>
              {fieldLabel(field.name)}
              <select
                value={typeof currentValue === "string" ? currentValue : ""}
                onChange={(event) =>
                  update((next) => {
                    if (event.target.value) {
                      next[field.name] = event.target.value;
                    } else {
                      delete next[field.name];
                    }
                  })
                }
              >
                {!field.required && <option value="">Default</option>}
                {(field.targets ?? catalog.targets).map((target) => (
                  <option key={target} value={target}>
                    {formatCatalogValue(target)}
                  </option>
                ))}
              </select>
            </label>
          );
        }

        const currentValue = action[field.name];
        const value = typeof currentValue === "number" ? currentValue : "";
        const ratio = field.kind === "ratio";
        return (
          <label key={field.name}>
            {fieldLabel(field.name)}
            <input
              type="number"
              min={ratio ? 0.01 : field.kind === "integer" ? 1 : 0}
              max={ratio ? 1 : 200}
              step={ratio ? 0.05 : field.kind === "integer" ? 1 : 0.5}
              value={value}
              onChange={(event) =>
                update((next) => {
                  if (event.target.value === "") {
                    delete next[field.name];
                  } else {
                    next[field.name] = Number(event.target.value);
                  }
                })
              }
            />
          </label>
        );
      })}
      {!spec && <small>This action is not described by the current catalog.</small>}
    </div>
  );
}
