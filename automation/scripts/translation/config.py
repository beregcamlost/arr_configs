"""Configuration for the subtitle translator."""

import os
from dataclasses import dataclass

DEFAULT_BAZARR_URL = "http://127.0.0.1:6767/bazarr"
DEFAULT_BAZARR_DB = "/opt/bazarr/data/db/bazarr.db"
DEFAULT_STATE_DIR = "/APPBOX_DATA/storage/.translation-state"

# Provider constants
PROVIDER_DEEPL = "deepl"
PROVIDER_GOOGLE = "google"

# Bazarr 2-letter code → DeepL target language code
DEEPL_LANG_MAP = {
    "en": "EN-US",
    "es": "ES",
    "fr": "FR",
    "de": "DE",
    "it": "IT",
    "pt": "PT-BR",
    "nl": "NL",
    "pl": "PL",
    "ru": "RU",
    "ja": "JA",
    "ko": "KO",
    "zh": "ZH-HANS",
    "zt": "ZH-HANT",
    "sv": "SV",
    "da": "DA",
    "fi": "FI",
    "el": "EL",
    "cs": "CS",
    "ro": "RO",
    "hu": "HU",
    "sk": "SK",
    "bg": "BG",
    "tr": "TR",
    "id": "ID",
    "uk": "UK",
    "ar": "AR",
    "nb": "NB",
    "et": "ET",
    "lv": "LV",
    "lt": "LT",
    "sl": "SL",
}

# Bazarr 2-letter code → DeepL source language code (no region needed)
DEEPL_SOURCE_LANG_MAP = {
    "en": "EN",
    "es": "ES",
    "fr": "FR",
    "de": "DE",
    "it": "IT",
    "pt": "PT",
    "nl": "NL",
    "pl": "PL",
    "ru": "RU",
    "ja": "JA",
    "ko": "KO",
    "zh": "ZH",
    "zt": "ZH",
    "sv": "SV",
    "da": "DA",
    "fi": "FI",
    "el": "EL",
    "cs": "CS",
    "ro": "RO",
    "hu": "HU",
    "sk": "SK",
    "bg": "BG",
    "tr": "TR",
    "id": "ID",
    "uk": "UK",
    "ar": "AR",
    "nb": "NB",
    "et": "ET",
    "lv": "LV",
    "lt": "LT",
    "sl": "SL",
}

# Bazarr 2-letter code → Google Translate language code
# Google uses the same codes for source and target languages
GOOGLE_LANG_MAP = {
    "en": "en",
    "es": "es",
    "fr": "fr",
    "de": "de",
    "it": "it",
    "pt": "pt",
    "nl": "nl",
    "pl": "pl",
    "ru": "ru",
    "ja": "ja",
    "ko": "ko",
    "zh": "zh-cn",
    "zt": "zh-tw",
    "sv": "sv",
    "da": "da",
    "fi": "fi",
    "el": "el",
    "cs": "cs",
    "ro": "ro",
    "hu": "hu",
    "sk": "sk",
    "bg": "bg",
    "tr": "tr",
    "id": "id",
    "uk": "uk",
    "ar": "ar",
    "nb": "no",
    "et": "et",
    "lv": "lv",
    "lt": "lt",
    "sl": "sl",
    "hi": "hi",
    "th": "th",
    "vi": "vi",
    "ms": "ms",
    "he": "iw",
    "fa": "fa",
    "sr": "sr",
    "hr": "hr",
    "ca": "ca",
    "tl": "tl",
}


@dataclass
class Config:
    deepl_api_key: str = ""
    bazarr_api_key: str = ""
    discord_webhook_url: str = ""
    bazarr_url: str = DEFAULT_BAZARR_URL
    bazarr_db: str = DEFAULT_BAZARR_DB
    state_dir: str = DEFAULT_STATE_DIR
    google_translate_enabled: bool = True


def load_config(
    bazarr_db=None,
    bazarr_url=None,
    state_dir=None,
) -> Config:
    """Load config from environment variables with optional CLI overrides."""
    deepl_key = os.environ.get("DEEPL_API_KEY", "")
    google_enabled = os.environ.get("GOOGLE_TRANSLATE_ENABLED", "1") != "0"

    if not deepl_key and not google_enabled:
        raise ValueError(
            "DEEPL_API_KEY is required when Google Translate is disabled"
        )

    return Config(
        deepl_api_key=deepl_key,
        bazarr_api_key=os.environ.get("BAZARR_API_KEY", ""),
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        bazarr_url=bazarr_url or os.environ.get("BAZARR_URL", DEFAULT_BAZARR_URL),
        bazarr_db=bazarr_db or DEFAULT_BAZARR_DB,
        state_dir=state_dir or DEFAULT_STATE_DIR,
        google_translate_enabled=google_enabled,
    )
