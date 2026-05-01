import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { deleteSubmission, listSubmissions, uploadFile, Submission } from "../api";
import { ArrowRightIcon, TrashIcon, UploadIcon } from "../icons";

const TERMINAL = new Set(["parsed", "ready", "error"]);

export default function Upload() {
  const [submissions, setSubmissions] = useState<Submission[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [drag, setDrag] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  async function refresh() {
    try {
      setSubmissions(await listSubmissions());
    } catch (e) {
      setErr(String(e));
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(() => {
      if (submissions.some((s) => !TERMINAL.has(s.status))) refresh();
    }, 3000);
    return () => clearInterval(t);
  }, [submissions]);

  async function handleFile(file: File) {
    setBusy(true);
    setErr(null);
    try {
      await uploadFile(file);
      await refresh();
    } catch (er) {
      setErr(String(er));
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  async function handleDelete(s: Submission) {
    if (!confirm(`Delete submission #${s.id} (${s.filename}) and all its generated files?`)) return;
    setErr(null);
    try {
      await deleteSubmission(s.id);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  return (
    <section>
      <div className="page-header">
        <div className="crumbs">Workspace</div>
        <div className="row">
          <h1>Submissions</h1>
        </div>
      </div>

      <div className="card">
        <h2 className="card-title">Upload a paper</h2>
        <p className="card-subtitle">
          PDF or DOCX. Docling parses layout · GLiNER2 extracts entities by region ·
          an OpenAI-compatible agent reconciles them against ORCID, ROR, OpenAlex, and Crossref.
        </p>

        <label
          className={`dropzone ${drag ? "drag" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            const f = e.dataTransfer.files?.[0];
            if (f) handleFile(f);
          }}
        >
          <div className="dropzone-icon">
            <UploadIcon size={20} />
          </div>
          <h3>{busy ? "Uploading…" : "Drop a PDF or DOCX here"}</h3>
          <p>or click to choose · max one file at a time</p>
          <input
            ref={inputRef}
            type="file"
            accept=".pdf,.docx,.doc"
            disabled={busy}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) handleFile(f);
            }}
          />
        </label>

        {err && <p className="error" style={{ marginTop: 14 }}>{err}</p>}
      </div>

      <div className="card">
        <div className="row" style={{ marginBottom: 14 }}>
          <h2 className="card-title" style={{ margin: 0 }}>Recent submissions</h2>
          <span className="muted">{submissions.length} total</span>
        </div>

        {submissions.length === 0 ? (
          <div className="empty-state">
            <h3>No submissions yet</h3>
            <p>Upload your first paper to begin.</p>
          </div>
        ) : (
          <div className="submission-list">
            {submissions.map((s) => (
              <div key={s.id} className="submission-row fade-in">
                <span className="submission-id">#{s.id}</span>
                <div>
                  <div className="submission-name">{s.filename}</div>
                  {s.error && <div className="submission-meta error small">{s.error}</div>}
                </div>
                <span className={`status status-${s.status}`}>{s.status}</span>
                <div className="cluster" style={{ gap: 6 }}>
                  {(s.status === "parsed" || s.status === "ready") ? (
                    <Link to={`/review/${s.id}`} className="btn primary">
                      Open <ArrowRightIcon size={14} />
                    </Link>
                  ) : s.status === "error" ? (
                    <span className="muted small">—</span>
                  ) : (
                    <span className="muted small loading">working</span>
                  )}
                  <button
                    className="ghost"
                    onClick={() => handleDelete(s)}
                    title="Delete submission and its files"
                    aria-label={`Delete submission ${s.id}`}
                  >
                    <TrashIcon size={14} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
