"""Configuration for the subtitle translator."""

import os
from dataclasses import dataclass, field
from datetime import date

DEFAULT_BAZARR_URL = "http://127.0.0.1:6767/bazarr"
DEFAULT_BAZARR_DB = "/opt/bazarr/data/db/bazarr.db"
DEFAULT_STATE_DIR = "/APPBOX_DATA/storage/.translation-state"

# DeepL quota reset date — skip DeepL until this date (inclusive)
# Set via DEEPL_SKIP_UNTIL env var (YYYY-MM-DD) or None to disable
_skip_raw = os.environ.get("DEEPL_SKIP_UNTIL", "").strip()
DEEPL_SKIP_UNTIL = date.fromisoformat(_skip_raw) if _skip_raw else None

# Provider constants
PROVIDER_DEEPL = "deepl"
PROVIDER_GEMINI = "gemini"
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

# Bazarr 2-letter code → full language name for Gemini prompting
# Gemini uses natural language names (supports all languages via prompting)
GEMINI_LANG_MAP = {
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

# Merged set of all supported target/source language codes (for quick "any provider" checks)
ALL_SUPPORTED_LANGS = frozenset(DEEPL_LANG_MAP) | frozenset(GEMINI_LANG_MAP) | frozenset(GOOGLE_LANG_MAP)
ALL_SUPPORTED_SOURCE_LANGS = frozenset(DEEPL_SOURCE_LANG_MAP) | frozenset(GEMINI_LANG_MAP) | frozenset(GOOGLE_LANG_MAP)


@dataclass
class Config:
    deepl_api_keys: list = field(default_factory=list)
    bazarr_api_key: str = ""
    discord_webhook_url: str = ""
    bazarr_url: str = DEFAULT_BAZARR_URL
    bazarr_db: str = DEFAULT_BAZARR_DB
    state_dir: str = DEFAULT_STATE_DIR
    google_translate_enabled: bool = True
    gemini_api_keys: list = field(default_factory=list)


def load_config(
    bazarr_db=None,
    bazarr_url=None,
    state_dir=None,
) -> Config:
    """Load config from environment variables with optional CLI overrides."""
    deepl_keys = [k.strip() for k in os.environ.get("DEEPL_API_KEYS", "").split(",") if k.strip()]
    # Legacy single-key support for backward compatibility — prefer DEEPL_API_KEYS
    legacy_key = os.environ.get("DEEPL_API_KEY", "").strip()
    if legacy_key and legacy_key not in deepl_keys:
        deepl_keys.append(legacy_key)

    google_enabled = os.environ.get("GOOGLE_TRANSLATE_ENABLED", "1") != "0"

    gemini_keys = [k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()]

    if not deepl_keys and not gemini_keys and not google_enabled:
        raise ValueError(
            "No translation provider available: set DEEPL_API_KEYS, "
            "GEMINI_API_KEYS, or enable Google Translate"
        )

    return Config(
        deepl_api_keys=deepl_keys,
        bazarr_api_key=os.environ.get("BAZARR_API_KEY", ""),
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        bazarr_url=bazarr_url or os.environ.get("BAZARR_URL", DEFAULT_BAZARR_URL),
        bazarr_db=bazarr_db or DEFAULT_BAZARR_DB,
        state_dir=state_dir or DEFAULT_STATE_DIR,
        google_translate_enabled=google_enabled,
        gemini_api_keys=gemini_keys,
    )
