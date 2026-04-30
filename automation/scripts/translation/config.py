"""Configuration for the subtitle translator — Ollama-only (2026-04-29)."""

import os
from dataclasses import dataclass, field

DEFAULT_BAZARR_URL = "http://127.0.0.1:6767/bazarr"
DEFAULT_BAZARR_DB = "/opt/bazarr/data/db/bazarr.db"
DEFAULT_STATE_DIR = "/APPBOX_DATA/storage/.translation-state"

# Provider constants — DeepL/Gemini/Google retained as dead strings so existing
# DB rows (provider column) remain readable by the status command.
PROVIDER_DEEPL = "deepl"
PROVIDER_GEMINI = "gemini"
PROVIDER_GOOGLE = "google"
PROVIDER_OLLAMA = "ollama"

# Bazarr 2-letter code → full language name for Ollama prompting
OLLAMA_LANG_MAP = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Simplified Chinese",
    "zt": "Traditional Chinese",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "el": "Greek",
    "cs": "Czech",
    "ro": "Romanian",
    "hu": "Hungarian",
    "sk": "Slovak",
    "bg": "Bulgarian",
    "tr": "Turkish",
    "id": "Indonesian",
    "uk": "Ukrainian",
    "ar": "Arabic",
    "nb": "Norwegian",
    "et": "Estonian",
    "lv": "Latvian",
    "lt": "Lithuanian",
    "sl": "Slovenian",
    "hi": "Hindi",
    "th": "Thai",
    "vi": "Vietnamese",
    "ms": "Malay",
    "he": "Hebrew",
    "fa": "Persian",
    "sr": "Serbian",
    "hr": "Croatian",
    "ca": "Catalan",
    "tl": "Filipino",
}

# Merged set of all supported language codes (Ollama covers everything)
ALL_SUPPORTED_LANGS = frozenset(OLLAMA_LANG_MAP)
ALL_SUPPORTED_SOURCE_LANGS = frozenset(OLLAMA_LANG_MAP)

# DeepL/Gemini/Google provider dicts removed (Phase 4b cleanup).
# Old DB rows still use string literals "deepl"/"gemini"/"google" — those are fine.


@dataclass
class Config:
    bazarr_api_key: str = ""
    discord_webhook_url: str = ""
    bazarr_url: str = DEFAULT_BAZARR_URL
    bazarr_db: str = DEFAULT_BAZARR_DB
    state_dir: str = DEFAULT_STATE_DIR
    ollama_base_url: str = ""
    ollama_model: str = "subtitler:v2"
    # Kept as empty lists — referenced by some helper functions but never used.
    deepl_api_keys: list = field(default_factory=list)
    gemini_api_keys: list = field(default_factory=list)
    google_translate_enabled: bool = False


def load_config(
    bazarr_db=None,
    bazarr_url=None,
    state_dir=None,
) -> Config:
    """Load config from environment variables with optional CLI overrides."""
    ollama_base_url = os.environ.get("OLLAMA_BASE_URL", "").strip()
    ollama_model = os.environ.get("OLLAMA_MODEL", "subtitler:v2").strip()

    if not ollama_base_url:
        raise ValueError(
            "No translation provider available: set OLLAMA_BASE_URL"
        )

    return Config(
        bazarr_api_key=os.environ.get("BAZARR_API_KEY", ""),
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        bazarr_url=bazarr_url or os.environ.get("BAZARR_URL", DEFAULT_BAZARR_URL),
        bazarr_db=bazarr_db or DEFAULT_BAZARR_DB,
        state_dir=state_dir or DEFAULT_STATE_DIR,
        ollama_base_url=ollama_base_url,
        ollama_model=ollama_model,
    )
