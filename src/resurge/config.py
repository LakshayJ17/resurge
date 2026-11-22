"""Runtime settings: paths, environment, and policy switches.

Resolution order: explicit arguments > environment variables (.env supported) >
defaults. Nothing here performs network I/O.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent.parent  # src/resurge -> project root (editable installs)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_choice(name: str, choices: tuple[str, ...], default: str) -> str:
    raw = (os.getenv(name) or "").strip().lower()
    return raw if raw in choices else default


def _env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _find_config_dir() -> Path:
    env = os.getenv("RESURGE_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    for candidate in (Path.cwd() / "config", PROJECT_ROOT / "config", PACKAGE_DIR / "config"):
        if (candidate / "models.yaml").exists():
            return candidate
    return PROJECT_ROOT / "config"


@dataclass
class Settings:
    home: Path = field(default_factory=lambda: Path(os.getenv("RESURGE_HOME", Path.cwd())).expanduser())
    config_dir: Path = field(default_factory=_find_config_dir)
    data_dir: Path | None = None
    reports_dir: Path | None = None

    # policy switches
    local_only: bool = field(default_factory=lambda: _env_bool("RESURGE_LOCAL_ONLY", False))
    private_mode: bool = field(default_factory=lambda: _env_bool("RESURGE_PRIVATE", False))
    providers_allow: list[str] = field(default_factory=lambda: _env_list("RESURGE_PROVIDERS"))
    providers_deny: list[str] = field(default_factory=lambda: _env_list("RESURGE_PROVIDERS_DENY"))
    use_cache: bool = field(default_factory=lambda: _env_bool("RESURGE_CACHE", True))
    learning_enabled: bool = field(default_factory=lambda: _env_bool("RESURGE_LEARNING", True))
    guardrails: str = field(default_factory=lambda: _env_choice("RESURGE_GUARDRAILS", ("on", "strict", "off"), "on"))
    max_wait_s: float = field(default_factory=lambda: float(os.getenv("RESURGE_MAX_RATE_WAIT_S", "25")))
    llm_timeout_s: float = field(default_factory=lambda: float(os.getenv("RESURGE_LLM_TIMEOUT_S", "180")))
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir or os.getenv("RESURGE_DATA_DIR", self.home / "data")).expanduser()
        self.reports_dir = Path(self.reports_dir or os.getenv("RESURGE_REPORTS_DIR", self.home / "reports")).expanduser()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "resurge.db"  # type: ignore[operator]

    @property
    def models_file(self) -> Path:
        return self.config_dir / "models.yaml"

    @property
    def mcp_servers_file(self) -> Path:
        return self.config_dir / "mcp_servers.yaml"

    def ensure_dirs(self) -> None:
        assert self.data_dir and self.reports_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)


_OUR_PREFIXES = ("RESURGE_", "TURNSTILE_", "QDRANT_", "OLLAMA_", "GOOGLE_", "CLOUDFLARE_", "SLACK_", "DISCORD_")


def _drop_comment_values() -> None:
    """`docker run --env-file` keeps inline comments, so a blank `GROQ_API_KEY=  # https://...` arrives as the value
    "# https://...". Such values are never real keys or settings: treat them as empty."""
    for k, v in list(os.environ.items()):
        if v.strip().startswith("#") and (k.startswith(_OUR_PREFIXES) or k.endswith(("_API_KEY", "_TOKEN"))):
            os.environ[k] = ""


def load_settings(**overrides) -> Settings:
    """Load .env (cwd first, then project root) and build Settings."""
    for env_file in (Path.cwd() / ".env", PROJECT_ROOT / ".env"):
        if env_file.exists():
            load_dotenv(env_file, override=False)
    _drop_comment_values()
    s = Settings(**{k: v for k, v in overrides.items() if v is not None})
    return s
