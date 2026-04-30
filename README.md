# Metadata Gap Fixer

A local diagnostic tool that turns a journal article PDF/DOCX into a
**metadata completeness scorecard** plus a Crossref-ready DOI submission XML.

The pitch: editors and publishers don't always know what their record is
missing. Most existing deposit forms accept whatever you give them and
silently produce thin metadata. This tool runs the document through layout
analysis + deterministic extraction + opt-in AI enrichment and shows you,
in one screen, exactly which integrity-relevant fields are present, which
can be auto-filled, and which need editorial input — with running cost in
USD for every paid LLM call.

```
┌────────────────────────────────────────────────────────────────────┐
│  Metadata completeness                                             │
│   ┌────┐    Depositable, but the record is invisible to most       │
│   │ 47 │    cross-system linking.                                  │
│   │/100│                                                           │
│   └────┘                                                           │
│   T0  Depositable        ████████  100%   Crossref required        │
│   T1  Discoverable       ████░░░░   42%   Crossref recommended     │
│   T2  Linkable           █░░░░░░░   13%   Nexus benchmarks         │
│   T3  Integrity-grade    ░░░░░░░░    0%   Crossref+DataCite guide  │
│                                                                    │
│   [⚡ Auto-fix everything (12 high-impact)]   [Generate XML]       │
└────────────────────────────────────────────────────────────────────┘
```

---

## Architecture

Five layers, each layered on the one below:

| Layer | What | Cost | Trigger |
|---|---|---|---|
| **L0 · Docling layout** | PDF/DOCX → structured JSON + per-element bboxes + page renders | $0 | Always (parse stage) |
| **L1 · Deterministic factsheet** | Regex sweep (DOI / ORCID / ROR / ISSN / arXiv / license / grant patterns / preprint DOIs) + PDF /Info + header parser (authors + affiliation marker map) + boilerplate anchor matching (funding / CoI / data availability / ethics) | $0 | Always (parse stage) |
| **L2 · Free enricher APIs** | ORCID public API · ROR v2 · OpenAlex (works/authors/funders) · Crossref REST. Single-candidate hits filled directly; multi-candidate hits stored as `needs_pick` with the candidate list attached. | $0 | Auto-fix (publisher click) |
| **L3 · LLM picker** | When the editor explicitly opts in, runs ONE structured-output call per ambiguous field that picks among the API-returned candidates with reasoning + confidence. | ~$0.0002/call | Per-field "✨ Adjudicate with AI" or bulk |
| **L4 · LLM structurer** | Higher-leverage: takes a raw content region + (optionally) seed candidates and returns a clean structured record. Four named tasks: `structure_authors`, `structure_references`, `structure_funding`, `structure_credit`. | ~$0.0003 – $0.009/call | Per-section "🧠 Structure with AI" |

Cost rule: **the LLM is never called automatically**. Every paid call is the
publisher's deliberate choice and is recorded in a per-submission USD ledger.

For a typical paper, full premium processing tops out around **$0.025**, well
under the configurable $1 ceiling. Standard auto-fix (no LLM) is **$0**.

---

## Tier rubric

The composite score is weighted across four published tiers — each one
references a real spec or guideline so editors can map the score to
external benchmarks they already know:

| Tier | Anchor | What it represents |
|---|---|---|
| **T0 · Depositable** | [Crossref schema 5.4.0 required](https://data.crossref.org/reports/help/schema_doc/5.4.0/index.html) | Without these, you literally cannot deposit. |
| **T1 · Discoverable** | Crossref schema recommended | What makes the record usable to indexers. |
| **T2 · Linkable** | [Crossref Participation Reports / Nexus](https://www.crossref.org/members/prep/) | What enables ORCID, ROR, Funder Registry, citation-graph linking. |
| **T3 · Integrity-grade** | [Why metadata matters for research integrity (Crossref + DataCite, 2026)](https://zenodo.org/records/19695957) | Preprint relations, Crossmark, CRediT roles, data/code availability. |

Field weights and tier weights live in `backend/app/services/scoring.py` and
are easy to customise per publisher.

---

## Pipeline

1. **Upload** PDF/DOCX → `POST /submissions`. Status flows
   `uploaded → parsing → parsed`.
2. **Parse** runs once per upload, in the background:
   - `docling-serve /v1/convert/file` → structured JSON + markdown.
   - PyMuPDF renders each page to PNG at 150 DPI; bboxes are mapped from
     PDF points → image pixels for the layout overlay.
   - `factsheet.py` runs the deterministic L1 extraction.
3. **Score** is computed on demand from the factsheet + saved metadata.
   Returned by `GET /submissions/{id}/score`.
4. **Auto-fix** (free, deterministic):
   - `POST /submissions/{id}/autofix` `{action}` — one fixer
   - `POST /submissions/{id}/autofix/all` — every high-impact fixer
5. **Pick / adjudicate** ambiguous candidates:
   - `POST /submissions/{id}/pick` `{field_path, chosen_id}` — manual, free
   - `POST /submissions/{id}/disambiguate/estimate` `{field_path?}` — preview USD cost
   - `POST /submissions/{id}/disambiguate` `{field_path? | field_paths?}` — opt-in LLM pick
6. **Structure messy regions** with LLM:
   - `GET  /submissions/structure/estimate` — global cost preview
   - `POST /submissions/{id}/structure/{task}/estimate`
   - `POST /submissions/{id}/structure/{task}` — `structure_authors` |
     `structure_references` | `structure_funding` | `structure_credit`
7. **Generate XML** — `POST /submissions/{id}/xml` builds, `GET /submissions/{id}/xml` downloads.

---

## API surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/submissions` | Multipart upload, kicks off parse |
| `GET`  | `/submissions` | List submissions + status |
| `GET`  | `/submissions/{id}` | Status of one submission |
| `DELETE` | `/submissions/{id}` | Remove submission + all generated files |
| `GET`  | `/submissions/{id}/factsheet` | Deterministic L1 output |
| `GET`  | `/submissions/{id}/score` | Tier rubric + gap buckets |
| `GET`  | `/submissions/{id}/sections` | Layout-derived sections |
| `GET`  | `/submissions/{id}/sections/{n}` | One section with full text |
| `GET`  | `/submissions/{id}/markdown` | Full Docling markdown |
| `GET`  | `/submissions/{id}/pages` | Per-page render dimensions |
| `GET`  | `/submissions/{id}/pages/{n}/image` | Page PNG |
| `GET`  | `/submissions/{id}/pages/{n}/boxes` | Per-page Docling bboxes + text |
| `GET`  | `/submissions/{id}/provenance` | Per-field source / confidence / reasoning |
| `GET`  | `/submissions/{id}/cost` | LLM cost ledger for this submission |
| `POST` | `/submissions/{id}/autofix` | One deterministic fixer (free) |
| `POST` | `/submissions/{id}/autofix/all` | Run every high-impact fixer (free) |
| `POST` | `/submissions/{id}/pick` | Manual editor pick from candidate list (free) |
| `POST` | `/submissions/{id}/disambiguate/estimate` | Preview LLM cost (free) |
| `POST` | `/submissions/{id}/disambiguate` | Opt-in LLM picker (~$0.0002/call) |
| `GET`  | `/submissions/structure/estimate` | All structurer tasks' costs |
| `POST` | `/submissions/{id}/structure/{task}/estimate` | Per-task cost preview |
| `POST` | `/submissions/{id}/structure/{task}` | Opt-in LLM structurer |
| `POST` | `/submissions/{id}/ner` | On-demand GLiNER2 NER on a text block |
| `GET`  | `/submissions/presets/labels` | NER label presets per zone |
| `GET`  | `/submissions/{id}/metadata` | Saved metadata JSON |
| `PUT`  | `/submissions/{id}/metadata` | Save edited metadata |
| `POST` | `/submissions/{id}/reconcile` | Legacy tool-calling agent (premium, single button) |
| `POST` | `/submissions/{id}/xml` | Build Crossref XML |
| `GET`  | `/submissions/{id}/xml` | Download XML |

---

## GUI

Vite + React + react-router. Two pages:

- **Submissions** (`/upload`) — drag-and-drop dropzone, status pills with
  animated dots for in-flight states, per-row delete button.
- **Review** (`/review/:id`) — scorecard hero (composite + four tier bars
  + interpretation), three gap buckets (high impact / medium / manual)
  with per-field Fix buttons, "What we found" panel with the factsheet
  contents, optional metadata JSON editor, optional premium reconcile
  button, generate-XML + download.
- **Inspect** (`/inspect/:id`) — demoted layout / sections explorer for
  drilling into specific PDF regions when the scorecard looks wrong.
  Click bboxes on the page render to multi-select, run NER on the
  combined text.

Light/dark theme respects the OS preference and persists to `localStorage`
under `mgf.theme`. Toggle in the sidebar footer.

---

## Quick start

```bash
git clone <this-repo>
cd metadata_gapfixer
cp .env.example .env                # edit OPENAI_API_KEY + CONTACT_EMAIL
docker compose up -d --build
```

Open <http://localhost:3000>.

First boot downloads:
- `ghcr.io/docling-project/docling-serve:latest` (~2 GB)
- GLiNER2 (`fastino/gliner2-large-v1`) into `./data/hf-cache/` (~1.4 GB)

Pre-warm GLiNER2 to avoid the wait on the first NER call:

```bash
docker compose exec backend python -c \
  "from gliner2 import GLiNER2; GLiNER2.from_pretrained('fastino/gliner2-large-v1')"
```

Tail logs with `docker compose logs -f backend`.

---

## Configuration

All knobs live in `.env`. See `.env.example` for working snippets per
provider (OpenAI, OpenRouter, Anthropic-compat, Groq, Ollama, LiteLLM).

| Var | Default | Notes |
|---|---|---|
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint |
| `OPENAI_API_KEY` | *(required)* | Provider API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | Must support `response_format=json_schema` |
| `GLINER_MODEL` | `fastino/gliner2-large-v1` | Use `fastino/gliner2-base-v1` (205M) on small hosts |
| `CONTACT_EMAIL` | `anonymous@example.org` | Sent in User-Agent for ORCID/ROR/OpenAlex/Crossref polite pools |
| `HF_TOKEN` | *(unset)* | Optional — higher rate limits on HuggingFace downloads |

Per-task model routing lives in `backend/app/services/llm_router.py`'s
`TASK_CONFIG` dict. Override individual tasks via env if you want
`structure_references` on a flagship model and the rest on mini, etc.

---

## Storage

For now, **everything is on disk** under `./data/`:

```
data/
├── uploads/                      # raw uploaded PDFs/DOCXs
├── outputs/
│   ├── {id}_docling.json         # full DoclingDocument
│   ├── {id}_layout.json          # rendered pages + bboxes (PDFs only)
│   ├── {id}_pages/page_NNN.png   # 150 DPI page renders
│   ├── {id}_factsheet.json       # deterministic L1 extraction
│   ├── {id}_metadata.json        # editor-facing metadata + provenance
│   ├── {id}_cost.json            # per-call LLM cost ledger
│   └── {id}_crossref.xml         # generated deposit XML
├── cache/http/                   # diskcache for ORCID/ROR/OpenAlex/Crossref
├── hf-cache/                     # huggingface model weights
└── mgf.db                        # SQLite — submissions table only
```

**To swap storage** (S3, GCS, Azure Blob, NFS, etc.):

- Bind-mount or symlink `./data/uploads` and `./data/outputs` to your
  network storage (works for any POSIX-mountable backend, e.g. `s3fs`,
  `gcsfuse`, `azure-storage-fuse`, NFS, SMB).
- Or replace the file I/O in `backend/app/pipeline.py` and
  `backend/app/routes/submissions.py` with calls to your SDK of choice.
- Replace the SQLite engine in `backend/app/db.py` with a Postgres / MySQL
  URL — SQLModel already speaks both.

We deliberately did *not* embed an S3 / cloud-specific client so the
project stays portable. Pick what fits your infrastructure.

---

## Layout

```
metadata_gapfixer/
├── docker-compose.yml
├── .env.example
├── README.md
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── templates/journal_article.xml.j2     # Crossref 5.3.1 deposit XML
│   └── app/
│       ├── main.py                          # FastAPI app
│       ├── config.py                        # env settings
│       ├── db.py                            # SQLite + Submission model
│       ├── models.py                        # JournalArticleMetadata + provenance
│       ├── pipeline.py                      # parse → factsheet → render pages
│       ├── routes/
│       │   ├── health.py
│       │   └── submissions.py               # all the endpoints
│       └── services/
│           ├── docling_client.py            # docling-serve HTTP client
│           ├── page_render.py               # PyMuPDF → PNG + bbox extraction
│           ├── sections.py                  # layout-aware section walker
│           ├── factsheet.py                 # L1 deterministic extraction
│           ├── ner.py                       # GLiNER2 (Inspect view only)
│           ├── scoring.py                   # tier rubric + composite score
│           ├── autofix.py                   # free deterministic fixers + needs_pick
│           ├── llm_router.py                # per-task models + cost ledger + disambiguate
│           ├── structurers.py               # four LLM structurers (authors/refs/funding/credit)
│           ├── llm_agent.py                 # legacy tool-calling agent (premium)
│           ├── crossref_xml.py              # Jinja2 → XML + lxml well-formedness
│           └── enrichers/
│               ├── _base.py                 # diskcache-backed httpx wrapper
│               ├── orcid.py
│               ├── ror.py
│               ├── openalex.py
│               └── crossref.py
└── frontend/
    ├── Dockerfile
    ├── package.json
    └── src/
        ├── App.tsx                          # sidebar shell + theme toggle
        ├── api.ts                           # typed API client
        ├── theme.ts                         # light/dark + localStorage
        ├── styles.css                       # design tokens, scorecard, dropzone
        └── pages/
            ├── Upload.tsx                   # dropzone + submission list
            ├── Review.tsx                   # scorecard + gap buckets + actions
            └── Inspect.tsx                  # layout overlay + NER probe (demoted)
```

---

## Notes / caveats

- **Journal articles only.** No book chapters, conference papers, datasets,
  preprints (as deposit type). Preprint *relations* on a journal article are
  supported via the `preprint_doi` field.
- **No fabrication.** The LLM is constrained by structured outputs and
  explicit candidate lists. ORCIDs, DOIs, RORs, ISSNs come from APIs or
  PDF text — never made up.
- **XML is well-formedness checked, not XSD-validated.** Run Crossref's
  XML validator (or `xmllint --schema crossref5.3.1.xsd ...`) before
  deposit. The template targets schema 5.3.1; bumping to 5.4.0 is a
  one-file change in `templates/journal_article.xml.j2`.
- **LLM provider** must support OpenAI structured outputs (`response_format`
  with `json_schema`). OpenAI, Anthropic-compat, OpenRouter, Groq, Together,
  and recent Ollama all do. Check before swapping.
- **Apple Silicon torch.** The Dockerfile pins CPU torch; if you want MPS
  acceleration, run the backend natively (outside Docker) since Docker
  cannot reach the M-series GPU.
- **Dependency versions** are current as of April 2026 (FastAPI 0.136,
  OpenAI SDK 2.33, Pydantic 2.13, lxml 6.1, SQLModel 0.0.38, GLiNER2 latest).
