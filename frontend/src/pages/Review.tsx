import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  autofix,
  autofixAll,
  buildXml,
  confirmField,
  Dimension,
  DimensionScore,
  enrichAll,
  FieldScore,
  getCost,
  getFactsheet,
  getMetadata,
  getPages,
  getPageBoxes,
  getScore,
  LayoutBox,
  LocateSelection,
  locateField,
  pageImageUrl,
  PageInfo,
  putMetadata,
  rejectField,
  runStructurer,
  Scorecard,
  Tier,
  xmlDownloadUrl,
} from "../api";
import {
  ArrowLeftIcon,
  ArrowRightIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CircleIcon,
  HelpCircleIcon,
  PencilIcon,
  ScaleIcon,
  XIcon,
} from "../icons";

import type { ComponentType, SVGProps } from "react";
type IconType = ComponentType<SVGProps<SVGSVGElement> & { size?: number }>;

const TIER_DESCRIPTION: Record<Tier, string> = {
  T0: "Crossref mandatory — DOI, title, author, date, target URL.",
  T1: "Rich bibliographic metadata.",
  T2: "Research Nexus connections.",
  T3: "Integrity signals.",
};

// Order of the buckets in the page: Mandatory first (deposit gate), then
// the five Research Nexus dimensions in nexus-score's order.
const DIMENSION_ORDER: Dimension[] = [
  "mandatory", "provenance", "people", "funding", "access", "organizations",
];

// Map the backend's entity-count pillars onto the dimension that owns them,
// so each dimension section can show its own underlying progress (e.g.
// People shows "7/9 authors with ORCID" inside its header).
const PILLAR_TO_DIM: Record<string, Dimension> = {
  researchers:   "people",
  funders:       "funding",
  organizations: "organizations",
  outputs:       "provenance",
};


const TIER_ORDER: Tier[] = ["T0", "T1", "T2", "T3"];

const HIGH_CONF = 0.9;

function scoreColor(score: number): string {
  if (score >= 80) return "var(--ok)";
  if (score >= 50) return "var(--warn)";
  return "var(--error)";
}

type CardState =
  | "confirmed"           // present + (confirmed flag OR confidence ≥ 0.9)
  | "pending"             // present, mid confidence, awaiting confirmation
  | "needs_pick"          // missing because multiple candidates exist
  | "needs_locate"        // missing, editor previously rejected
  | "missing"             // no extraction yet — show autofix
  | "manual";             // publisher policy — needs editor input

function deriveState(f: FieldScore): CardState {
  if (f.status === "present") {
    if (f.provenance_confirmed) return "confirmed";
    const conf = f.provenance_confidence ?? 1.0;
    if (conf >= HIGH_CONF) return "confirmed";
    return "pending";
  }
  if (f.provenance_source === "needs_pick") return "needs_pick";
  if (f.provenance_source === "needs_locate") return "needs_locate";
  if (f.bucket === "manual") return "manual";
  return "missing";
}

// Heuristic: which page is this field most likely on?
function expectedPageFor(fieldKey: string): number {
  if (["doi","title","journal_title","issn","publication_year","authors_any",
       "abstract","full_author_names","license_url","oa_indicator",
       "publication_date_full","volume_issue_pages"].includes(fieldKey)) {
    return 1;
  }
  return 1;
}

function sourcePillText(source?: string | null): string {
  if (!source) return "auto";
  return source.replace(/_/g, " ");
}

export default function Review() {
  const { id } = useParams();
  const subId = Number(id);

  const [card, setCard] = useState<Scorecard | null>(null);
  const [cost, setCost] = useState<{ total_usd: number; calls: any[] }>({ total_usd: 0, calls: [] });
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [metaText, setMetaText] = useState<string>("");
  const [showMeta, setShowMeta] = useState(false);
  const [xmlBuilt, setXmlBuilt] = useState(false);

  async function refresh() {
    try {
      const [c, co] = await Promise.all([
        getScore(subId),
        getCost(subId).catch(() => ({ total_usd: 0, calls: [] })),
      ]);
      setCard(c);
      setCost(co);
    } catch (e) {
      setErr(String(e));
    }
  }

  useEffect(() => { refresh(); }, [subId]);

  // Keep the metadata JSON view in sync with server state whenever the
  // scorecard updates (i.e. after reject / confirm / locate / autofix /
  // structurer). Without this the editor shows stale fields the user just
  // rejected. Any unsaved local edits get blown away — the editor is meant
  // for inspecting the canonical metadata, and clicking "Save" is the
  // explicit way to push manual edits.
  useEffect(() => {
    if (!showMeta || !card) return;
    (async () => {
      try {
        const m = await getMetadata(subId);
        setMetaText(JSON.stringify(m, null, 2));
      } catch (e) { setErr(String(e)); }
    })();
  }, [card, showMeta, subId]);

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(null), 4000);
  }

  function toggleExpand(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }

  async function fixOne(action: string) {
    setBusy(`Running ${action}…`);
    try {
      const res = await autofix(subId, action);
      setCard(res.score);
      const co = await getCost(subId).catch(() => null);
      if (co) setCost(co);
    } catch (e) { setErr(String(e)); }
    finally { setBusy(null); }
  }

  async function runStructure(field: FieldScore) {
    if (!field.structurer_task) return;
    const cost = field.ai_cost_estimate ?? 0;
    const ok = window.confirm(
      `Run AI structurer "${field.structurer_task}" for "${field.label}"?\n\n` +
      `This is a paid LLM call. Estimated cost: ~$${cost.toFixed(4)}.`
    );
    if (!ok) return;
    setBusy(`Running ${field.structurer_task}…`);
    try {
      const res = await runStructurer(subId, field.structurer_task);
      if (res?.score) setCard(res.score);
      const co = await getCost(subId).catch(() => null);
      if (co) setCost(co);
      const r = (res && res.report) || {};
      if (r.ok === false) {
        setErr(`${field.structurer_task} returned: ${r.error || "no result"}`);
        return;
      }
      const summary = Object.entries(r)
        .filter(([k, v]) => k !== "ok" && k !== "task" && typeof v !== "object")
        .map(([k, v]) => `${k}=${v}`)
        .join(" · ");
      showToast(`${field.structurer_task} complete · ${summary || "see metadata"}`);
    } catch (e) { setErr(String(e)); }
    finally { setBusy(null); }
  }

  async function fixAll() {
    setBusy("Running all auto-fixes…");
    try {
      const res = await autofixAll(subId);
      setCard(res.score);
      const co = await getCost(subId).catch(() => null);
      if (co) setCost(co);
      showToast(`${res.reports.length} auto-fixers ran (free)`);
    } catch (e) { setErr(String(e)); }
    finally { setBusy(null); }
  }

  async function premiumEnrichAll() {
    if (!card?.estimated_full_enrichment_usd) return;
    const ok = confirm(
      `This will run all premium AI enrichers (verify_authors, structure_references, ` +
      `structure_funding, structure_credit). Estimated cost: $${card.estimated_full_enrichment_usd.toFixed(4)}. Proceed?`
    );
    if (!ok) return;
    setBusy("Running premium enrichers…");
    try {
      const res = await enrichAll(subId);
      setCard(res.score);
      const co = await getCost(subId).catch(() => null);
      if (co) setCost(co);
      showToast(`Premium enrichment complete: ${res.reports.length} tasks ran`);
    } catch (e) { setErr(String(e)); }
    finally { setBusy(null); }
  }

  async function handleConfirm(field: FieldScore) {
    const path = (field.metadata_paths || [])[0] || field.key;
    setBusy(`Confirming ${field.label}…`);
    try {
      const res = await confirmField(subId, path);
      setCard(res.score);
    } catch (e) { setErr(String(e)); }
    finally { setBusy(null); }
  }

  async function reject(field: FieldScore) {
    const path = (field.metadata_paths || [])[0] || field.key;
    setBusy(`Marking ${field.label} as wrong…`);
    try {
      const res = await rejectField(subId, path);
      setCard(res.score);
      // Auto-expand so the locate panel is visible
      setExpanded((prev) => new Set(prev).add(field.key));
    } catch (e) { setErr(String(e)); }
    finally { setBusy(null); }
  }

  async function locate(field: FieldScore, page: number, boxIds: number[], joinedText?: string, selections?: LocateSelection[]) {
    const text = (joinedText ?? "").trim();
    // For AI-leverage fields with a structurer task, the located text is fed
    // straight to the LLM (so e.g. CRediT prose without an explicit heading
    // can still be processed). Otherwise we use the regex-based locate path.
    if (field.structurer_task && field.llm_leverage === "ai") {
      if (!text) {
        setErr("No text in the selected boxes — pick the paragraph that contains the contributions.");
        throw new Error("empty selection");
      }
      const cost = field.ai_cost_estimate ?? 0;
      const ok = window.confirm(
        `Send the selected text (${text.length} chars) to the AI structurer ` +
        `"${field.structurer_task}" for "${field.label}"?\n\n` +
        `Estimated cost: ~$${cost.toFixed(4)}.`
      );
      if (!ok) throw new Error("cancelled");
      setBusy(`Running ${field.structurer_task} on selection…`);
      try {
        const res = await runStructurer(subId, field.structurer_task!, { text_override: text });
        if (res?.score) setCard(res.score);
        const co = await getCost(subId).catch(() => null);
        if (co) setCost(co);
        const r = (res && res.report) || {};
        if (r.ok) {
          const summary = Object.entries(r)
            .filter(([k, v]) => k !== "ok" && k !== "task" && typeof v !== "object")
            .map(([k, v]) => `${k}=${v}`)
            .join(" · ");
          showToast(`${field.structurer_task} from selection · ${summary}`);
        } else {
          setErr(`${field.structurer_task} failed: ${r.error || "see report"}`);
        }
      } catch (e) { setErr(String(e)); throw e; }
      finally { setBusy(null); }
      return;
    }

    const path = (field.metadata_paths || [])[0] || field.key;
    setBusy(`Saving location for ${field.label}…`);
    try {
      const res = await locateField(subId, path, page, boxIds, selections);
      setCard(res.score);
      const pageLabel = selections && selections.length > 1
        ? `pp.${selections.map((s) => s.page).join(", ")}`
        : `page ${page}`;
      showToast(`${field.label} set from ${pageLabel} selection`);
    } catch (e) { setErr(String(e)); throw e; }
    finally { setBusy(null); }
  }

  async function loadMetadata() {
    try {
      const m = await getMetadata(subId);
      setMetaText(JSON.stringify(m, null, 2));
      setShowMeta(true);
    } catch (e) { setErr(String(e)); }
  }

  async function generateXml() {
    setBusy("Generating Crossref XML…");
    try {
      await buildXml(subId);
      setXmlBuilt(true);
    } catch (e) { setErr(String(e)); }
    finally { setBusy(null); }
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

  // Group fields by Research Nexus dimension. We no longer filter out the
  // author/affiliation-related fields — they render as their own FieldCards
  // inside the People & Organizations dimensions, with an inline
  // AuthorsListView in the expanded panel so editors can review the
  // extracted authors and their ORCIDs/RORs without hunting for the
  // supercard. The supercard above stays as the combined-locate workflow.
  const fieldsByDim: Record<Dimension, FieldScore[]> = {
    mandatory: [], provenance: [], people: [], funding: [], access: [], organizations: [],
  };
  card.fields.forEach((f) => {
    const d = (f.dimension ?? "access") as Dimension;
    fieldsByDim[d].push(f);
  });
  const dimensionByKey: Record<string, DimensionScore> = {};
  (card.dimensions || []).forEach((d) => { dimensionByKey[d.key] = d; });

  return (
    <section>
      <div className="page-header">
        <div className="crumbs"><Link to="/upload">Submissions</Link> / #{subId}</div>
        <div className="row">
          <h1>Metadata Generator</h1>
          <span className="muted small mono">cost so far · ${cost.total_usd.toFixed(4)}</span>
        </div>
      </div>

      {/* HERO — Research Nexus score + Mandatory gate */}
      <div className="card scorecard-hero">
        <div className="hero-grid">
          <div className="hero-score" style={{ borderColor: scoreColor(card.research_nexus_score ?? card.composite) }}>
            <div className="hero-num" style={{ color: scoreColor(card.research_nexus_score ?? card.composite) }}>
              {card.research_nexus_score ?? card.composite}
            </div>
            <div className="hero-den">RESEARCH NEXUS</div>
          </div>
          <div className="hero-text">
            <h2 className="card-title" style={{ marginBottom: 8 }}>{card.interpretation}</h2>
            <div className={`mandatory-banner ${card.mandatory_ready ? "is-ready" : "is-blocked"}`}>
              <span className="mandatory-dot" />
              <strong>Mandatory:</strong>
              <span className="muted small mono">{card.mandatory_present ?? 0}/{card.mandatory_total ?? 0} fields</span>
              <span className="mandatory-status">
                {card.mandatory_ready ? "Depositable" : "Not yet depositable"}
              </span>
            </div>
            <div className="tier-bars">
              {(card.dimensions || [])
                .filter((d) => d.weight > 0)   // skip Mandatory bar (gate, not dimension)
                .map((d) => (
                  <div key={d.key} className="tier-row" title={d.description}>
                    <span className="tier-name">
                      <strong>{d.label}</strong>
                      <span className="muted small mono"> · {d.weight}% wt</span>
                    </span>
                    <div className="tier-track">
                      <div className="tier-fill" style={{ width: `${d.score}%`, background: scoreColor(d.score) }} />
                    </div>
                    <span className="tier-stat mono">
                      {d.score}% <span className="muted">· {d.fields_present}/{d.fields_total}</span>
                    </span>
                  </div>
                ))}
            </div>
          </div>
        </div>
        <div className="actions">
          <button className="primary" onClick={fixAll} disabled={busy !== null}>
            Run automated extraction
          </button>
          {(card.estimated_full_enrichment_usd ?? 0) > 0 && (
            <button onClick={premiumEnrichAll} disabled={busy !== null} title="Run all AI enrichers in sequence">
              Run AI enrichment<span className="btn-meta">~${(card.estimated_full_enrichment_usd ?? 0).toFixed(4)}</span>
            </button>
          )}
          <button onClick={generateXml} disabled={busy !== null || !card.mandatory_ready}>
            Generate Crossref XML
          </button>
          {xmlBuilt && <a className="btn" href={xmlDownloadUrl(subId)} target="_blank" rel="noreferrer">Download XML</a>}
        </div>
        {busy && <p className="muted loading" style={{ marginTop: 12 }}>{busy}</p>}
        {err && <p className="error" style={{ marginTop: 12 }}>{err}</p>}
        {toast && <p className="toast">{toast}</p>}
      </div>

      {/* DIMENSION NAV — sticky strip linking to each bucket */}
      <nav className="tier-nav" aria-label="Jump to dimension">
        {DIMENSION_ORDER.map((dim) => {
          const d = dimensionByKey[dim];
          if (!d || fieldsByDim[dim].length === 0) return null;
          return (
            <a key={dim} href={`#dim-${dim}`} className="tier-nav-item">
              <span className="tier-nav-code">{dim === "mandatory" ? "GATE" : `${d.weight}%`}</span>
              <span className="tier-nav-label">{d.label}</span>
              <span className="tier-nav-score mono" style={{ color: scoreColor(d.score) }}>
                {d.score}%
              </span>
            </a>
          );
        })}
      </nav>

      {/* PER-DIMENSION FIELD CARDS */}
      {DIMENSION_ORDER.map((dim) => {
        const fields = fieldsByDim[dim];
        const d = dimensionByKey[dim];
        if (!d || fields.length === 0) return null;
        const isMandatory = dim === "mandatory";
        const todo = fields.filter((f) => deriveState(f) !== "confirmed");
        const done = fields.filter((f) => deriveState(f) === "confirmed");

        // Find the entity-count pillar that this dimension owns (if any)
        const pillar = (card.research_nexus?.pillars || []).find(
          (p) => PILLAR_TO_DIM[p.key] === dim
        );
        const pillarPct = pillar && pillar.denominator > 0
          ? Math.round((100 * pillar.numerator) / pillar.denominator)
          : 0;

        const renderField = (f: FieldScore) => (
          <FieldCard
            key={f.key}
            subId={subId}
            field={f}
            expanded={expanded.has(f.key)}
            onToggleExpand={() => toggleExpand(f.key)}
            onConfirm={() => handleConfirm(f)}
            onReject={() => reject(f)}
            onAutofix={f.autofix_action ? () => fixOne(f.autofix_action!) : undefined}
            onRunStructurer={f.structurer_task ? () => runStructure(f) : undefined}
            onLocate={(page, boxIds, joinedText, selections) => locate(f, page, boxIds, joinedText, selections)}
            busy={busy !== null}
          />
        );

        return (
          <section key={dim} id={`dim-${dim}`} className={`tier-section dim-section dim-${dim} ${isMandatory ? "dim-mandatory" : ""}`}>
            <header className="tier-header">
              <div className="tier-header-left">
                <span className={`tier-code ${isMandatory ? "tier-code-gate" : ""}`}>
                  {isMandatory ? "GATE" : `${d.weight}%`}
                </span>
                <div>
                  <h2 className="tier-title">{d.label}</h2>
                  <p className="tier-desc muted small">{d.description}</p>
                </div>
              </div>
              <div className="tier-header-right">
                <div className="tier-score-block">
                  <span className="tier-score-num mono" style={{ color: scoreColor(d.score) }}>
                    {d.score}<span className="tier-score-pct">%</span>
                  </span>
                  <span className="muted small mono">
                    {d.fields_present}/{d.fields_total} fields
                  </span>
                </div>
                <div className="tier-track-lg">
                  <div className="tier-fill-lg" style={{ width: `${d.score}%`, background: scoreColor(d.score) }} />
                </div>
              </div>
            </header>

            {pillar && (
              <div className={`dim-pillar nexus-${pillar.status}`} title={pillar.caption}>
                <span className="nexus-dot" />
                <div className="dim-pillar-body">
                  <div className="dim-pillar-row-top">
                    <span className="dim-pillar-caption">{pillar.caption}</span>
                    <span className="dim-pillar-frac mono small">
                      {pillar.denominator > 0 ? `${pillar.numerator}/${pillar.denominator}` : "—"}
                    </span>
                  </div>
                  <div className="nexus-pillar-track">
                    <div className="nexus-pillar-fill" style={{ width: `${pillarPct}%` }} />
                  </div>
                </div>
              </div>
            )}

            {todo.length > 0 && (
              <div className="tier-bucket">
                <h4 className="tier-bucket-title">Needs attention <span className="muted small mono">{todo.length}</span></h4>
                <div className="field-cards">{todo.map(renderField)}</div>
              </div>
            )}
            {done.length > 0 && (
              <div className="tier-bucket">
                <h4 className="tier-bucket-title">Confirmed <span className="muted small mono">{done.length}</span></h4>
                <div className="field-cards">{done.map(renderField)}</div>
              </div>
            )}
          </section>
        );
      })}

      {/* METADATA EDITOR (collapsed) */}
      <div className="card">
        <div className="row">
          <h2 className="card-title" style={{ margin: 0 }}>Metadata JSON (advanced)</h2>
          <button className="ghost" onClick={loadMetadata}>Load current</button>
        </div>
        {showMeta && (
          <>
            <textarea className="json-editor" value={metaText} onChange={(e) => setMetaText(e.target.value)} spellCheck={false} />
            <div className="actions">
              <button onClick={async () => {
                setBusy("Saving…");
                try { await putMetadata(subId, JSON.parse(metaText)); await refresh(); }
                catch (e) { setErr(String(e)); }
                finally { setBusy(null); }
              }}>Save edits</button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}

// ============================================================================

function CreditContributionsView({ subId }: { subId: number }) {
  const [contribs, setContribs] = useState<any[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const m = await getMetadata(subId);
        setContribs(m.credit_contributions || []);
      } catch (e) {
        setErr(String(e));
      }
    })();
  }, [subId]);

  if (err) return <p className="error" style={{ marginTop: 8 }}>{err}</p>;
  if (contribs === null) return <p className="muted small loading">Loading contributions</p>;
  if (contribs.length === 0) {
    return <p className="muted small" style={{ marginTop: 8 }}>No contributions on metadata yet.</p>;
  }

  const totalRoles = contribs.reduce((acc: number, c: any) => acc + (c.roles?.length ?? 0), 0);

  return (
    <div className="credit-contribs" style={{ marginTop: 10 }}>
      <h5 style={{ marginBottom: 6 }}>
        {contribs.length} contributors · {totalRoles} CRediT roles
      </h5>
      <ul className="credit-author-rows">
        {contribs.map((c: any, i: number) => (
          <li key={i} className="credit-author-row">
            <div className="credit-author-head">
              <span className="credit-author-name">{c.author_name || "(unnamed)"}</span>
              {c.author_initials && <span className="chip credit-initials">{c.author_initials}</span>}
            </div>
            {(c.roles && c.roles.length > 0) ? (
              <ul className="credit-role-rows">
                {c.roles.map((r: any, j: number) => (
                  <li key={j} className="credit-role-row">
                    <span className="chip chip-credit">{r.role}</span>
                    {r.evidence && (
                      <span className="muted small credit-evidence">"{r.evidence}"</span>
                    )}
                    {typeof r.confidence === "number" && (
                      <span className="muted small mono">{Math.round(r.confidence * 100)}%</span>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted small" style={{ marginLeft: 16 }}>(no roles assigned)</p>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

type AuthorsListMode = "full_names" | "orcid_corresponding" | "orcid_all" | "affiliations" | "ror";
function AuthorsListView({ subId, mode }: { subId: number; mode: AuthorsListMode }) {
  const [authors, setAuthors] = useState<any[] | null>(null);
  const [provenance, setProvenance] = useState<Record<string, any>>({});
  const [fromFactsheet, setFromFactsheet] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const m = await getMetadata(subId);
        const metaAuthors = (m.authors || []) as any[];
        setProvenance(m.provenance || {});
        // The score endpoint falls back to the factsheet's parsed authors when
        // meta has none, so the card preview reads "20 authors" before
        // auto-fix has copied them into meta. Mirror that here so the
        // expanded view shows the names instead of "No authors yet."
        if (metaAuthors.length === 0) {
          try {
            const fs = await getFactsheet(subId);
            const fsAuthors = (fs.authors || []) as any[];
            setAuthors(fsAuthors.map((a: any) => ({
              full_name: a.name,
              given_name: a.given,
              surname: a.surname,
              orcid: a.orcid,
              email: a.email,
              is_corresponding: a.is_corresponding,
              affiliations: a.markers ? a.markers
                .map((mk: string) => (fs.affiliations || {})[mk])
                .filter(Boolean) : [],
              ror_ids: [],
            })));
            setFromFactsheet(true);
          } catch {
            setAuthors([]);
          }
        } else {
          setAuthors(metaAuthors);
          setFromFactsheet(false);
        }
      } catch (e) { setErr(String(e)); }
    })();
  }, [subId]);

  if (err) return <p className="error" style={{ marginTop: 8 }}>{err}</p>;
  if (authors === null) return <p className="muted small loading">Loading authors</p>;
  if (authors.length === 0) {
    return <p className="muted small" style={{ marginTop: 8 }}>No authors on metadata yet.</p>;
  }

  const visible = mode === "orcid_corresponding"
    ? authors.filter((a: any) => a.is_corresponding)
    : authors;
  if (visible.length === 0) {
    return <p className="muted small" style={{ marginTop: 8 }}>No corresponding author marked.</p>;
  }

  return (
    <>
      {fromFactsheet && (
        <p className="muted small" style={{ marginTop: 8 }}>
          From factsheet (regex-parsed). Run "Run automated extraction" to copy into metadata.
        </p>
      )}
    <ol className="author-rows" style={{ marginTop: 8 }}>
      {visible.map((a: any, i: number) => {
        const idx = authors.indexOf(a);
        const prov = provenance[`authors[${idx}]`] || {};
        const evidence: string[] = prov.evidence_chain || [];
        const showOrcid = mode === "orcid_corresponding" || mode === "orcid_all" || mode === "full_names";
        const showAffils = mode === "affiliations" || mode === "ror" || mode === "full_names";
        return (
          <li key={i} className="author-row">
            <div className="author-main">
              <span className="author-name">
                {a.full_name || `${a.given_name || ""} ${a.surname || ""}`.trim() || "(unnamed)"}
              </span>
              {a.is_corresponding && <span className="chip chip-warn">corresponding</span>}
              {showOrcid && (a.orcid ? (
                <a className="chip chip-orcid" href={`https://orcid.org/${a.orcid}`} target="_blank" rel="noreferrer">
                  ORCID {a.orcid}
                </a>
              ) : (
                <span className="chip chip-missing">no ORCID</span>
              ))}
              {a.email && <span className="muted small mono">{a.email}</span>}
            </div>
            {showAffils && ((a.affiliations && a.affiliations.length > 0) ? (
              <ul className="aff-rows">
                {a.affiliations.map((aff: string, j: number) => {
                  const ror = (a.ror_ids || [])[j];
                  return (
                    <li key={j} className="aff-row">
                      <span className="aff-text">{aff}</span>
                      {mode === "ror" && (ror ? (
                        <a className="chip chip-ror" href={ror} target="_blank" rel="noreferrer">
                          {ror.replace("https://ror.org/", "ROR ")}
                        </a>
                      ) : (
                        <span className="chip chip-missing">no ROR</span>
                      ))}
                      {mode === "affiliations" && ror && (
                        <a className="chip chip-ror" href={ror} target="_blank" rel="noreferrer">
                          {ror.replace("https://ror.org/", "ROR ")}
                        </a>
                      )}
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="muted small" style={{ marginLeft: 16 }}>no affiliations attached</p>
            ))}
            {evidence.length > 0 && (
              <details className="author-evidence">
                <summary className="muted small">why this match? · confidence {Math.round((prov.confidence ?? 0) * 100)}%</summary>
                <ul>{evidence.map((e, k) => <li key={k} className="small">{e}</li>)}</ul>
              </details>
            )}
          </li>
        );
      })}
    </ol>
    </>
  );
}

const AUTHOR_VIEW_FIELDS: Record<string, AuthorsListMode> = {
  authors_any:              "full_names",
  full_author_names:        "full_names",
  orcid_for_corresponding:  "orcid_corresponding",
  orcid_for_all_authors:    "orcid_all",
  affiliations_listed:      "affiliations",
  ror_for_all_affiliations: "ror",
};

const REFERENCES_VIEW_FIELDS = new Set(["references_any", "references_with_doi"]);

const REFS_PAGE_SIZE = 10;
function ReferencesView({ subId, mode }: { subId: number; mode: "all" | "with_doi" }) {
  const [refs, setRefs] = useState<any[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [page, setPage] = useState(0);

  useEffect(() => {
    (async () => {
      try {
        const m = await getMetadata(subId);
        setRefs(m.references || []);
      } catch (e) { setErr(String(e)); }
    })();
  }, [subId]);

  if (err) return <p className="error" style={{ marginTop: 8 }}>{err}</p>;
  if (refs === null) return <p className="muted small loading">Loading references</p>;
  if (refs.length === 0) {
    return <p className="muted small" style={{ marginTop: 8 }}>No references on metadata yet.</p>;
  }

  const visible = mode === "with_doi" ? refs.filter((r: any) => r && r.doi) : refs;
  if (visible.length === 0) {
    return <p className="muted small" style={{ marginTop: 8 }}>No references with a DOI yet.</p>;
  }
  const totalPages = Math.max(1, Math.ceil(visible.length / REFS_PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const start = safePage * REFS_PAGE_SIZE;
  const slice = visible.slice(start, start + REFS_PAGE_SIZE);
  const withDoi = refs.filter((r: any) => r && r.doi).length;

  return (
    <div className="references-view" style={{ marginTop: 10 }}>
      <h5 style={{ marginBottom: 6 }}>
        {mode === "with_doi"
          ? `${visible.length} references with DOI`
          : `${refs.length} references · ${withDoi} with DOI`}
      </h5>
      <ol className="references-list" start={start + 1} style={{ paddingLeft: 24 }}>
        {slice.map((r: any, i: number) => (
          <li key={start + i} className="reference-row" style={{ marginBottom: 6 }}>
            <div className="reference-raw small">
              {(r.raw || "").trim() || <em className="muted">(empty)</em>}
            </div>
            <div className="reference-meta muted small" style={{ marginTop: 2 }}>
              {r.year && <span>{r.year}</span>}
              {r.doi ? (
                <>
                  {r.year && <span> · </span>}
                  <a href={`https://doi.org/${r.doi}`} target="_blank" rel="noreferrer" className="mono">
                    {r.doi}
                  </a>
                </>
              ) : (
                <>
                  {r.year && <span> · </span>}
                  <span className="chip chip-missing">no DOI</span>
                </>
              )}
              {r.title && <span> · {r.title}</span>}
            </div>
          </li>
        ))}
      </ol>
      {totalPages > 1 && (
        <div className="references-pager row" style={{ marginTop: 8, gap: 8, alignItems: "center" }}>
          <button className="ghost" onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={safePage === 0}>
            <ArrowLeftIcon size={13} /> Prev
          </button>
          <span className="muted small">
            Page {safePage + 1} of {totalPages} · showing {start + 1}–{Math.min(start + REFS_PAGE_SIZE, visible.length)} of {visible.length}
          </span>
          <button className="ghost" onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))} disabled={safePage >= totalPages - 1}>
            Next <ArrowRightIcon size={13} />
          </button>
        </div>
      )}
    </div>
  );
}

function FieldCard({
  subId, field, expanded, onToggleExpand, onConfirm, onReject, onAutofix, onRunStructurer, onLocate, busy,
}: {
  subId: number;
  field: FieldScore;
  expanded: boolean;
  onToggleExpand: () => void;
  onConfirm: () => void;
  onReject: () => void;
  onAutofix?: () => void;
  onRunStructurer?: () => void;
  onLocate: (page: number, boxIds: number[], joinedText: string, selections: LocateSelection[]) => Promise<void>;
  busy: boolean;
}) {
  const state = deriveState(field);
  const stateInfo = STATE_INFO[state];
  const [locateOpen, setLocateOpen] = useState(false);

  const canLocate = state === "missing" || state === "needs_locate" || state === "pending" || state === "confirmed";

  // Auto-open locate panel for needs_locate state
  useEffect(() => {
    if (state === "needs_locate" && expanded) setLocateOpen(true);
  }, [state, expanded]);

  const StateIcon = stateInfo.Icon;
  return (
    <div className={`field-card field-state-${state}`}>
      <div className="field-row" onClick={onToggleExpand}>
        <span className="field-icon" style={{ color: stateInfo.color }} title={stateInfo.label}>
          <StateIcon size={14} />
        </span>
        <span className="field-label">
          {field.label}
          {field.llm_leverage && <LeverageBadge leverage={field.llm_leverage} />}
        </span>
        <span className="field-value-preview muted">
          {field.value_preview ?? <em>—</em>}
        </span>
        {field.provenance_source && (
          <span className="field-source-pill" title={field.provenance_reasoning || ""}>
            {sourcePillText(field.provenance_source)}
            {field.provenance_confidence != null && ` · ${Math.round(field.provenance_confidence * 100)}%`}
          </span>
        )}
        <span className="field-expand">
          {expanded ? <ChevronDownIcon size={14} /> : <ChevronRightIcon size={14} />}
        </span>
      </div>

      {expanded && (
        <div className="field-detail">
          <p className="muted small">{field.why}</p>

          {field.key === "credit_roles" && (state === "confirmed" || state === "pending") && (
            <CreditContributionsView subId={subId} />
          )}

          {AUTHOR_VIEW_FIELDS[field.key] && (state === "confirmed" || state === "pending" || state === "missing") && (
            <AuthorsListView subId={subId} mode={AUTHOR_VIEW_FIELDS[field.key]} />
          )}

          {REFERENCES_VIEW_FIELDS.has(field.key) && (state === "confirmed" || state === "pending") && (
            <ReferencesView subId={subId} mode={field.key === "references_with_doi" ? "with_doi" : "all"} />
          )}

          {state === "confirmed" && (
            <div className="actions">
              <button className="ghost" onClick={onReject} disabled={busy}
                      title="Mark this field as wrong and re-identify on the document.">
                Reject and re-identify
              </button>
            </div>
          )}

          {state === "pending" && (
            <>
              {field.provenance_reasoning && <p className="small mono">{field.provenance_reasoning}</p>}
              <div className="actions">
                <button className="primary" onClick={onConfirm} disabled={busy}>Confirm</button>
                <button className="ghost" onClick={onReject} disabled={busy}>Reject and re-identify</button>
              </div>
            </>
          )}

          {state === "missing" && (
            <div className="actions">
              <button className="primary" onClick={() => setLocateOpen((v) => !v)} disabled={busy}
                      title={
                        field.llm_leverage === "ai" && field.structurer_task
                          ? `Pick the section on the page; we'll send the selected text to ${field.structurer_task} (~$${(field.ai_cost_estimate ?? 0).toFixed(4)}).`
                          : "Pick the box(es) on the page that contain this field's value."
                      }>
                Identify on document
                {field.llm_leverage === "ai" && field.structurer_task && (
                  <span className="btn-meta">AI · ~${(field.ai_cost_estimate ?? 0).toFixed(4)}</span>
                )}
              </button>
            </div>
          )}

          {state === "needs_locate" && (
            <p className="muted small">
              Pick the box(es) on the page that contain this field's value.
              {field.llm_leverage === "ai" && field.structurer_task && (
                <> The selection will be sent to the AI structurer (~${(field.ai_cost_estimate ?? 0).toFixed(4)}).</>
              )}
            </p>
          )}

          {state === "needs_pick" && (
            <p className="muted small">
              Multiple candidates returned. Pick UI lands in the next slice — for now, use
              <code> POST /submissions/{"{id}"}/pick </code> or <code> /disambiguate</code>.
            </p>
          )}

          {state === "manual" && (
            <p className="muted small">
              Publisher-policy field. Inline input form lands in the next slice. You can also
              click <strong>Identify on document</strong> if the value happens to be in the PDF.
            </p>
          )}

          {locateOpen && (
            <LocatePanel
              subId={subId}
              field={field}
              defaultPage={expectedPageFor(field.key)}
              onSubmit={async (page, boxIds, joinedText, selections) => {
                await onLocate(page, boxIds, joinedText, selections);
                setLocateOpen(false);
              }}
            />
          )}
        </div>
      )}
    </div>
  );
}

// ============================================================================
// LocatePanel — inline PDF-with-bboxes for the editor to point at
// ============================================================================

function LocatePanel({
  subId, field, defaultPage, onSubmit,
}: {
  subId: number;
  field: FieldScore;
  defaultPage: number;
  onSubmit: (page: number, boxIds: number[], joinedText: string, selections: LocateSelection[]) => Promise<void>;
}) {
  const [pages, setPages] = useState<PageInfo[]>([]);
  const [pageNo, setPageNo] = useState(defaultPage);
  // Per-page cache so selections survive page navigation and joined text
  // can be assembled from boxes on pages the editor isn't currently viewing
  // (e.g. references that span pp.45-49).
  const [boxesByPage, setBoxesByPage] = useState<Map<number, LayoutBox[]>>(new Map());
  const [selectedByPage, setSelectedByPage] = useState<Map<number, Set<number>>>(new Map());
  const [err, setErr] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const dragStartRef = useRef<{ x: number; y: number; clickedId: number | null; shift: boolean } | null>(null);
  const [lasso, setLasso] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);

  const boxes = boxesByPage.get(pageNo) ?? [];
  const selected = selectedByPage.get(pageNo) ?? new Set<number>();
  const totalSelected = Array.from(selectedByPage.values()).reduce((n, s) => n + s.size, 0);
  const pagesWithSelection = Array.from(selectedByPage.keys()).filter((p) => (selectedByPage.get(p) ?? new Set()).size > 0).sort((a, b) => a - b);

  useEffect(() => {
    (async () => {
      try {
        const pp = await getPages(subId);
        if (pp.page_count === 0) {
          setErr("No layout available — this is probably a DOCX upload. Locate works only for PDFs.");
        } else {
          setPages(pp.pages);
          setPageNo((p) => Math.min(Math.max(1, p), pp.page_count));
        }
      } catch (e) { setErr(String(e)); }
    })();
  }, [subId]);

  useEffect(() => {
    if (pages.length === 0) return;
    if (boxesByPage.has(pageNo)) return;
    (async () => {
      try {
        const r = await getPageBoxes(subId, pageNo);
        setBoxesByPage((prev) => {
          const m = new Map(prev);
          m.set(pageNo, r.boxes);
          return m;
        });
      } catch (e) { setErr(String(e)); }
    })();
  }, [subId, pageNo, pages.length, boxesByPage]);

  function setSelectedForPage(pg: number, updater: (prev: Set<number>) => Set<number>) {
    setSelectedByPage((prev) => {
      const cur = new Set(prev.get(pg) ?? []);
      const next = updater(cur);
      const m = new Map(prev);
      if (next.size === 0) m.delete(pg);
      else m.set(pg, next);
      return m;
    });
  }
  function toggleBox(id: number) {
    setSelectedForPage(pageNo, (prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // Convert a mouse event to image-px coords (using the canvas's
  // bounding rect — works regardless of the displayed render size).
  function pointInImagePx(e: React.MouseEvent): { x: number; y: number } | null {
    const wrap = canvasRef.current;
    const cur = pages.find((p) => p.page === pageNo);
    if (!wrap || !cur) return null;
    const rect = wrap.getBoundingClientRect();
    const xPct = (e.clientX - rect.left) / rect.width;
    const yPct = (e.clientY - rect.top) / rect.height;
    return { x: xPct * cur.w_px, y: yPct * cur.h_px };
  }
  function boxAt(p: { x: number; y: number }): number | null {
    for (const b of boxes) {
      if (p.x >= b.bbox.x && p.x <= b.bbox.x + b.bbox.w &&
          p.y >= b.bbox.y && p.y <= b.bbox.y + b.bbox.h) {
        return b.id;
      }
    }
    return null;
  }
  function boxesInRect(r: { x0: number; y0: number; x1: number; y1: number }): number[] {
    const xMin = Math.min(r.x0, r.x1), xMax = Math.max(r.x0, r.x1);
    const yMin = Math.min(r.y0, r.y1), yMax = Math.max(r.y0, r.y1);
    const out: number[] = [];
    for (const b of boxes) {
      const bx2 = b.bbox.x + b.bbox.w;
      const by2 = b.bbox.y + b.bbox.h;
      const noOverlap = bx2 < xMin || b.bbox.x > xMax || by2 < yMin || b.bbox.y > yMax;
      if (!noOverlap) out.push(b.id);
    }
    return out;
  }
  function onCanvasMouseDown(e: React.MouseEvent) {
    if (e.button !== 0) return;
    const p = pointInImagePx(e);
    if (!p) return;
    dragStartRef.current = {
      x: p.x, y: p.y,
      clickedId: boxAt(p),
      shift: e.shiftKey,
    };
    setLasso(null);
    e.preventDefault();
  }
  function onCanvasMouseMove(e: React.MouseEvent) {
    if (!dragStartRef.current) return;
    const p = pointInImagePx(e);
    if (!p) return;
    const dx = p.x - dragStartRef.current.x;
    const dy = p.y - dragStartRef.current.y;
    // 6 image-px threshold to distinguish a click from a drag
    if (lasso == null && Math.hypot(dx, dy) < 6) return;
    setLasso({
      x0: dragStartRef.current.x, y0: dragStartRef.current.y,
      x1: p.x, y1: p.y,
    });
  }
  function onCanvasMouseUp() {
    const start = dragStartRef.current;
    if (!start) return;
    if (lasso) {
      const ids = boxesInRect(lasso);
      setSelectedForPage(pageNo, (prev) => {
        const next = new Set(prev);
        if (start.shift) {
          for (const id of ids) next.delete(id);
        } else {
          for (const id of ids) next.add(id);
        }
        return next;
      });
    } else if (start.clickedId != null) {
      toggleBox(start.clickedId);
    }
    dragStartRef.current = null;
    setLasso(null);
  }
  function onCanvasMouseLeave() {
    // Cancel any in-flight drag if cursor leaves the canvas
    if (dragStartRef.current) {
      dragStartRef.current = null;
      setLasso(null);
    }
  }

  // Aggregate selections across every page the editor has touched, in page
  // order. previewText, the box-id list, and the multi-page `selections`
  // payload all derive from this so the Locate flow works for content that
  // spans multiple pages (e.g. a 3-page reference list).
  const allSelectedBoxes: LayoutBox[] = [];
  for (const pg of pagesWithSelection) {
    const pageBoxes = boxesByPage.get(pg) ?? [];
    const sel = selectedByPage.get(pg) ?? new Set<number>();
    for (const b of pageBoxes) if (sel.has(b.id)) allSelectedBoxes.push(b);
  }
  const previewText = allSelectedBoxes.map((b) => b.text).filter(Boolean).join("\n\n");
  const selectionsPayload: LocateSelection[] = pagesWithSelection.map((pg) => ({
    page: pg,
    box_ids: Array.from(selectedByPage.get(pg) ?? []),
  }));

  async function save() {
    if (totalSelected === 0) {
      setErr("Click at least one box on the page.");
      return;
    }
    setErr(null);
    setSubmitting(true);
    try {
      // Pass the first selected page's IDs as the legacy single-page payload
      // for backwards compatibility, plus the full multi-page selections.
      const firstPage = pagesWithSelection[0] ?? pageNo;
      const firstPageIds = Array.from(selectedByPage.get(firstPage) ?? []);
      await onSubmit(firstPage, firstPageIds, previewText, selectionsPayload);
    } catch (e) {
      setErr(String(e));
    } finally {
      setSubmitting(false);
    }
  }
  function clearAll() {
    setSelectedByPage(new Map());
  }

  if (err && pages.length === 0) {
    return <div className="locate-panel"><p className="error">{err}</p></div>;
  }
  if (pages.length === 0) {
    return <div className="locate-panel"><p className="muted loading">Loading page</p></div>;
  }

  const currentPage = pages.find((p) => p.page === pageNo);

  return (
    <div className="locate-panel">
      <div className="locate-controls">
        <button className="ghost" onClick={() => setPageNo((p) => Math.max(1, p - 1))} disabled={pageNo <= 1}>
          <ArrowLeftIcon size={13} /> Prev
        </button>
        <span className="muted small">
          Page <input type="number" min={1} max={pages.length} value={pageNo}
            onChange={(e) => setPageNo(Math.max(1, Math.min(pages.length, Number(e.target.value))))}
            style={{ width: 50 }} /> of {pages.length}
        </span>
        <button className="ghost" onClick={() => setPageNo((p) => Math.min(pages.length, p + 1))} disabled={pageNo >= pages.length}>
          Next <ArrowRightIcon size={13} />
        </button>
        <span className="muted small">
          · {boxes.length} boxes · {selected.size} on this page
          {pagesWithSelection.length > 1 && (
            <> · {totalSelected} total across pp.{pagesWithSelection.join(", ")}</>
          )}
        </span>
        <span className="spacer" />
        <button className="ghost" onClick={clearAll} disabled={totalSelected === 0}>Clear all</button>
        <button className="primary" onClick={save} disabled={submitting || totalSelected === 0}>
          {submitting ? "Saving…" : (
            <>
              <CheckIcon size={13} /> Use selection for {field.label}
            </>
          )}
        </button>
      </div>

      <p className="muted small" style={{ margin: "0 0 8px" }}>
        Drag to select multiple at once. Shift-drag to deselect a region.
        Click any box to toggle it individually. Selections on other pages are kept.
      </p>
      <div
        ref={canvasRef}
        className="page-canvas-wrap locate-canvas"
        style={currentPage ? { aspectRatio: `${currentPage.w_px} / ${currentPage.h_px}`, cursor: "crosshair", userSelect: "none" } : undefined}
        onMouseDown={onCanvasMouseDown}
        onMouseMove={onCanvasMouseMove}
        onMouseUp={onCanvasMouseUp}
        onMouseLeave={onCanvasMouseLeave}
      >
        {currentPage && (
          <>
            <img src={pageImageUrl(subId, pageNo)} draggable={false} />
            <div className="overlay-layer">
              {boxes.map((b) => {
                const sel = selected.has(b.id);
                return (
                  <div
                    key={b.id}
                    className={`box ${sel ? "selected" : ""}`}
                    style={{
                      left: `${(b.bbox.x / currentPage.w_px) * 100}%`,
                      top: `${(b.bbox.y / currentPage.h_px) * 100}%`,
                      width: `${(b.bbox.w / currentPage.w_px) * 100}%`,
                      height: `${(b.bbox.h / currentPage.h_px) * 100}%`,
                      borderColor: sel ? "var(--color-onyx-outline)" : "rgba(122, 121, 116, 0.35)",
                      background: sel ? "var(--accent-soft)" : "transparent",
                      pointerEvents: "none",
                    }}
                    title={b.text.slice(0, 120)}
                  />
                );
              })}
              {lasso && currentPage && (() => {
                const xMin = Math.min(lasso.x0, lasso.x1);
                const yMin = Math.min(lasso.y0, lasso.y1);
                const w = Math.abs(lasso.x1 - lasso.x0);
                const h = Math.abs(lasso.y1 - lasso.y0);
                const subtract = !!dragStartRef.current?.shift;
                return (
                  <div
                    className={`lasso-rect ${subtract ? "subtract" : ""}`}
                    style={{
                      left: `${(xMin / currentPage.w_px) * 100}%`,
                      top: `${(yMin / currentPage.h_px) * 100}%`,
                      width: `${(w / currentPage.w_px) * 100}%`,
                      height: `${(h / currentPage.h_px) * 100}%`,
                    }}
                  />
                );
              })()}
            </div>
          </>
        )}
      </div>

      {previewText && (
        <details className="locate-preview" open>
          <summary className="muted small">Preview ({previewText.length} chars)</summary>
          <pre className="section-text">{previewText}</pre>
        </details>
      )}
      {err && <p className="error" style={{ marginTop: 8 }}>{err}</p>}
    </div>
  );
}

const STATE_INFO: Record<CardState, { Icon: IconType; color: string; label: string }> = {
  confirmed:    { Icon: CheckIcon,      color: "var(--ok)",            label: "Confirmed" },
  pending:      { Icon: HelpCircleIcon, color: "var(--warn)",          label: "Pending confirmation" },
  needs_pick:   { Icon: ScaleIcon,      color: "var(--info)",          label: "Pick candidate" },
  needs_locate: { Icon: XIcon,          color: "var(--error)",         label: "Locate in document" },
  missing:      { Icon: CircleIcon,     color: "var(--fg-tertiary)",   label: "Missing" },
  manual:       { Icon: PencilIcon,     color: "var(--info)",          label: "Needs you" },
};


function LeverageBadge({ leverage }: { leverage: "deterministic" | "api" | "ai" }) {
  const cfg = {
    deterministic: { label: "free",     title: "Deterministic — regex / Docling, no LLM needed" },
    api:           { label: "lookup",   title: "Free enricher API lookup; LLM only for ambiguous picks" },
    ai:            { label: "ai",       title: "AI enrichment recommended — LLM is a step-change here" },
  }[leverage];
  return (
    <span className={`leverage-badge leverage-${leverage}`} title={cfg.title}>
      {cfg.label}
    </span>
  );
}
