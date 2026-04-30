import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Entity,
  getLabelPresets,
  getPages,
  getPageBoxes,
  getSection,
  getSections,
  LayoutBox,
  PageInfo,
  pageImageUrl,
  runNer,
  Section,
} from "../api";

const CATEGORY_COLOR: Record<string, string> = {
  header: "#7c5cff",
  text: "#9ca3af",
  table: "#fbbf24",
  figure: "#f87171",
  furniture: "#4b5563",
  code: "#4ade80",
  formula: "#a78bfa",
  other: "#6b7280",
};

type Mode = "layout" | "sections";

export default function Inspect() {
  const { id } = useParams();
  const subId = Number(id);
  const [mode, setMode] = useState<Mode>("layout");
  const [presets, setPresets] = useState<Record<string, Record<string, string>>>({});
  const [preset, setPreset] = useState<string>("header");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const [pages, setPages] = useState<PageInfo[]>([]);
  const [hasLayout, setHasLayout] = useState(true);
  const [pageNo, setPageNo] = useState(1);
  const [boxes, setBoxes] = useState<LayoutBox[]>([]);
  const [pageDims, setPageDims] = useState<{ w: number; h: number }>({ w: 1, h: 1 });
  const [selectedBoxIds, setSelectedBoxIds] = useState<Set<number>>(new Set());

  const [sections, setSections] = useState<Section[]>([]);
  const [selectedSecId, setSelectedSecId] = useState<number | null>(null);
  const [section, setSection] = useState<Section | null>(null);

  const [entityResults, setEntityResults] = useState<Entity[]>([]);

  useEffect(() => {
    (async () => {
      try {
        const [pp, ps] = await Promise.all([getPages(subId), getLabelPresets()]);
        setPresets(ps);
        if (pp.page_count > 0) {
          setPages(pp.pages);
          setHasLayout(true);
        } else {
          setHasLayout(false);
          setMode("sections");
          const secs = await getSections(subId);
          setSections(secs.sections);
          if (secs.sections.length > 0) setSelectedSecId(secs.sections[0].id);
        }
      } catch (e) { setErr(String(e)); }
    })();
  }, [subId]);

  useEffect(() => {
    if (mode !== "layout" || pages.length === 0) return;
    (async () => {
      try {
        const r = await getPageBoxes(subId, pageNo);
        setBoxes(r.boxes);
        setPageDims({ w: r.w_px, h: r.h_px });
      } catch (e) { setErr(String(e)); }
    })();
  }, [subId, pageNo, mode, pages.length]);

  useEffect(() => {
    if (mode !== "sections" || selectedSecId === null) return;
    (async () => {
      try {
        const s = await getSection(subId, selectedSecId);
        setSection(s);
      } catch (e) { setErr(String(e)); }
    })();
  }, [subId, selectedSecId, mode]);

  const selectedBoxes = useMemo(() => boxes.filter((b) => selectedBoxIds.has(b.id)), [boxes, selectedBoxIds]);
  const selectedText = useMemo(() => selectedBoxes.map((b) => b.text).filter(Boolean).join("\n\n"), [selectedBoxes]);
  const currentPage = pages.find((p) => p.page === pageNo);

  function toggleBox(id: number) {
    setSelectedBoxIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  async function nerOnSelected() {
    if (!selectedText.trim()) { setErr("No boxes selected."); return; }
    setBusy("Running NER…");
    setErr(null);
    try {
      const res = await runNer(subId, { text: selectedText, preset });
      setEntityResults(res.entities);
    } catch (e) { setErr(String(e)); }
    finally { setBusy(null); }
  }

  async function nerOnSection() {
    if (!section) return;
    setBusy("Running NER…");
    setErr(null);
    try {
      const res = await runNer(subId, { text: section.text || "", preset });
      setEntityResults(res.entities);
    } catch (e) { setErr(String(e)); }
    finally { setBusy(null); }
  }

  return (
    <section>
      <div className="page-header">
        <div className="crumbs">
          <Link to="/upload">Submissions</Link> / #{subId} / <Link to={`/review/${subId}`}>Review</Link> / Inspect
        </div>
        <div className="row">
          <h1>Inspect document</h1>
          <div className="tabs">
            <button className={mode === "layout" ? "active" : ""} onClick={() => setMode("layout")} disabled={!hasLayout}>Layout</button>
            <button className={mode === "sections" ? "active" : ""} onClick={() => setMode("sections")}>Sections</button>
          </div>
        </div>
      </div>

      <div className="card">
        <p className="muted small">
          For drilling into specific regions when something looks wrong on the scorecard.
          Select boxes (or a section), pick a label preset, run NER, and see what the model sees.
          Nothing here is auto-saved — this is a probe, not the main workflow.
        </p>
        {busy && <p className="muted loading">{busy}</p>}
        {err && <p className="error">{err}</p>}
      </div>

      {mode === "layout" && hasLayout && (
        <div className="card">
          <div className="page-controls">
            <button className="ghost" onClick={() => setPageNo((p) => Math.max(1, p - 1))} disabled={pageNo <= 1}>← Prev</button>
            <span className="muted">
              Page <input type="number" min={1} max={pages.length} value={pageNo}
                onChange={(e) => setPageNo(Math.max(1, Math.min(pages.length, Number(e.target.value))))} /> of {pages.length}
            </span>
            <button className="ghost" onClick={() => setPageNo((p) => Math.min(pages.length, p + 1))} disabled={pageNo >= pages.length}>Next →</button>
            <span className="muted">· {boxes.length} boxes on page</span>
            <span className="spacer" />
            <button className="ghost" onClick={() => setSelectedBoxIds(new Set())} disabled={selectedBoxIds.size === 0}>
              Clear ({selectedBoxIds.size})
            </button>
          </div>
          <div className="layout-grid">
            <div className="page-canvas-wrap" style={currentPage ? { aspectRatio: `${currentPage.w_px} / ${currentPage.h_px}` } : undefined}>
              {currentPage && (
                <>
                  <img src={pageImageUrl(subId, pageNo)} draggable={false} />
                  <div className="overlay-layer">
                    {boxes.map((b) => {
                      const sel = selectedBoxIds.has(b.id);
                      const color = CATEGORY_COLOR[b.category] || CATEGORY_COLOR.other;
                      return (
                        <div
                          key={b.id}
                          className={`box ${sel ? "selected" : ""}`}
                          style={{
                            left: `${(b.bbox.x / currentPage.w_px) * 100}%`,
                            top: `${(b.bbox.y / currentPage.h_px) * 100}%`,
                            width: `${(b.bbox.w / currentPage.w_px) * 100}%`,
                            height: `${(b.bbox.h / currentPage.h_px) * 100}%`,
                            borderColor: color,
                            background: sel ? `${color}33` : "transparent",
                          }}
                          title={`${b.label}: ${b.text.slice(0, 120)}`}
                          onClick={() => toggleBox(b.id)}
                        />
                      );
                    })}
                  </div>
                </>
              )}
            </div>
            <aside className="layout-side">
              <h4>Selected ({selectedBoxIds.size})</h4>
              {selectedBoxes.length === 0 ? <p className="muted small">Click boxes on the page.</p> : (
                <ul className="selected-list">
                  {selectedBoxes.map((b) => (
                    <li key={b.id}>
                      <span className="badge" style={{ background: CATEGORY_COLOR[b.category] }}>{b.label}</span>
                      <span>{b.text.slice(0, 80)}{b.text.length > 80 ? "…" : ""}</span>
                    </li>
                  ))}
                </ul>
              )}
              <div className="ner-controls">
                <select value={preset} onChange={(e) => setPreset(e.target.value)}>
                  {Object.keys(presets).map((p) => (
                    <option key={p} value={p}>{p} · {Object.keys(presets[p]).length} labels</option>
                  ))}
                </select>
                <button className="primary" onClick={nerOnSelected} disabled={busy !== null || selectedBoxes.length === 0}>
                  Run NER →
                </button>
              </div>
              {entityResults.length > 0 && (
                <div className="ner-result">
                  <h5>{entityResults.length} entities</h5>
                  <table>
                    <tbody>
                      {entityResults.map((e, i) => (
                        <tr key={i}><td><code>{e.label}</code></td><td>{e.text}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </aside>
          </div>
        </div>
      )}

      {mode === "sections" && (
        <div className="card">
          <div className="explorer">
            <aside className="sections-list">
              <ul>
                {sections.map((s) => (
                  <li key={s.id} className={selectedSecId === s.id ? "selected" : ""} onClick={() => setSelectedSecId(s.id)}
                      style={{ paddingLeft: 4 + (s.level || 0) * 12 }}>
                    <span className="heading">{s.heading || `(${s.label})`}</span>
                    <span className="meta">{s.char_count} ch{s.page_start ? ` · p${s.page_start}` : ""}</span>
                  </li>
                ))}
              </ul>
            </aside>
            <main className="section-detail">
              {!section ? <p className="muted">Pick a section.</p> : (
                <>
                  <h3>{section.heading}</h3>
                  <div className="ner-controls">
                    <select value={preset} onChange={(e) => setPreset(e.target.value)}>
                      {Object.keys(presets).map((p) => (<option key={p} value={p}>{p}</option>))}
                    </select>
                    <button className="primary" onClick={nerOnSection} disabled={busy !== null}>Run NER →</button>
                  </div>
                  <pre className="section-text">{section.text}</pre>
                  {entityResults.length > 0 && (
                    <table>
                      <tbody>
                        {entityResults.map((e, i) => (
                          <tr key={i}><td><code>{e.label}</code></td><td>{e.text}</td></tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </>
              )}
            </main>
          </div>
        </div>
      )}
    </section>
  );
}
