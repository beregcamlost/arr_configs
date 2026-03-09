"""Configuration for the streaming availability checker."""

import os
from dataclasses import dataclass, field
from typing import List

PROVIDER_MAP = {
    "netflix": 8,
    "disney": 337,
    "hbo": 384,
    "amazon": 119,
    "apple": 350,
    "paramount": 531,
}

DEFAULT_PROVIDERS = ["netflix", "disney"]
DEFAULT_COUNTRY = "CL"
DEFAULT_DB_PATH = "/APPBOX_DATA/storage/.streaming-checker-state/streaming_state.db"


@dataclass
class Config:
    tmdb_api_key: str
    radarr_key: str
    sonarr_key: str
    emby_api_key: str = ""
    discord_webhook_url: str = ""
    radarr_url: str = "http://127.0.0.1:7878/radarr"
    sonarr_url: str = "http://127.0.0.1:8989/sonarr"
    emby_url: str = "http://127.0.0.1:8096"
    country: str = DEFAULT_COUNTRY
    providers: List[str] = field(default_factory=lambda: list(DEFAULT_PROVIDERS))
    rapidapi_key: str = ""
    watchmode_api_key: str = ""
    dry_run: bool = False
    verbose: bool = False
    db_path: str = DEFAULT_DB_PATH

    @property
    def provider_ids(self) -> List[int]:
        return [PROVIDER_MAP[p] for p in self.providers]


def load_config(
    country=None,
    providers=None,
    dry_run=False,
    verbose=False,
    db_path=None,
) -> Config:
    """Load config from environment variables with optional CLI overrides."""
    tmdb_key = os.environ.get("TMDB_API_KEY", "")
    if not tmdb_key:
        raise ValueError("TMDB_API_KEY environment variable is required")

    radarr_key = os.environ.get("RADARR_KEY", "")
    if not radarr_key:
        raise ValueError("RADARR_KEY environment variable is required")

    sonarr_key = os.environ.get("SONARR_KEY", "")
    if not sonarr_key:
        raise ValueError("SONARR_KEY environment variable is required")

    parsed_providers = DEFAULT_PROVIDERS
    if providers:
        parsed_providers = [p.strip() for p in providers.split(",")]
        for p in parsed_providers:
            if p not in PROVIDER_MAP:
                raise ValueError(
                    f"Unknown provider '{p}'. Available: {', '.join(PROVIDER_MAP.keys())}"
                )

    return Config(
        tmdb_api_key=tmdb_key,
        radarr_key=radarr_key,
        sonarr_key=sonarr_key,
        emby_api_key=os.environ.get("EMBY_API_KEY", ""),
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        radarr_url=os.environ.get("RADARR_URL", "http://127.0.0.1:7878/radarr"),
        sonarr_url=os.environ.get("SONARR_URL", "http://127.0.0.1:8989/sonarr"),
        emby_url=os.environ.get("EMBY_URL", "http://127.0.0.1:8096"),
        rapidapi_key=os.environ.get("RAPIDAPI_KEY", ""),
        watchmode_api_key=os.environ.get("WATCHMODE_API_KEY", ""),
        country=country or DEFAULT_COUNTRY,
        providers=parsed_providers,
        dry_run=dry_run,
        verbose=verbose,
        db_path=db_path or DEFAULT_DB_PATH,
    )
