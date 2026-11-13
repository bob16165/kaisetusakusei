import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path
    ollama_base_url: str
    ollama_model: str
    external_api_enabled: bool


def load_settings() -> Settings:
    data_dir_value = os.getenv("MKE_DATA_DIR", "storage")
    data_dir = Path(data_dir_value).expanduser()
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    return Settings(
        project_root=PROJECT_ROOT,
        data_dir=data_dir.resolve(),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:3b"),
        external_api_enabled=os.getenv("EXTERNAL_API_ENABLED", "false").lower()
        in {"1", "true", "yes"},
    )


settings = load_settings()
