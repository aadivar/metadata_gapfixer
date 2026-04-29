from datetime import datetime
from typing import Optional
from sqlmodel import Field, SQLModel, Session, create_engine

from .config import settings


class Submission(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    filename: str
    upload_path: str
    status: str = "uploaded"  # uploaded | parsing | extracting | enriching | ready | error
    error: Optional[str] = None
    docling_json_path: Optional[str] = None
    layout_json_path: Optional[str] = None
    entities_json_path: Optional[str] = None
    metadata_json_path: Optional[str] = None
    crossref_xml_path: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


engine = create_engine(f"sqlite:///{settings.sqlite_path}", echo=False)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)
