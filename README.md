# Metadata Generator

A local diagnostic tool that turns a journal article PDF/DOCX into a
**Research Nexus completeness scorecard** plus a Crossref-ready DOI
submission XML. It runs the document through layout analysis, deterministic
extraction, free identifier-registry lookups, and opt-in AI enrichment, and
shows you in one screen exactly which integrity-relevant fields are
present, which can be auto-filled, and which need editorial input — with
running cost in USD for every paid LLM call.

A follow-up to the Crossref Research Nexus Score visualised at
**[nexus-score.vercel.app](https://nexus-score.vercel.app/)**. Where the
score tells you *what's missing*, this tool helps you *fix it*.

```
┌────────────────────────────────────────────────────────────────────┐
│  47   RESEARCH NEXUS                                               │
│       Mandatory: 9/9 · Depositable                                 │
│                                                                    │
│   25%  Provenance       █░░░░░░░   13%                             │
│   20%  People           ████░░░░   60%                             │
│   20%  Funding          ░░░░░░░░    0%                             │
│   20%  Access           ███████░   80%                             │
│   15%  Organizations    █████░░░   70%                             │
│                                                                    │
│   [Run automated extraction]  [Run AI enrichment ~$0.025]          │
└────────────────────────────────────────────────────────────────────┘
```

---

## Categorisation: one Mandatory gate + five Research Nexus dimensions

Aligned with the **[Crossref Research Nexus](https://www.crossref.org/research-nexus/)**
framing and the weighting used by **[nexus-score.vercel.app](https://nexus-score.vercel.app/)**.

| Bucket | Weight | What it covers |
|---|---|---|
| **Mandatory** | gate | DOI, title, journal, ISSN, year, ≥1 author, full pub date, vol/issue/pages, copyright. The Crossref deposit minimum — must be satisfied before deposit. |
| **Provenance** | 25% | References, refs-with-DOI, preprint→VoR link, Crossmark, conflict-of-interest, data/code availability. |
| **People** | 20% | Full author names, ORCID for corresponding author, ORCID for every author, CRediT contributor roles. |
| **Funding** | 20% | Funder Registry DOI, award/grant numbers. |
| **Access** | 20% | Abstract (plain + JATS), license, OA indicator, plain-language summary. |
| **Organizations** | 15% | Affiliations extracted, ROR for every affiliation. |

The hero **Research Nexus score** is the weight-averaged percentage across
the five dimensions. The Mandatory gate decides whether the record is
depositable at all.

---

## Architecture

Five layers, each layered on the one below:

| Layer | What | Cost | Trigger |
|---|---|---|---|
| **L0 · Docling layout** | PDF/DOCX → structured JSON + per-element bboxes + page renders | $0 | Always (parse stage) |
| **L1 · Deterministic factsheet** | Regex sweep (DOI / ORCID / ROR / ISSN / arXiv / license / grant patterns / preprint DOIs) + PDF /Info + header parser (authors + affiliation marker map) + boilerplate anchor matching (funding / CoI / data availability / ethics) | $0 | Always (parse stage) |
| **L2 · Free enricher APIs** | ORCID public API · ROR v2 · OpenAlex · Crossref REST. Now with affiliation normalisation (Solr-style alternatives, `Indian Institute of Technology, Delhi → Indian Institute of Technology Delhi`), name-swap fallback for Indian/Telugu profiles, and ROR clear-winner auto-accept (top score ≥ 0.95 with 0.10+ margin). | $0 | Auto-fix (publisher click) |
| **L3 · LLM picker** | When the editor explicitly opts in, runs ONE structured-output call per ambiguous field that picks among the API-returned candidates with reasoning + confidence. | ~$0.0002/call | Per-field "Adjudicate with AI" |
| **L4 · LLM structurer** | Higher-leverage: takes a raw content region (or editor-located text via `text_override`) and returns a clean structured record. Five named tasks: `structure_authors`, `structure_references`, `structure_funding`, `structure_credit`, `verify_authors`. The verifier now also receives the paper's title + abstract excerpt + OpenAlex concepts so it can reject candidates whose research domain doesn't match. | ~$0.0003 – $0.013/call | Per-field "Identify on document" with AI cost meta-pill |

**Cost rule**: the LLM is never called automatically. Every paid call is
the publisher's deliberate choice and is recorded in a per-submission USD
ledger.

For a typical paper, full premium processing tops out around **$0.025**.
Standard auto-fix (no LLM) is **$0**.

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
   Returned by `GET /submissions/{id}/score` — includes per-dimension
   scores, the Research Nexus weighted score, the Mandatory gate state,
   and the entity-count pillars (e.g. `7/9 authors with ORCID`,
   `3/8 affiliations with ROR`).
4. **Auto-fix** (free, deterministic):
   - `POST /submissions/{id}/autofix/all` runs every high-impact fixer
     in one click (the hero CTA).
5. **Identify on document** (the unified per-field action when something
   is missing): editor selects boxes containing the value; the backend
   transparently routes to either field-aware regex (for deterministic
   fields) or the LLM structurer with `text_override` (for AI-leverage
   fields), with an upfront cost-confirm dialog.
6. **Confirm / Reject** — `POST /submissions/{id}/confirm` flips
   `provenance.confirmed=true`; `POST .../reject` flips back to
   `needs_locate` to prompt re-identification.
7. **Generate XML** — `POST /submissions/{id}/xml` builds, `GET .../xml`
   downloads.

---

## API surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/submissions` | Multipart upload, kicks off parse |
| `GET`  | `/submissions` | List submissions + status |
| `GET`  | `/submissions/{id}` | Status of one submission |
| `DELETE` | `/submissions/{id}` | Remove submission + all generated files |
| `GET`  | `/submissions/{id}/factsheet` | Deterministic L1 output |
| `GET`  | `/submissions/{id}/score` | Dimension rubric + Research Nexus score + entity pillars |
| `GET`  | `/submissions/{id}/sections` | Layout-derived sections |
| `GET`  | `/submissions/{id}/sections/{n}` | One section with full text |
| `GET`  | `/submissions/{id}/markdown` | Full Docling markdown |
| `GET`  | `/submissions/{id}/pages` | Per-page render dimensions |
| `GET`  | `/submissions/{id}/pages/{n}/image` | Page PNG |
| `GET`  | `/submissions/{id}/pages/{n}/boxes` | Per-page Docling bboxes + text |
| `GET`  | `/submissions/{id}/references_layout` | Three-tier references detection result |
| `GET`  | `/submissions/{id}/provenance` | Per-field source / confidence / reasoning |
| `GET`  | `/submissions/{id}/cost` | LLM cost ledger for this submission |
| `GET`  | `/submissions/{id}/metadata` | Saved metadata JSON |
| `PUT`  | `/submissions/{id}/metadata` | Save edited metadata |
| `POST` | `/submissions/{id}/autofix` | One deterministic fixer (free) |
| `POST` | `/submissions/{id}/autofix/all` | Run every high-impact fixer (free) |
| `POST` | `/submissions/{id}/pick` | Manual editor pick from candidate list (free) |
| `POST` | `/submissions/{id}/disambiguate/estimate` | Preview LLM cost (free) |
| `POST` | `/submissions/{id}/disambiguate` | Opt-in LLM picker (~$0.0002/call) |
| `POST` | `/submissions/{id}/structure/{task}/estimate` | Per-task cost preview |
| `POST` | `/submissions/{id}/structure/{task}` | Opt-in LLM structurer; optional body `{"text_override": "..."}` for editor-located source text |
| `POST` | `/submissions/{id}/enrich/all` | Run all premium structurers in sequence |
| `POST` | `/submissions/{id}/confirm` | Editor confirms a field |
| `POST` | `/submissions/{id}/reject` | Editor rejects a field → `needs_locate` |
| `POST` | `/submissions/{id}/locate` | Editor pointed to box(es); regex extraction for deterministic fields |
| `POST` | `/submissions/{id}/xml` | Build Crossref XML |
| `GET`  | `/submissions/{id}/xml` | Download XML |

---

## GUI

Vite + React + react-router. Restrained scholarly palette
(parchment / inkwell / muted-stone / onyx-orange accent), shadcn-style
icons (lucide), Lato body + JetBrains Mono code, 4/8px radii, compact
density. Light is the canonical theme; dark is a derived inverse.

Two pages:

- **Submissions** (`/upload`) — drag-and-drop dropzone, status pills,
  per-row delete.
- **Review** (`/review/:id`) — top-to-bottom flow:
  1. **Hero** — Research Nexus score (weighted), Mandatory gate banner
     ("9/9 fields · Depositable" or "Not yet depositable"), five
     dimension bars with weight labels, action buttons (`Run automated
     extraction` · `Run AI enrichment ~$0.0NN` · `Generate Crossref XML`).
  2. **Sticky dimension nav** — chip per dimension with current score, jumps to that section.
  3. **Per-dimension sections** — Mandatory, then Provenance / People /
     Funding / Access / Organizations. Each has a strong header (weight
     badge, title, description, score number, progress bar), a
     dimension-owned entity-progress strip when applicable (e.g. People
     shows "7/9 authors with ORCID"), and field cards split into two
     sub-buckets:
     - **Needs attention** — everything not confirmed
     - **Confirmed** — green-edged cards collapsed beneath
  4. **Field cards** — each card is collapsed by default; click to expand
     the detail panel showing the structured data inline:
     - Author-related fields (full names, ORCIDs, affiliations, RORs)
       expand to show a per-author list with ORCID / ROR / affiliation
       chips and AI evidence chains.
     - CRediT contributor roles expand to show per-author roles with
       evidence quotes from the contribution paragraph and confidence %.
- **Step-by-step CTAs**: at most one primary action per state. Missing
  fields show a single `Identify on document` button (with cost
  meta-pill if AI-leverage). Pending fields show `Confirm` / `Reject`.
  Confirmed fields show only `Reject and re-identify`. The hero owns
  the global `Run automated extraction` (free pass) and `Run AI
  enrichment` (priced pass) buttons.

---

## Quick start

```bash
git clone https://github.com/aadivar/metadata_gapfixer
cd metadata_gapfixer
cp .env.example .env                # edit OPENAI_API_KEY + CONTACT_EMAIL
docker compose up -d --build
```

Open <http://localhost:3000>.

First boot downloads:
- `ghcr.io/docling-project/docling-serve:latest` (~2 GB)
- GLiNER2 (`fastino/gliner2-large-v1`) into `./data/hf-cache/` (~1.4 GB)

Tail logs with `docker compose logs -f backend`.

> **Set `CONTACT_EMAIL` in `.env`** — it's used in the User-Agent for
> ORCID, ROR, OpenAlex, and Crossref polite-pool routing. Identified
> clients get much higher rate limits.

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
| `CONTACT_EMAIL` | `anonymous@example.org` | Sent in User-Agent for ORCID/ROR/OpenAlex/Crossref polite pools — **change this** |
| `HF_TOKEN` | *(unset)* | Optional — higher rate limits on HuggingFace downloads |

Per-task model routing lives in `backend/app/services/llm_router.py`'s
`TASK_CONFIG` dict.

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

**To swap storage** (S3, GCS, Azure Blob, NFS, etc.): bind-mount
`./data/uploads` and `./data/outputs` to your network storage (works for
any POSIX-mountable backend), and replace the SQLite engine in
`backend/app/db.py` with a Postgres / MySQL URL — SQLModel speaks both.

We deliberately did not embed an S3 / cloud-specific client so the project
stays portable. Pick what fits your infrastructure.

---

## Layout

```
metadata_gapfixer/
├── docker-compose.yml
├── .env.example
├── README.md  · DESIGN.md
├── LICENSE                                   # AGPL-v3
├── backend/
│   ├── Dockerfile · requirements.txt
│   ├── templates/journal_article.xml.j2
│   └── app/
│       ├── main.py · config.py · db.py · models.py · pipeline.py
│       ├── routes/
│       │   ├── health.py
│       │   └── submissions.py
│       └── services/
│           ├── docling_client.py · page_render.py · sections.py
│           ├── factsheet.py · ner.py
│           ├── scoring.py                    # rubric + dimensions + Research Nexus score
│           ├── autofix.py                    # free deterministic fixers + needs_pick
│           ├── llm_router.py                 # per-task models + cost ledger
│           ├── structurers.py                # five LLM structurers + paper-context aware verifier
│           ├── crossref_xml.py
│           └── enrichers/
│               ├── _base.py · orcid.py · ror.py · openalex.py · crossref.py
│               └── (ORCID name-swap, ROR comma-delete normalisation, etc.)
└── frontend/
    ├── Dockerfile · package.json
    └── src/
        ├── App.tsx · api.ts · theme.ts · icons.tsx · styles.css
        └── pages/
            ├── Upload.tsx
            └── Review.tsx                    # dimension-bucketed scorecard + step-by-step CTAs
```

---

## Notes / caveats

- **Journal articles only.** No book chapters, conference papers, datasets,
  preprints (as deposit type). Preprint *relations* on a journal article
  are supported via the `preprint_doi` field.
- **No fabrication.** The LLM is constrained by structured outputs and
  explicit candidate lists. ORCIDs, DOIs, RORs, ISSNs come from APIs or
  PDF text — never made up.
- **Topic-aware verification.** The `verify_authors` structurer now
  receives the paper's title, abstract excerpt, and OpenAlex concepts,
  and rejects candidates whose top concepts have zero overlap with the
  paper — even when the institution matches.
- **XML is well-formedness checked, not XSD-validated.** Run Crossref's
  XML validator (or `xmllint --schema crossref5.3.1.xsd ...`) before
  deposit. The template targets schema 5.3.1.
- **LLM provider** must support OpenAI structured outputs (`response_format`
  with `json_schema`).

---

## Credits

Built by the team behind **[nexus-score.vercel.app](https://nexus-score.vercel.app/)**.
Source on GitHub: <https://github.com/aadivar/metadata_gapfixer>.

## License

[AGPL-v3](https://www.gnu.org/licenses/agpl-3.0.html). If you run a
modified version of this software on a network-accessible service, you
must offer that modified source to the service's users.
