# Metadata Gap Fixer

Local pipeline that turns a journal article PDF or DOCX into a Crossref-ready
DOI submission XML. Built around four open / pluggable components:

| Stage | What | Why |
|---|---|---|
| Parse | [Docling](https://github.com/docling-project/docling) (`docling-serve` Docker) | Layout-aware PDF/DOCX → structured JSON + markdown |
| Extract | [GLiNER2](https://github.com/fastino-ai/GLiNER2) (`fastino/gliner2-large-v1`) | Schema-driven NER for authors, affiliations, ORCIDs, DOIs, funders, grants |
| Reconcile | OpenAI-compatible LLM with tool-calling | Disambiguate authors / affiliations / journals / references via live enrichers |
| Enrich | ORCID, ROR, OpenAlex, Crossref REST | Authoritative IDs (ORCID iD, ROR, DOI, ISSN, Funder Registry) |
| Emit | Jinja2 → Crossref schema 5.3.1 | `journal_article` deposit XML, well-formedness checked |

GUI is a small Vite + React app for upload, review, edit, and download.

## Quick start

```bash
git clone <this-repo>
cd metadata_gapfixer
cp .env.example .env
# edit .env — set OPENAI_API_KEY (and optionally swap OPENAI_BASE_URL / OPENAI_MODEL
# to point at any OpenAI-compatible endpoint) and CONTACT_EMAIL
docker compose up -d --build
```

Then open <http://localhost:3000>.

Services on first start:

- `mgf-docling` — pulls the official `docling-serve` image
- `mgf-backend` — installs deps and downloads GLiNER2 weights into `./data/hf-cache`
  (~1–2 GB, persists across restarts)
- `mgf-frontend` — Vite dev server

The GLiNER2 download dominates first-boot time. Tail logs with
`docker compose logs -f backend`.

## Pipeline

1. **Upload** a PDF/DOCX in the GUI (`POST /submissions`).
2. Backend background task:
   - Sends the file to `docling-serve` `/v1/convert/file` → JSON + markdown.
   - Slices the markdown into header / abstract / body / references / acknowledgements zones.
   - Runs GLiNER2 with a per-zone schema to extract candidate entities.
   - Hands the Docling output + entities to the LLM agent. The agent tool-calls:
     - `orcid_search` (ORCID public API)
     - `ror_search` (ROR API)
     - `openalex_author_lookup` / `openalex_work_lookup` / `openalex_funder_lookup`
     - `crossref_doi_lookup` / `crossref_work_search` / `crossref_journal_search`
   - Returns a strict `JournalArticleMetadata` JSON.
3. **Review** in the GUI: edit the JSON, then click **Generate Crossref XML**.
4. **Download** the Crossref 5.3.1 deposit XML and submit to Crossref via your
   normal channel (web deposit form, REST API with your member credentials, or OJS).

## API surface (backend)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/submissions` | Multipart upload, kicks off pipeline |
| `GET`  | `/submissions` | List submissions + status |
| `GET`  | `/submissions/{id}` | Status of one submission |
| `GET`  | `/submissions/{id}/entities` | Raw GLiNER2 output |
| `GET`  | `/submissions/{id}/metadata` | Reconciled metadata JSON |
| `PUT`  | `/submissions/{id}/metadata` | Save edited metadata |
| `POST` | `/submissions/{id}/xml` | Build Crossref XML from current metadata |
| `GET`  | `/submissions/{id}/xml` | Download generated XML |

## Configuration

All knobs live in `.env` (loaded by Compose). Key ones:

| Var | Default | Notes |
|---|---|---|
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint |
| `OPENAI_API_KEY` | *(required)* | Provider API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | Use a tool-calling-capable model |
| `GLINER_MODEL` | `fastino/gliner2-large-v1` | Swap to `fastino/gliner2-base-v1` (205M) on small hosts |
| `CONTACT_EMAIL` | `anonymous@example.org` | Identifies you to ORCID/ROR/OpenAlex/Crossref polite pools |

See `.env.example` for working snippets per provider (OpenRouter, Anthropic
compat, Groq, Ollama, LiteLLM).

## Storage

For now, **everything is on disk** under `./data/`:

```
data/
├── uploads/          # raw uploaded PDFs/DOCXs
├── outputs/          # docling json, entities, metadata json, crossref xml
├── cache/http/       # diskcache for ORCID/ROR/OpenAlex/Crossref responses
├── hf-cache/         # huggingface model cache (GLiNER2 weights)
└── mgf.db            # SQLite — submissions table only
```

**To swap storage** (S3, GCS, Azure Blob, NFS, etc.):

- Bind-mount or symlink `./data/uploads` and `./data/outputs` to your network
  storage (works for any POSIX-mountable backend, e.g. `s3fs`, `gcsfuse`,
  `azure-storage-fuse`, NFS, SMB).
- Or replace the file I/O calls in `backend/app/pipeline.py` and
  `backend/app/routes/submissions.py` with calls to your SDK of choice.
- Replace the SQLite engine in `backend/app/db.py` with a Postgres /
  MySQL URL — `sqlmodel` already speaks both.

We deliberately did *not* embed an S3 / cloud-specific client so the project
stays portable. Pick what fits your infrastructure.

## Layout

```
metadata_gapfixer/
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── templates/journal_article.xml.j2
│   └── app/
│       ├── main.py                # FastAPI app
│       ├── config.py              # env settings
│       ├── db.py                  # SQLite + Submission model
│       ├── models.py              # Pydantic JournalArticleMetadata
│       ├── pipeline.py            # parse → extract → enrich orchestrator
│       ├── routes/
│       │   ├── health.py
│       │   └── submissions.py
│       └── services/
│           ├── docling_client.py  # docling-serve HTTP client
│           ├── ner.py             # GLiNER2 zone-based extraction
│           ├── llm_agent.py       # OpenAI-compat tool-calling agent
│           ├── crossref_xml.py    # Jinja2 → Crossref 5.3.1 XML
│           └── enrichers/
│               ├── orcid.py
│               ├── ror.py
│               ├── openalex.py
│               └── crossref.py
└── frontend/
    ├── Dockerfile
    ├── package.json
    └── src/
        ├── App.tsx
        ├── api.ts
        ├── styles.css
        └── pages/{Upload,Review}.tsx
```

## Notes / caveats

- This generates a Crossref **journal article** deposit only (no books,
  conference papers, datasets, or preprints).
- The agent never invents identifiers — it only writes ORCIDs / DOIs / ROR IDs
  it found via the enricher tools. Anything it could not resolve is left null
  with a note in `confidence_notes`.
- The XML is well-formedness-checked but not XSD-validated. Run Crossref's
  XML validator (or `xmllint --schema crossref5.3.1.xsd ...`) before deposit.
- LLM provider must support OpenAI tool-calling format — most major providers
  do; check before swapping.
