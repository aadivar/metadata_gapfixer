import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  autofix,
  autofixAll,
  buildXml,
  confirmField,
  enrichAll,
  FieldScore,
  getCost,
  getMetadata,
  getPages,
  getPageBoxes,
  getScore,
  LayoutBox,
  locateAuthorsAffiliations,
  locateField,
  pageImageUrl,
  PageInfo,
  putMetadata,
  rejectField,
  Scorecard,
  Tier,
  xmlDownloadUrl,
} from "../api";

const TIER_DESCRIPTION: Record<Tier, string> = {
  T0: "Crossref schema 5.4.0 — required to deposit.",
  T1: "Crossref recommended — what makes the record usable for indexers.",
  T2: "Crossref Participation / Nexus benchmarks — cross-system linking.",
  T3: "Crossref+DataCite integrity guide — what makes this record trustable.",
};

const TIER_ORDER: Tier[] = ["T0", "T1", "T2", "T3"];

// Fields owned by the Authors & Affiliations supercard at the top.
// We hide these from the per-tier rendering so the editor doesn't see the
// same person/affiliation data repeated in five places.
const SUPERCARD_OWNED_KEYS = new Set<string>([
  "authors_any",
  "full_author_names",
  "affiliations_listed",
  "orcid_for_corresponding",
  "orcid_for_all_authors",
  "ror_for_all_affiliations",
]);

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

  async function confirm(field: FieldScore) {
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

  async function locate(field: FieldScore, page: number, boxIds: number[]) {
    const path = (field.metadata_paths || [])[0] || field.key;
    setBusy(`Saving location for ${field.label}…`);
    try {
      const res = await locateField(subId, path, page, boxIds);
      setCard(res.score);
      showToast(`${field.label} set from page ${page} selection`);
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

  // Group fields by tier in declared order — skip fields owned by the supercard
  const fieldsByTier: Record<Tier, FieldScore[]> = { T0: [], T1: [], T2: [], T3: [] };
  card.fields.forEach((f) => {
    if (SUPERCARD_OWNED_KEYS.has(f.key)) return;
    fieldsByTier[f.tier].push(f);
  });

  return (
    <section>
      <div className="page-header">
        <div className="crumbs"><Link to="/upload">Submissions</Link> / #{subId}</div>
        <div className="row">
          <h1>Metadata gap report</h1>
          <span className="muted small mono">cost so far · ${cost.total_usd.toFixed(4)}</span>
        </div>
      </div>

      {/* HERO */}
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
                  <span className="tier-name"><strong>{t.tier}</strong> {t.label}</span>
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
          <button className="primary" onClick={fixAll} disabled={busy !== null}>
            Run automated extraction
          </button>
          {(card.estimated_full_enrichment_usd ?? 0) > 0 && (
            <button onClick={premiumEnrichAll} disabled={busy !== null} title="Run all AI enrichers in sequence">
              Run AI enrichment<span className="btn-meta">~${(card.estimated_full_enrichment_usd ?? 0).toFixed(4)}</span>
            </button>
          )}
          <button onClick={generateXml} disabled={busy !== null || card.tiers[0].score < 80}>
            Generate Crossref XML
          </button>
          {xmlBuilt && <a className="btn" href={xmlDownloadUrl(subId)} target="_blank" rel="noreferrer">Download XML</a>}
        </div>
        {busy && <p className="muted loading" style={{ marginTop: 12 }}>{busy}</p>}
        {err && <p className="error" style={{ marginTop: 12 }}>{err}</p>}
        {toast && <p className="toast">{toast}</p>}
      </div>

      {/* COMBINED AUTHORS + AFFILIATIONS SUPERCARD (T1) */}
      <AuthorsAffiliationsSuperCard
        subId={subId}
        card={card}
        busy={busy !== null}
        onUpdate={async () => { await refresh(); }}
        showToast={showToast}
        setBusy={setBusy}
        setErr={setErr}
      />

      {/* PER-TIER FIELD CARDS */}
      {TIER_ORDER.map((tier) => {
        const fields = fieldsByTier[tier];
        if (fields.length === 0) return null;
        const tinfo = card.tiers.find((t) => t.tier === tier)!;
        return (
          <div key={tier} className="card tier-section">
            <div className="row" style={{ marginBottom: 16 }}>
              <div>
                <h2 className="card-title" style={{ margin: 0 }}>
                  <span className="tier-badge">{tier}</span> {tinfo.label}
                </h2>
                <p className="muted small" style={{ margin: "4px 0 0" }}>{TIER_DESCRIPTION[tier]}</p>
              </div>
              <span className="mono small" style={{ color: scoreColor(tinfo.score) }}>
                {tinfo.score}% · {tinfo.fields_present}/{tinfo.fields_total}
              </span>
            </div>
            <div className="field-cards">
              {fields.map((f) => (
                <FieldCard
                  key={f.key}
                  subId={subId}
                  field={f}
                  expanded={expanded.has(f.key)}
                  onToggleExpand={() => toggleExpand(f.key)}
                  onConfirm={() => confirm(f)}
                  onReject={() => reject(f)}
                  onAutofix={f.autofix_action ? () => fixOne(f.autofix_action!) : undefined}
                  onLocate={(page, boxIds) => locate(f, page, boxIds)}
                  busy={busy !== null}
                />
              ))}
            </div>
          </div>
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

function FieldCard({
  subId, field, expanded, onToggleExpand, onConfirm, onReject, onAutofix, onLocate, busy,
}: {
  subId: number;
  field: FieldScore;
  expanded: boolean;
  onToggleExpand: () => void;
  onConfirm: () => void;
  onReject: () => void;
  onAutofix?: () => void;
  onLocate: (page: number, boxIds: number[]) => Promise<void>;
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

  return (
    <div className={`field-card field-state-${state}`}>
      <div className="field-row" onClick={onToggleExpand}>
        <span className="field-icon" style={{ color: stateInfo.color }}>{stateInfo.icon}</span>
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
        <span className="field-expand">{expanded ? "▾" : "▸"}</span>
      </div>

      {expanded && (
        <div className="field-detail">
          <p className="muted small">{field.why}</p>

          {state === "confirmed" && (
            <div className="actions">
              <button className="ghost" onClick={onReject} disabled={busy}>Reject and re-identify</button>
              {canLocate && (
                <button className="ghost" onClick={() => setLocateOpen((v) => !v)} disabled={busy}>
                  {locateOpen ? "Close" : "Identify on document"}
                </button>
              )}
            </div>
          )}

          {state === "pending" && (
            <>
              {field.provenance_reasoning && <p className="small mono">{field.provenance_reasoning}</p>}
              <div className="actions">
                <button className="primary" onClick={onConfirm} disabled={busy}>Confirm</button>
                <button className="ghost" onClick={onReject} disabled={busy}>Reject and re-identify</button>
                <button className="ghost" onClick={() => setLocateOpen((v) => !v)} disabled={busy}>
                  {locateOpen ? "Close" : "Identify on document"}
                </button>
              </div>
            </>
          )}

          {state === "missing" && (
            <div className="actions">
              {onAutofix && <button className="primary" onClick={onAutofix} disabled={busy}>Run extraction</button>}
              <button className={onAutofix ? "ghost" : "primary"} onClick={() => setLocateOpen((v) => !v)} disabled={busy}>
                Identify on document
              </button>
            </div>
          )}

          {state === "needs_locate" && (
            <p className="muted small">Pick the box(es) on the page that contain this field's value.</p>
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
              onSubmit={async (page, boxIds) => {
                await onLocate(page, boxIds);
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
  onSubmit: (page: number, boxIds: number[]) => Promise<void>;
}) {
  const [pages, setPages] = useState<PageInfo[]>([]);
  const [pageNo, setPageNo] = useState(defaultPage);
  const [boxes, setBoxes] = useState<LayoutBox[]>([]);
  const [pageDims, setPageDims] = useState<{ w: number; h: number }>({ w: 1, h: 1 });
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [err, setErr] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

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
    (async () => {
      try {
        const r = await getPageBoxes(subId, pageNo);
        setBoxes(r.boxes);
        setPageDims({ w: r.w_px, h: r.h_px });
        setSelected(new Set());  // clear selection when page changes
      } catch (e) { setErr(String(e)); }
    })();
  }, [subId, pageNo, pages.length]);

  function toggleBox(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const selectedBoxes = boxes.filter((b) => selected.has(b.id));
  const previewText = selectedBoxes.map((b) => b.text).filter(Boolean).join("\n\n");

  async function save() {
    if (selected.size === 0) {
      setErr("Click at least one box on the page.");
      return;
    }
    setErr(null);
    setSubmitting(true);
    try {
      await onSubmit(pageNo, [...selected]);
    } catch (e) {
      setErr(String(e));
    } finally {
      setSubmitting(false);
    }
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
        <button className="ghost" onClick={() => setPageNo((p) => Math.max(1, p - 1))} disabled={pageNo <= 1}>← Prev</button>
        <span className="muted small">
          Page <input type="number" min={1} max={pages.length} value={pageNo}
            onChange={(e) => setPageNo(Math.max(1, Math.min(pages.length, Number(e.target.value))))}
            style={{ width: 50 }} /> of {pages.length}
        </span>
        <button className="ghost" onClick={() => setPageNo((p) => Math.min(pages.length, p + 1))} disabled={pageNo >= pages.length}>Next →</button>
        <span className="muted small">· {boxes.length} boxes · {selected.size} selected</span>
        <span className="spacer" />
        <button className="ghost" onClick={() => setSelected(new Set())} disabled={selected.size === 0}>Clear</button>
        <button className="primary" onClick={save} disabled={submitting || selected.size === 0}>
          {submitting ? "Saving…" : `✓ Use selection for ${field.label}`}
        </button>
      </div>

      <div
        className="page-canvas-wrap locate-canvas"
        style={currentPage ? { aspectRatio: `${currentPage.w_px} / ${currentPage.h_px}` } : undefined}
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
                      borderColor: sel ? "var(--accent)" : "rgba(124, 92, 255, 0.3)",
                      background: sel ? "var(--accent-soft)" : "transparent",
                    }}
                    title={b.text.slice(0, 120)}
                    onClick={() => toggleBox(b.id)}
                  />
                );
              })}
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

const STATE_INFO: Record<CardState, { icon: string; color: string; label: string }> = {
  confirmed:    { icon: "✓", color: "var(--ok)",    label: "Confirmed" },
  pending:      { icon: "?", color: "var(--warn)",  label: "Pending confirmation" },
  needs_pick:   { icon: "⚖", color: "var(--info)",  label: "Pick candidate" },
  needs_locate: { icon: "✗", color: "var(--error)", label: "Locate in document" },
  missing:      { icon: "○", color: "var(--fg-tertiary)", label: "Missing" },
  manual:       { icon: "✎", color: "var(--info)",  label: "Needs you" },
};

// ============================================================================
// Authors + Affiliations supercard — single combined interaction
// ============================================================================

type SupercardStage = "locate" | "enrich" | "verify" | "done";

function AuthorsAffiliationsSuperCard({
  subId, card, busy, onUpdate, showToast, setBusy, setErr,
}: {
  subId: number;
  card: Scorecard;
  busy: boolean;
  onUpdate: () => Promise<void>;
  showToast: (msg: string) => void;
  setBusy: (s: string | null) => void;
  setErr: (s: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [listOpen, setListOpen] = useState(true);
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    return localStorage.getItem(`supercard.aa.collapsed.${subId}`) === "1";
  });
  const [authors, setAuthors] = useState<any[]>([]);
  const [provenance, setProvenance] = useState<Record<string, any>>({});

  function toggleCollapsed() {
    setCollapsed((v) => {
      const n = !v;
      localStorage.setItem(`supercard.aa.collapsed.${subId}`, n ? "1" : "0");
      return n;
    });
  }

  useEffect(() => {
    (async () => {
      try {
        const m = await getMetadata(subId);
        setAuthors(m.authors || []);
        setProvenance(m.provenance || {});
      } catch (e) {
        setAuthors([]);
      }
    })();
  }, [subId, card]);

  // Read current state for every rubric field this supercard owns
  const ownedFields: { key: string; tier: string; label: string; field: FieldScore | undefined; state: CardState }[] = [
    "authors_any", "full_author_names", "affiliations_listed",
    "orcid_for_corresponding", "orcid_for_all_authors", "ror_for_all_affiliations",
  ].map((k) => {
    const f = card.fields.find((x) => x.key === k);
    return { key: k, tier: f?.tier ?? "", label: f?.label ?? k, field: f, state: f ? deriveState(f) : "missing" };
  });

  if (!ownedFields[0].field) return null;

  const stateGlyph: Record<CardState, string> = {
    confirmed: "✓", pending: "?", needs_pick: "⚖",
    needs_locate: "✗", missing: "○", manual: "✎",
  };
  const stateColor: Record<CardState, string> = {
    confirmed: "var(--ok)", pending: "var(--warn)", needs_pick: "var(--info)",
    needs_locate: "var(--error)", missing: "var(--fg-tertiary)", manual: "var(--info)",
  };

  const presentCount = ownedFields.filter((o) => o.field?.status === "present").length;

  // Derive the current stage from the actual data — drives which CTA shows.
  const haveAuthors = authors.length > 0;
  const haveAnyOrcid = authors.some((a) => a.orcid);
  const haveAnyROR = authors.some((a) => (a.ror_ids || []).some((r: any) => r));
  const allHaveOrcid = haveAuthors && authors.every((a) => a.orcid);
  const allAffsHaveROR = haveAuthors && authors.every((a) => {
    const affs = a.affiliations || [];
    if (affs.length === 0) return true;
    const rors = a.ror_ids || [];
    return affs.every((_: string, i: number) => rors[i]);
  });

  let stage: SupercardStage;
  if (!haveAuthors) stage = "locate";
  else if (!haveAnyOrcid && !haveAnyROR) stage = "enrich";
  else if (!allHaveOrcid || !allAffsHaveROR) stage = "verify";
  else stage = "done";

  async function fetchOrcidsAndRors() {
    setBusy("Fetching ORCIDs and RORs (free)…");
    setErr(null);
    try {
      await autofix(subId, "resolve_orcids");
      await autofix(subId, "resolve_rors");
      await onUpdate();
      showToast("ORCID + ROR sweeps complete (free)");
    } catch (e) { setErr(String(e)); }
    finally { setBusy(null); }
  }

  async function verifyWithAI() {
    setBusy("Verifying authors with AI…");
    setErr(null);
    try {
      const r = await fetch(`${(import.meta.env.VITE_API_BASE_URL as string) || "http://localhost:8000"}/submissions/${subId}/structure/verify_authors`, { method: "POST" });
      if (!r.ok) throw new Error(`verify failed: ${r.status} ${await r.text()}`);
      await onUpdate();
      showToast("AI verification complete · check the evidence chains");
    } catch (e) { setErr(String(e)); }
    finally { setBusy(null); }
  }

  return (
    <div className={`card supercard ${collapsed ? "supercard-collapsed" : ""}`}>
      <div className="row supercard-header" onClick={toggleCollapsed} style={{ cursor: "pointer" }}>
        <div className="cluster" style={{ alignItems: "baseline" }}>
          <span className="muted">{collapsed ? "›" : "⌄"}</span>
          <h2 className="card-title" style={{ margin: 0 }}>Authors and affiliations</h2>
        </div>
        <span className="mono small" style={{ color: presentCount === ownedFields.length ? "var(--ok)" : "var(--fg-tertiary)" }}>
          {presentCount}/{ownedFields.length} fields · {stage}
        </span>
      </div>

      {!collapsed && (
        <>
          <p className="muted small" style={{ margin: "6px 0 0" }}>
            Linked via superscript markers — extracted, looked up, and verified together.
            Covers {ownedFields.length} rubric fields across T0–T2.
          </p>

          <div className="supercard-checklist">
            {ownedFields.map((o) => (
              <div key={o.key} className="supercard-check-row" title={o.field?.why || ""}>
                <span style={{ color: stateColor[o.state] }}>{stateGlyph[o.state]}</span>
                <span className="check-tier mono">{o.tier}</span>
                <span className="check-label">{o.label}</span>
                <span className="check-preview muted">{o.field?.value_preview ?? "—"}</span>
              </div>
            ))}
          </div>

          <div className="actions supercard-actions">
            {stage === "locate" && (
              <button className="primary" onClick={() => setOpen((v) => !v)} disabled={busy}>
                {open ? "Close" : "Identify on document"}
              </button>
            )}

            {stage === "enrich" && (
              <>
                <button className="primary" onClick={fetchOrcidsAndRors} disabled={busy}>
                  Run lookups<span className="btn-meta">ORCID · ROR · free</span>
                </button>
                <button onClick={verifyWithAI} disabled={busy}>
                  Cross-source verify<span className="btn-meta">AI · ~$0.011</span>
                </button>
                <button className="ghost" onClick={() => setOpen((v) => !v)} disabled={busy}>
                  {open ? "Close" : "Re-identify"}
                </button>
              </>
            )}

            {stage === "verify" && (
              <>
                <button className="primary" onClick={verifyWithAI} disabled={busy}>
                  Cross-source verify<span className="btn-meta">AI · ~$0.011</span>
                </button>
                <button onClick={fetchOrcidsAndRors} disabled={busy}>
                  Re-run lookups
                </button>
                <button className="ghost" onClick={() => setOpen((v) => !v)} disabled={busy}>
                  {open ? "Close" : "Re-identify"}
                </button>
              </>
            )}

            {stage === "done" && (
              <>
                <span className="status status-ready">complete</span>
                <button className="ghost" onClick={verifyWithAI} disabled={busy}>
                  Re-verify
                </button>
                <button className="ghost" onClick={() => setOpen((v) => !v)} disabled={busy}>
                  {open ? "Close" : "Re-identify"}
                </button>
              </>
            )}
          </div>

      {open && (
        <AuthorsAffiliationsLocate
          subId={subId}
          onCancel={() => setOpen(false)}
          onSubmit={async (page, authorIds, affilIds) => {
            setBusy("Structuring authors & affiliations…");
            setErr(null);
            try {
              const res = await locateAuthorsAffiliations(subId, page, authorIds, affilIds);
              await onUpdate();
              showToast(`Linked ${res.report?.authors_count ?? 0} authors with affiliations`);
              setOpen(false);
            } catch (e) {
              setErr(String(e));
            } finally {
              setBusy(null);
            }
          }}
        />
      )}

      {!collapsed && authors.length > 0 && (
        <div className="authors-list">
          <button
            className="ghost authors-list-toggle"
            onClick={() => setListOpen((v) => !v)}
            aria-expanded={listOpen}
          >
            <span>{listOpen ? "▾" : "▸"}</span>
            <strong>{authors.length} extracted authors</strong>
            <span className="muted small">
              · {authors.filter((a) => a.orcid).length}/{authors.length} ORCID
              · {authors.filter((a) => (a.ror_ids || []).some((r: any) => r)).length}/{authors.length} have ROR
            </span>
          </button>
          {listOpen && (
            <ol className="author-rows">
              {authors.map((a: any, i: number) => {
                const prov = provenance[`authors[${i}]`] || {};
                const evidence: string[] = prov.evidence_chain || [];
                return (
                  <li key={i} className="author-row">
                    <div className="author-main">
                      <span className="author-name">
                        {a.full_name || `${a.given_name || ""} ${a.surname || ""}`.trim() || "(unnamed)"}
                      </span>
                      {a.is_corresponding && <span className="chip chip-warn">corresponding</span>}
                      {a.orcid ? (
                        <a className="chip chip-orcid" href={`https://orcid.org/${a.orcid}`} target="_blank" rel="noreferrer">
                          ORCID {a.orcid}
                        </a>
                      ) : (
                        <span className="chip chip-missing">no ORCID</span>
                      )}
                      {a.email && <span className="muted small mono">{a.email}</span>}
                    </div>
                    {(a.affiliations && a.affiliations.length > 0) ? (
                      <ul className="aff-rows">
                        {a.affiliations.map((aff: string, j: number) => {
                          const ror = (a.ror_ids || [])[j];
                          return (
                            <li key={j} className="aff-row">
                              <span className="aff-text">{aff}</span>
                              {ror ? (
                                <a className="chip chip-ror" href={ror} target="_blank" rel="noreferrer">
                                  {ror.replace("https://ror.org/", "ROR ")}
                                </a>
                              ) : (
                                <span className="chip chip-missing">no ROR</span>
                              )}
                            </li>
                          );
                        })}
                      </ul>
                    ) : (
                      <p className="muted small" style={{ marginLeft: 26 }}>no affiliations attached</p>
                    )}
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
          )}
        </div>
      )}
        </>
      )}
    </div>
  );
}

type LocateMode = "authors" | "affiliations";

function AuthorsAffiliationsLocate({
  subId, onCancel, onSubmit,
}: {
  subId: number;
  onCancel: () => void;
  onSubmit: (page: number, authorIds: number[], affilIds: number[]) => Promise<void>;
}) {
  const [pages, setPages] = useState<PageInfo[]>([]);
  const [pageNo, setPageNo] = useState(1);
  const [boxes, setBoxes] = useState<LayoutBox[]>([]);
  const [mode, setMode] = useState<LocateMode>("authors");
  const [authorIds, setAuthorIds] = useState<Set<number>>(new Set());
  const [affilIds, setAffilIds] = useState<Set<number>>(new Set());
  const [err, setErr] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const pp = await getPages(subId);
        if (pp.page_count === 0) {
          setErr("No layout — DOCX uploads can't use Locate.");
        } else {
          setPages(pp.pages);
        }
      } catch (e) { setErr(String(e)); }
    })();
  }, [subId]);

  useEffect(() => {
    if (pages.length === 0) return;
    (async () => {
      try {
        const r = await getPageBoxes(subId, pageNo);
        setBoxes(r.boxes);
      } catch (e) { setErr(String(e)); }
    })();
  }, [subId, pageNo, pages.length]);

  function toggleBox(id: number) {
    // A box can be in either set, but not both. Clicking in author-mode
    // adds to authors and removes from affiliations (and vice versa).
    if (mode === "authors") {
      setAffilIds((prev) => { const n = new Set(prev); n.delete(id); return n; });
      setAuthorIds((prev) => {
        const n = new Set(prev);
        if (n.has(id)) n.delete(id); else n.add(id);
        return n;
      });
    } else {
      setAuthorIds((prev) => { const n = new Set(prev); n.delete(id); return n; });
      setAffilIds((prev) => {
        const n = new Set(prev);
        if (n.has(id)) n.delete(id); else n.add(id);
        return n;
      });
    }
  }

  async function save() {
    if (authorIds.size === 0 && affilIds.size === 0) {
      setErr("Tag at least one box.");
      return;
    }
    setErr(null);
    setSubmitting(true);
    try {
      await onSubmit(pageNo, [...authorIds], [...affilIds]);
    } catch (e) {
      setErr(String(e));
    } finally {
      setSubmitting(false);
    }
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
        <button className="ghost" onClick={() => setPageNo((p) => Math.max(1, p - 1))} disabled={pageNo <= 1}>← Prev</button>
        <span className="muted small">
          Page <input type="number" min={1} max={pages.length} value={pageNo}
            onChange={(e) => setPageNo(Math.max(1, Math.min(pages.length, Number(e.target.value))))}
            style={{ width: 50 }} /> of {pages.length}
        </span>
        <button className="ghost" onClick={() => setPageNo((p) => Math.min(pages.length, p + 1))} disabled={pageNo >= pages.length}>Next →</button>
        <span className="spacer" />
        <span className="muted small">{boxes.length} boxes · {authorIds.size} authors · {affilIds.size} affs</span>
      </div>

      <div className="mode-toggle">
        <span className="muted small">Tag boxes as:</span>
        <button
          className={`mode-btn mode-authors ${mode === "authors" ? "active" : ""}`}
          onClick={() => setMode("authors")}
        >
          ● Authors ({authorIds.size})
        </button>
        <button
          className={`mode-btn mode-affils ${mode === "affiliations" ? "active" : ""}`}
          onClick={() => setMode("affiliations")}
        >
          ● Affiliations ({affilIds.size})
        </button>
        <span className="spacer" />
        <button className="ghost" onClick={() => { setAuthorIds(new Set()); setAffilIds(new Set()); }}>Clear all</button>
        <button className="ghost" onClick={onCancel}>Cancel</button>
        <button className="primary" onClick={save} disabled={submitting || (authorIds.size === 0 && affilIds.size === 0)}>
          {submitting ? "Structuring…" : "Extract linked authors  (~$0.0006)"}
        </button>
      </div>

      <div
        className="page-canvas-wrap locate-canvas"
        style={currentPage ? { aspectRatio: `${currentPage.w_px} / ${currentPage.h_px}` } : undefined}
      >
        {currentPage && (
          <>
            <img src={pageImageUrl(subId, pageNo)} draggable={false} />
            <div className="overlay-layer">
              {boxes.map((b) => {
                const isAuthor = authorIds.has(b.id);
                const isAffil = affilIds.has(b.id);
                let color = "rgba(124, 92, 255, 0.25)";
                let bg = "transparent";
                let cls = "";
                if (isAuthor) { color = "#f87171"; bg = "rgba(248, 113, 113, 0.18)"; cls = "selected"; }
                else if (isAffil) { color = "#60a5fa"; bg = "rgba(96, 165, 250, 0.18)"; cls = "selected"; }
                return (
                  <div
                    key={b.id}
                    className={`box ${cls}`}
                    style={{
                      left: `${(b.bbox.x / currentPage.w_px) * 100}%`,
                      top: `${(b.bbox.y / currentPage.h_px) * 100}%`,
                      width: `${(b.bbox.w / currentPage.w_px) * 100}%`,
                      height: `${(b.bbox.h / currentPage.h_px) * 100}%`,
                      borderColor: color,
                      background: bg,
                    }}
                    title={b.text.slice(0, 120)}
                    onClick={() => toggleBox(b.id)}
                  />
                );
              })}
            </div>
          </>
        )}
      </div>

      {err && <p className="error" style={{ marginTop: 8 }}>{err}</p>}
    </div>
  );
}


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
