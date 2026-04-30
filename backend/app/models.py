from typing import Optional
from pydantic import BaseModel, Field


class Author(BaseModel):
    given_name: Optional[str] = None
    surname: Optional[str] = None
    full_name: Optional[str] = None
    orcid: Optional[str] = None
    affiliations: list[str] = Field(default_factory=list)
    ror_ids: list[str] = Field(default_factory=list)
    is_corresponding: bool = False
    email: Optional[str] = None


class Funder(BaseModel):
    name: Optional[str] = None
    doi: Optional[str] = None
    award_numbers: list[str] = Field(default_factory=list)


class Reference(BaseModel):
    raw: str
    doi: Optional[str] = None
    title: Optional[str] = None
    year: Optional[int] = None


class JournalArticleMetadata(BaseModel):
    title: Optional[str] = None
    subtitle: Optional[str] = None
    abstract: Optional[str] = None
    language: str = "en"
    authors: list[Author] = Field(default_factory=list)
    journal_title: Optional[str] = None
    journal_abbrev: Optional[str] = None
    issn_print: Optional[str] = None
    issn_electronic: Optional[str] = None
    volume: Optional[str] = None
    issue: Optional[str] = None
    first_page: Optional[str] = None
    last_page: Optional[str] = None
    publication_date: Optional[str] = None  # YYYY-MM-DD
    doi: Optional[str] = None
    resource_url: Optional[str] = None
    funders: list[Funder] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    license_url: Optional[str] = None
    confidence_notes: dict[str, str] = Field(default_factory=dict)

    # Per-field provenance: {field_path: {source, confidence, reasoning, alternatives}}
    # field_path examples:
    #   "doi" — top-level field
    #   "authors[3].orcid" — author at index 3, orcid sub-field
    #   "funders[0].doi"
    # Sources: "factsheet" | "regex" | "docling" | "header_parser" | "boilerplate"
    #          | "orcid_api" | "ror_api" | "openalex_api" | "crossref_api"
    #          | "llm_disambiguated" | "llm_structured" | "needs_review"
    #          | "needs_pick" | "user_pick" | "user_edit"
    provenance: dict[str, dict] = Field(default_factory=dict)

    # CRediT contributor roles — one entry per author when the paper has an
    # "Author contributions" section. Populated by the structure_credit task.
    credit_contributions: list[dict] = Field(default_factory=list)

    # Detected preprint DOI (e.g. bioRxiv) for the version-of-record relation.
    preprint_doi: Optional[str] = None
    is_open_access: Optional[bool] = None


class SubmissionOut(BaseModel):
    id: int
    filename: str
    status: str
    error: Optional[str] = None
