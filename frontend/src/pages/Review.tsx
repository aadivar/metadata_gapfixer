import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  autofix,
  autofixAll,
  buildXml,
  FieldScore,
  getCost,
  getFactsheet,
  getMetadata,
  getScore,
  putMetadata,
  reconcile,
  Scorecard,
  Tier,
  xmlDownloadUrl,
} from "../api";

const TIER_DESCRIPTION: Record<Tier, string> = {
  T0: "Crossref schema 5.4.0 — required to deposit at all.",
  T1: "Crossref recommended — what makes the record usable for indexers.",
  T2: "Crossref Participation / Nexus benchmarks — what enables cross-system linking.",
  T3: "Joint Crossref+DataCite integrity guide — what makes this record trustable.",
};

function scoreColor(score: number): string {
  if (score >= 80) return "var(--ok)";
  if (score >= 50) return "var(--warn)";
  return "var(--error)";
}

export default function Review() {
  const { id } = useParams();
  const subId = Number(id);

  const [card, setCard] = useState<Scorecard | null>(null);
  const [factsheet, setFactsheet] = useState<any>(null);
  const [cost, setCost] = useState<{ total_usd: number; calls: any[] }>({ total_usd: 0, calls: [] });
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [metaText, setMetaText] = useState<string>("");
  const [showMeta, setShowMeta] = useState(false);
  const [xmlBuilt, setXmlBuilt] = useState(false);

  async function refresh() {
    try {
      const [c, fs, co] = await Promise.all([
        getScore(subId),
        getFactsheet(subId).catch(() => null),
        getCost(subId).catch(() => ({ total_usd: 0, calls: [] })),
      ]);
      setCard(c);
      setFactsheet(fs);
      setCost(co);
    } catch (e) {
      setErr(String(e));
    }
  }

  useEffect(() => { refresh(); }, [subId]);

  async function loadMetadata() {
    try {
      const m = await getMetadata(subId);
      setMetaText(JSON.stringify(m, null, 2));
      setShowMeta(true);
    } catch (e) {
      setErr(String(e));
    }
  }

  function summarizeReport(r: any): string {
    if (!r) return "no report";
    if (r.error) return `failed: ${r.error}`;
    const bits: string[] = [];
    if (Array.isArray(r.changes)) bits.push(`changed: ${r.changes.join(", ") || "nothing"}`);
    if (typeof r.resolved === "number") bits.push(`resolved ${r.resolved}${r.out_of ? "/" + r.out_of : ""}`);
    if (typeof r.added_refs === "number") bits.push(`added ${r.added_refs} refs`);
    return bits.length ? bits.join(" · ") : (r.ok ? "ok (no changes)" : "no changes");
  }

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(null), 5000);
  }

  async function fixOne(action: string) {
    setBusy(`Running ${action}…`);
    setErr(null);
    try {
      const res = await autofix(subId, action);
      setCard(res.score);
      showToast(`${action} → ${summarizeReport(res.report)}`);
      const co = await getCost(subId).catch(() => null);
      if (co) setCost(co);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  async function fixAll() {
    setBusy("Running all auto-fixes…");
    setErr(null);
    try {
      const res = await autofixAll(subId);
      setCard(res.score);
      const summary = (res.reports || [])
        .map((r: any) => `${r.action}: ${summarizeReport(r)}`)
        .join("  |  ");
      showToast(summary || "auto-fix complete");
      const co = await getCost(subId).catch(() => null);
      if (co) setCost(co);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  async function premiumReconcile() {
    setBusy("Reconciling with LLM (premium)…");
    setErr(null);
    try {
      await reconcile(subId);
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  async function generateXml() {
    setBusy("Generating Crossref XML…");
    try {
      await buildXml(subId);
      setXmlBuilt(true);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  async function saveMetaEdits() {
    setBusy("Saving…");
    try {
      await putMetadata(subId, JSON.parse(metaText));
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  if (!card) {
    return (
      <section>
        <div className="page-header">
          <div className="crumbs"><Link to="/upload">Submissions</Link> / #{subId}</div>
          <h1>Review</h1>
        </div>
        <div className="card"><p className="muted loading">Loading scorecard</p></div>
        {err && <div className="card"><p className="error">{err}</p></div>}
      </section>
    );
  }

  return (
    <section>
      <div className="page-header">
        <div className="crumbs">
          <Link to="/upload">Submissions</Link> / #{subId}
        </div>
        <div className="row">
          <h1>Metadata gap report</h1>
          <span className="muted small mono">cost so far · ${cost.total_usd.toFixed(4)}</span>
        </div>
      </div>

      {/* ── HERO SCORECARD ──────────────────────────────────────────── */}
      <div className="card scorecard-hero">
        <div className="hero-grid">
          <div className="hero-score" style={{ borderColor: scoreColor(card.composite) }}>
            <div className="hero-num" style={{ color: scoreColor(card.composite) }}>{card.composite}</div>
            <div className="hero-den">/ 100</div>
          </div>
          <div className="hero-text">
            <h2 className="card-title" style={{ marginBottom: 8 }}>{card.interpretation}</h2>
            <div className="tier-bars">
              {card.tiers.map((t) => (
                <div key={t.tier} className="tier-row" title={TIER_DESCRIPTION[t.tier]}>
                  <span className="tier-name">
                    <strong>{t.tier}</strong> {t.label}
                  </span>
                  <div className="tier-track">
                    <div className="tier-fill" style={{ width: `${t.score}%`, background: scoreColor(t.score) }} />
                  </div>
                  <span className="tier-stat mono">
                    {t.score}% <span className="muted">· {t.fields_present}/{t.fields_total}</span>
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="actions">
          <button className="primary" onClick={fixAll} disabled={busy !== null || card.high_impact.length === 0}>
            ⚡ Auto-fix everything ({card.high_impact.length} high-impact gaps)
          </button>
          <button onClick={generateXml} disabled={busy !== null || card.tiers[0].score < 80}>
            Generate Crossref XML
          </button>
          {xmlBuilt && (
            <a className="btn" href={xmlDownloadUrl(subId)} target="_blank" rel="noreferrer">
              Download XML ↓
            </a>
          )}
          <span className="spacer" />
          <Link className="btn ghost" to={`/inspect/${subId}`}>Inspect document →</Link>
        </div>
        {busy && <p className="muted loading" style={{ marginTop: 12 }}>{busy}</p>}
        {err && <p className="error" style={{ marginTop: 12 }}>{err}</p>}
        {toast && <p className="toast">{toast}</p>}
      </div>

      {/* ── GAP BUCKETS ────────────────────────────────────────────── */}
      <div className="gaps-grid">
        <GapBucket
          title="High impact"
          subtitle="Auto-fixable from facts or free APIs"
          tone="ok"
          fields={card.high_impact}
          onFix={fixOne}
          busy={busy !== null}
        />
        <GapBucket
          title="Medium impact"
          subtitle="We have a candidate; you confirm"
          tone="warn"
          fields={card.medium}
          onFix={fixOne}
          busy={busy !== null}
        />
        <GapBucket
          title="Needs you"
          subtitle="Publisher policy fields — not extractable"
          tone="info"
          fields={card.manual}
          onFix={fixOne}
          busy={busy !== null}
          noFix
        />
      </div>

      {/* ── WHAT WE FOUND ──────────────────────────────────────────── */}
      <div className="card">
        <h2 className="card-title">What we found</h2>
        <p className="card-subtitle">
          Pulled deterministically from the PDF — no LLM cost. The reconciler trusts these and won't overwrite them.
        </p>
        <div className="found-grid">
          {Object.entries(card.facts_summary).map(([k, v]) => (
            <div key={k} className="found-row">
              <span className="found-key mono">{k}</span>
              <span className="found-val">
                {v === null || v === undefined ? <span className="muted">—</span>
                  : typeof v === "boolean" ? (v ? "✓ yes" : "no")
                  : String(v)}
              </span>
            </div>
          ))}
        </div>
      </div>

      {/* ── METADATA EDITOR (collapsible) ──────────────────────────── */}
      <div className="card">
        <div className="row">
          <h2 className="card-title" style={{ margin: 0 }}>Metadata JSON</h2>
          <div className="cluster">
            <button className="ghost" onClick={loadMetadata}>Load current</button>
            <button onClick={premiumReconcile} disabled={busy !== null}>
              Reconcile with LLM agent (premium)
            </button>
          </div>
        </div>
        <p className="card-subtitle">
          Optional. The auto-fixers above already build this for you. The LLM agent is for harder
          judgement calls (ambiguous ROR matches, reference disambiguation) — every call is logged
          to the cost ledger.
        </p>
        {showMeta && (
          <>
            <textarea
              className="json-editor"
              value={metaText}
              onChange={(e) => setMetaText(e.target.value)}
              spellCheck={false}
            />
            <div className="actions">
              <button onClick={saveMetaEdits} disabled={busy !== null}>Save edits</button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}

// ============================================================================

function GapBucket({
  title, subtitle, tone, fields, onFix, busy, noFix,
}: {
  title: string;
  subtitle: string;
  tone: "ok" | "warn" | "info";
  fields: FieldScore[];
  onFix: (action: string) => void;
  busy: boolean;
  noFix?: boolean;
}) {
  return (
    <div className={`card gap-bucket gap-${tone}`}>
      <div className="row" style={{ marginBottom: 6 }}>
        <h3 style={{ textTransform: "none", letterSpacing: 0, color: "var(--fg-primary)", fontSize: 14 }}>
          {title}
        </h3>
        <span className="status" data-tone={tone}>{fields.length}</span>
      </div>
      <p className="muted small" style={{ marginBottom: 12 }}>{subtitle}</p>
      {fields.length === 0 ? (
        <p className="muted small">Nothing in this bucket — nice.</p>
      ) : (
        <ul className="gap-list">
          {fields.map((f) => (
            <li key={f.key} className="gap-item">
              <div className="gap-info">
                <div className="gap-label">
                  {f.label}
                  <span className="status" style={{ marginLeft: 8 }}>{f.tier}</span>
                </div>
                <div className="muted small gap-why">{f.why}</div>
                {f.value_preview && (
                  <div className="muted small mono" style={{ marginTop: 2 }}>{f.value_preview}</div>
                )}
              </div>
              {!noFix && f.autofix_action && (
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() => onFix(f.autofix_action!)}
                  title={`action: ${f.autofix_action}`}
                >
                  Fix
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
