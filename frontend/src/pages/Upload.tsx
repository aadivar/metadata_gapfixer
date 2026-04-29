import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { listSubmissions, uploadFile, Submission } from "../api";

const TERMINAL = new Set(["parsed", "ready", "error"]);

export default function Upload() {
  const [submissions, setSubmissions] = useState<Submission[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

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

  async function onUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setErr(null);
    try {
      await uploadFile(file);
      await refresh();
    } catch (er) {
      setErr(String(er));
    } finally {
      setBusy(false);
      e.target.value = "";
    }
  }

  return (
    <section>
      <div className="card">
        <h2>Upload a paper</h2>
        <p className="muted">PDF or DOCX. Docling will parse it, GLiNER2 will extract entities, the LLM agent will reconcile against ORCID/ROR/OpenAlex/Crossref.</p>
        <input type="file" accept=".pdf,.docx,.doc" onChange={onUpload} disabled={busy} />
        {busy && <p>Uploading…</p>}
        {err && <p className="error">{err}</p>}
      </div>

      <div className="card">
        <h2>Submissions</h2>
        {submissions.length === 0 && <p className="muted">No submissions yet.</p>}
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>File</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {submissions.map((s) => (
              <tr key={s.id}>
                <td>{s.id}</td>
                <td>{s.filename}</td>
                <td>
                  <span className={`status status-${s.status}`}>{s.status}</span>
                  {s.error && <div className="error small">{s.error}</div>}
                </td>
                <td>
                  {(s.status === "parsed" || s.status === "ready") && (
                    <Link to={`/review/${s.id}`}>Open →</Link>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
