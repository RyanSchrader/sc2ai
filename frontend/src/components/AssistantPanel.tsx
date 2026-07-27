import { useMemo, useState } from "react";
import { api, jsonOptions } from "../api";
import {
  type BotRecord,
  type ProposalRecord,
  type Race,
  type StrategyDocument,
} from "../models";

interface Props {
  mode: "create" | "modify";
  bot?: BotRecord;
  strategy?: StrategyDocument;
  onApplied: (bot: BotRecord) => void;
  onProposalPendingChange?: (pending: boolean) => void;
}

export default function AssistantPanel({
  mode,
  bot,
  strategy,
  onApplied,
  onProposalPendingChange,
}: Props) {
  const [prompt, setPrompt] = useState("");
  const [requestedName, setRequestedName] = useState("");
  const [race, setRace] = useState<Race>(strategy?.race ?? "protoss");
  const [proposal, setProposal] = useState<ProposalRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const currentRuleCount = useMemo(
    () => strategy?.phases.reduce((total, phase) => total + phase.rules.length, 0) ?? 0,
    [strategy],
  );

  const requestProposal = async () => {
    if (!prompt.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api<ProposalRecord>(
        "/assistant/proposals",
        jsonOptions("POST", {
          prompt,
          base_bot_id: mode === "modify" ? bot?.id : null,
          strategy: strategy ?? null,
          requested_name: requestedName || bot?.name || null,
          requested_race: mode === "create" ? race : null,
        }),
      );
      setProposal(result);
      onProposalPendingChange?.(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not generate proposal.");
    } finally {
      setBusy(false);
    }
  };

  const apply = async () => {
    if (!proposal) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api<BotRecord>(
        `/assistant/proposals/${proposal.id}/apply`,
        jsonOptions("POST", {
          expected_revision: mode === "modify" ? bot?.currentRevision : null,
        }),
      );
      setProposal(null);
      setPrompt("");
      onProposalPendingChange?.(false);
      onApplied(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not apply proposal.");
    } finally {
      setBusy(false);
    }
  };

  const reject = async () => {
    if (!proposal) return;
    try {
      await api(`/assistant/proposals/${proposal.id}/reject`, jsonOptions("POST"));
    } finally {
      setProposal(null);
      onProposalPendingChange?.(false);
    }
  };

  if (proposal) {
    const candidateRules = proposal.proposal.strategy.phases.reduce(
      (total, phase) => total + phase.rules.length,
      0,
    );
    return (
      <section className="proposal">
        <div className="proposal-heading">
          <span className="spark">✦</span>
          <div>
            <span className="eyebrow">PROPOSAL READY</span>
            <h3>{proposal.proposal.summary}</h3>
          </div>
        </div>
        <div className="proposal-stats">
          <div>
            <strong>{proposal.proposal.strategy.phases.length}</strong>
            <span>phases</span>
          </div>
          <div>
            <strong>{candidateRules}</strong>
            <span>rules {mode === "modify" && `(was ${currentRuleCount})`}</span>
          </div>
          <div>
            <strong>{proposal.proposal.strategy.race}</strong>
            <span>race</span>
          </div>
        </div>
        {proposal.proposal.assumptions.length > 0 && (
          <div className="proposal-notes">
            <strong>Assumptions</strong>
            <ul>
              {proposal.proposal.assumptions.map((assumption) => (
                <li key={assumption}>{assumption}</li>
              ))}
            </ul>
          </div>
        )}
        {proposal.proposal.warnings.length > 0 && (
          <div className="proposal-notes warning">
            <strong>Warnings</strong>
            <ul>
              {proposal.proposal.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </div>
        )}
        <details className="json-preview">
          <summary>Inspect candidate strategy JSON</summary>
          <pre>{JSON.stringify(proposal.proposal.strategy, null, 2)}</pre>
        </details>
        {error && <div className="alert error">{error}</div>}
        <div className="modal-actions">
          <button className="button secondary" onClick={() => void reject()} disabled={busy}>
            Reject
          </button>
          <button className="button primary" onClick={() => void apply()} disabled={busy}>
            {busy ? "Applying…" : "Apply validated proposal"}
          </button>
        </div>
      </section>
    );
  }

  return (
    <section className="assistant-composer">
      {mode === "create" && (
        <div className="form-grid">
          <label>
            Working name
            <input
              value={requestedName}
              onChange={(event) => setRequestedName(event.target.value)}
              placeholder="e.g. Two-base Blink Pressure"
            />
          </label>
          <label>
            Race
            <select value={race} onChange={(event) => setRace(event.target.value as Race)}>
              <option value="protoss">Protoss</option>
              <option value="terran">Terran</option>
              <option value="zerg">Zerg</option>
            </select>
          </label>
        </div>
      )}
      <label>
        {mode === "create" ? "Strategy description" : "Describe the change"}
        <textarea
          rows={mode === "create" ? 8 : 5}
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          placeholder={
            mode === "create"
              ? "Build 4 Gateways on one base, keep making Zealots, and attack when 10 are ready…"
              : "Delay the second base until 30 workers and attack with 16 units instead of 12…"
          }
        />
      </label>
      <p className="assistant-footnote">
        Ollama generates a structured draft locally. The server validates every unit, structure,
        trigger, and action before showing it here.
      </p>
      {error && (
        <div className="alert error">
          {error}
          {error.includes("Ollama") && (
            <code className="setup-command">ollama pull qwen3:8b</code>
          )}
        </div>
      )}
      <div className="assistant-submit">
        <button
          className="button primary"
          disabled={busy || !prompt.trim()}
          onClick={() => void requestProposal()}
        >
          {busy ? "Thinking locally…" : "✦ Generate preview"}
        </button>
      </div>
    </section>
  );
}
