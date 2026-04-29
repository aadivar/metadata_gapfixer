import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  buildXml,
  Entity,
  getLabelPresets,
  getMetadata,
  getPages,
  getPageBoxes,
  getSection,
  getSections,
  LayoutBox,
  PageInfo,
  pageImageUrl,
  putMetadata,
  reconcile,
  runNer,
  saveEntitiesSnapshot,
  Section,
  xmlDownloadUrl,
} from "../api";

type PerSection = Record<string, Entity[]>;
type Mode = "layout" | "sections";

const CATEGORY_COLOR: Record<string, string> = {
  header: "#6aa9ff",
  text: "#aaaaaa",
  table: "#ffb86a",
  figure: "#ff6aa9",
  furniture: "#555555",
  code: "#6aff9b",
  formula: "#bb88ff",
  other: "#888888",
};

const PRESET_HINTS: Record<string, string> = {
  preamble: "header",
  title: "header",
  abstract: "abstract",
  funding: "funding",
  acknowledgements: "funding",
  references: "references",
  bibliography: "references",
};

function suggestPreset(label: string, heading?: string): string {
  const h = (heading || "").toLowerCase();
  for (const key of Object.keys(PRESET_HINTS)) {
    if (h.includes(key) || label === key) return PRESET_HINTS[key];
  }
  return "header";
}

export default function Review() {
  const { id } = useParams();
  const subId = Number(id);

  const [mode, setMode] = useState<Mode>("layout");
  const [presets, setPresets] = useState<Record<string, Record<string, string>>>({});
  const [preset, setPreset] = useState<string>("header");
  const [perKey, setPerKey] = useState<PerSection>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [metaText, setMetaText] = useState<string>("");
  const [xmlBuilt, setXmlBuilt] = useState(false);

  // Layout-mode state
  const [pages, setPages] = useState<PageInfo[]>([]);
  const [pageNo, setPageNo] = useState<number>(1);
  const [boxes, setBoxes] = useState<LayoutBox[]>([]);
  const [pageDims, setPageDims] = useState<{ w: number; h: number }>({ w: 1, h: 1 });
  const [selectedBoxIds, setSelectedBoxIds] = useState<Set<number>>(new Set());
  const [hasLayout, setHasLayout] = useState<boolean>(true);

  // Sections-mode state
  const [sections, setSections] = useState<Section[]>([]);
  const [selectedSecId, setSelectedSecId] = useState<number | null>(null);
  const [section, setSection] = useState<Section | null>(null);

  // Initial load
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
      } catch (e) {
        setErr(String(e));
      }
    })();
  }, [subId]);

  // Layout: load boxes for selected page
  useEffect(() => {
    if (mode !== "layout" || pages.length === 0) return;
    (async () => {
      try {
        const r = await getPageBoxes(subId, pageNo);
        setBoxes(r.boxes);
        setPageDims({ w: r.w_px, h: r.h_px });
      } catch (e) {
        setErr(String(e));
      }
    })();
  }, [subId, pageNo, mode, pages.length]);

  // Sections: when selectedSecId changes, fetch full section text
  useEffect(() => {
    if (mode !== "sections" || selectedSecId === null) return;
    (async () => {
      try {
        const s = await getSection(subId, selectedSecId);
        setSection(s);
        setPreset(suggestPreset(s.label, s.heading));
      } catch (e) {
        setErr(String(e));
      }
    })();
  }, [subId, selectedSecId, mode]);

  const selectedBoxes = useMemo(
    () => boxes.filter((b) => selectedBoxIds.has(b.id)),
    [boxes, selectedBoxIds]
  );

  const selectedText = useMemo(
    () => selectedBoxes.map((b) => b.text).filter(Boolean).join("\n\n"),
    [selectedBoxes]
  );

  const totalEntities = useMemo(
    () => Object.values(perKey).reduce((n, arr) => n + arr.length, 0),
    [perKey]
  );

  function toggleBox(id: number) {
    setSelectedBoxIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function clearSelection() {
    setSelectedBoxIds(new Set());
  }

  async function nerOnSelected() {
    if (!selectedText.trim()) {
      setErr("No boxes selected.");
      return;
    }
    setBusy("Running NER…");
    setErr(null);
    try {
      const res = await runNer(subId, { text: selectedText, preset });
      const key = `p${pageNo}_b${[...selectedBoxIds].sort().join("-")}`;
      setPerKey((prev) => ({ ...prev, [key]: res.entities }));
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  async function nerOnSection() {
    if (!section) return;
    setBusy("Running NER…");
    setErr(null);
    try {
      const res = await runNer(subId, { text: section.text || "", preset });
      setPerKey((prev) => ({ ...prev, [`s${section.id}`]: res.entities }));
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  async function doSaveSnapshot() {
    setBusy("Saving entities…");
    setErr(null);
    try {
      await saveEntitiesSnapshot(subId, perKey);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  async function doReconcile() {
    setBusy("Reconciling with LLM (tool-calling)…");
    setErr(null);
    try {
      await saveEntitiesSnapshot(subId, perKey);
      await reconcile(subId);
      const m = await getMetadata(subId);
      setMetaText(JSON.stringify(m, null, 2));
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  async function saveMetadata() {
    setBusy("Saving metadata…");
    try {
      await putMetadata(subId, JSON.parse(metaText));
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  async function generateXml() {
    setBusy("Generating XML…");
    try {
      await buildXml(subId);
      setXmlBuilt(true);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  const currentPage = pages.find((p) => p.page === pageNo);

  return (
    <section>
      <div className="card">
        <div className="row">
          <h2>Submission #{subId}</h2>
          <Link to="/upload">← Back</Link>
        </div>
        <p className="muted">
          {totalEntities} entities extracted across {Object.keys(perKey).length} runs
        </p>
        <div className="tabs">
          <button
            className={mode === "layout" ? "active" : ""}
            onClick={() => setMode("layout")}
            disabled={!hasLayout}
            title={hasLayout ? "" : "Layout view only available for PDFs"}
          >
            Layout (click to select)
          </button>
          <button className={mode === "sections" ? "active" : ""} onClick={() => setMode("sections")}>
            Sections
          </button>
        </div>
        {busy && <p className="muted">{busy}</p>}
        {err && <p className="error">{err}</p>}
      </div>

      {mode === "layout" && hasLayout && (
        <div className="card">
          <div className="page-controls">
            <button onClick={() => setPageNo((p) => Math.max(1, p - 1))} disabled={pageNo <= 1}>
              ← Prev
            </button>
            <span>
              Page{" "}
              <input
                type="number"
                min={1}
                max={pages.length}
                value={pageNo}
                onChange={(e) => setPageNo(Math.max(1, Math.min(pages.length, Number(e.target.value))))}
                style={{ width: 60 }}
              />{" "}
              / {pages.length}
            </span>
            <button onClick={() => setPageNo((p) => Math.min(pages.length, p + 1))} disabled={pageNo >= pages.length}>
              Next →
            </button>
            <span className="muted">{boxes.length} boxes on this page</span>
            <span className="spacer" />
            <button onClick={clearSelection} disabled={selectedBoxIds.size === 0}>
              Clear selection ({selectedBoxIds.size})
            </button>
          </div>

          <div className="layout-grid">
            <div
              className="page-canvas-wrap"
              style={currentPage ? { aspectRatio: `${currentPage.w_px} / ${currentPage.h_px}` } : undefined}
            >
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
              {selectedBoxes.length === 0 ? (
                <p className="muted">Click boxes on the page to add them.</p>
              ) : (
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
                <label>
                  Preset:{" "}
                  <select value={preset} onChange={(e) => setPreset(e.target.value)}>
                    {Object.keys(presets).map((p) => (
                      <option key={p} value={p}>
                        {p} ({Object.keys(presets[p]).length} labels)
                      </option>
                    ))}
                  </select>
                </label>
                <button onClick={nerOnSelected} disabled={busy !== null || selectedBoxes.length === 0}>
                  Run NER on selection
                </button>
              </div>

              {selectedText && (
                <details>
                  <summary>Combined text ({selectedText.length} ch)</summary>
                  <pre className="section-text">{selectedText}</pre>
                </details>
              )}

              {Object.entries(perKey)
                .filter(([k]) => k.startsWith(`p${pageNo}_`))
                .map(([k, ents]) => (
                  <div key={k} className="ner-result">
                    <h5>{k} — {ents.length} entities</h5>
                    <table>
                      <tbody>
                        {ents.map((e, i) => (
                          <tr key={i}>
                            <td><code>{e.label}</code></td>
                            <td>{e.text}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ))}
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
                  <li
                    key={s.id}
                    className={selectedSecId === s.id ? "selected" : ""}
                    onClick={() => setSelectedSecId(s.id)}
                    style={{ paddingLeft: 4 + (s.level || 0) * 12 }}
                  >
                    <span className="heading">{s.heading || `(${s.label})`}</span>
                    <span className="meta">
                      {s.char_count} ch
                      {s.page_start ? ` · p${s.page_start}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </aside>
            <main className="section-detail">
              {!section ? (
                <p className="muted">Pick a section.</p>
              ) : (
                <>
                  <h3>{section.heading}</h3>
                  <div className="ner-controls">
                    <label>
                      Preset:{" "}
                      <select value={preset} onChange={(e) => setPreset(e.target.value)}>
                        {Object.keys(presets).map((p) => (
                          <option key={p} value={p}>{p}</option>
                        ))}
                      </select>
                    </label>
                    <button onClick={nerOnSection} disabled={busy !== null}>Run NER</button>
                  </div>
                  <pre className="section-text">{section.text}</pre>
                  {perKey[`s${section.id}`] && (
                    <table>
                      <tbody>
                        {perKey[`s${section.id}`].map((e, i) => (
                          <tr key={i}>
                            <td><code>{e.label}</code></td>
                            <td>{e.text}</td>
                          </tr>
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

      <div className="card">
        <h3>Reconcile + emit XML</h3>
        <p className="muted">
          When happy with the per-region NER, save the snapshot and run the LLM agent.
          The agent uses ORCID / ROR / OpenAlex / Crossref to disambiguate, then emits a
          Crossref-ready metadata record.
        </p>
        <div className="actions">
          <button onClick={doSaveSnapshot} disabled={busy !== null || totalEntities === 0}>
            Save entities snapshot
          </button>
          <button onClick={doReconcile} disabled={busy !== null || totalEntities === 0}>
            Reconcile with LLM
          </button>
        </div>
        {metaText && (
          <>
            <h4>Metadata</h4>
            <textarea
              className="json-editor"
              value={metaText}
              onChange={(e) => setMetaText(e.target.value)}
              spellCheck={false}
            />
            <div className="actions">
              <button onClick={saveMetadata}>Save metadata</button>
              <button onClick={generateXml}>Generate Crossref XML</button>
              {xmlBuilt && (
                <a href={xmlDownloadUrl(subId)} target="_blank" rel="noreferrer">
                  Download XML
                </a>
              )}
            </div>
          </>
        )}
      </div>
    </section>
  );
}
