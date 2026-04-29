from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    docling_base_url: str = "http://docling:5001"
    data_dir: Path = Path("/data")
    sqlite_path: Path = Path("/data/mgf.db")

    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    contact_email: str = "anonymous@example.org"
    gliner_model: str = "fastino/gliner2-large-v1"

    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
(settings.data_dir / "uploads").mkdir(exist_ok=True)
(settings.data_dir / "outputs").mkdir(exist_ok=True)
(settings.data_dir / "cache").mkdir(exist_ok=True)
