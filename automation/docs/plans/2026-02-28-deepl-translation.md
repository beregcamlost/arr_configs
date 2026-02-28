# DeepL Subtitle Translation — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Automatically translate missing profile-language subtitles using DeepL free API, triggered by cron and import hook.

**Architecture:** Python package at `automation/scripts/translation/` following the streaming checker pattern (click CLI, SQLite state DB, Discord notifications). Two entry points: `translate --since N` for cron, `translate --file PATH` for import hook. SRT files parsed in Python, text sent to DeepL SDK in batches, reassembled with original timestamps.

**Tech Stack:** Python 3.12, `deepl` SDK, `click` CLI, SQLite (WAL mode), `requests` (Discord webhooks)

---

### Task 1: Project Skeleton + Dependencies

**Files:**
- Create: `automation/scripts/translation/__init__.py`
- Create: `automation/scripts/translation/tests/__init__.py`
- Create: `automation/scripts/translation/tests/conftest.py`

**Step 1: Install deepl SDK**

Run: `pip3 install deepl`
Expected: Successfully installed deepl-X.X.X

**Step 2: Create package skeleton**

```python
# automation/scripts/translation/__init__.py
# (empty)
```

```python
# automation/scripts/translation/tests/__init__.py
# (empty)
```

```python
# automation/scripts/translation/tests/conftest.py
"""Shared test fixtures for translation tests."""

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir),
)

from translation.db import init_db


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite database for testing."""
    db_path = str(tmp_path / "test_translation.db")
    init_db(db_path)
    return db_path


@pytest.fixture
def env_config(monkeypatch):
    """Set up environment variables for Config loading."""
    monkeypatch.setenv("DEEPL_API_KEY", "test-deepl-key:fx")
    monkeypatch.setenv("BAZARR_API_KEY", "test-bazarr-key")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
```

Note: conftest.py references `translation.db` which doesn't exist yet — that's fine, it will be created in Task 3.

**Step 3: Verify pytest can discover the test directory**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -c "import translation; print('OK')"`
Expected: OK

**Step 4: Commit**

```bash
git add automation/scripts/translation/
pip3 freeze | grep -i deepl >> automation/scripts/translation/requirements.txt
git add automation/scripts/translation/requirements.txt
git commit -m "feat(translation): project skeleton + deepl SDK dependency"
```

---

### Task 2: Config Module

**Files:**
- Create: `automation/scripts/translation/config.py`
- Create: `automation/scripts/translation/tests/test_config.py`

**Step 1: Write the failing tests**

```python
# automation/scripts/translation/tests/test_config.py
"""Tests for translation config."""

from translation.config import Config, load_config, DEEPL_LANG_MAP


def test_load_config_from_env(env_config):
    """Config loads all required values from environment."""
    cfg = load_config()
    assert cfg.deepl_api_key == "test-deepl-key:fx"
    assert cfg.bazarr_api_key == "test-bazarr-key"
    assert cfg.discord_webhook_url == "https://discord.com/api/webhooks/test"
    assert cfg.bazarr_url == "http://127.0.0.1:6767/bazarr"
    assert cfg.bazarr_db == "/opt/bazarr/data/db/bazarr.db"


def test_load_config_missing_deepl_key():
    """Config raises ValueError when DEEPL_API_KEY is missing."""
    import pytest
    with pytest.raises(ValueError, match="DEEPL_API_KEY"):
        load_config()


def test_load_config_cli_overrides(env_config):
    """CLI overrides take precedence over env vars."""
    cfg = load_config(bazarr_db="/custom/bazarr.db", state_dir="/custom/state")
    assert cfg.bazarr_db == "/custom/bazarr.db"
    assert cfg.state_dir == "/custom/state"


def test_deepl_lang_map_basics():
    """DeepL language mapping covers common Bazarr codes."""
    assert DEEPL_LANG_MAP["en"] == "EN-US"
    assert DEEPL_LANG_MAP["es"] == "ES"
    assert DEEPL_LANG_MAP["fr"] == "FR"
    assert DEEPL_LANG_MAP["it"] == "IT"
    assert DEEPL_LANG_MAP["pt"] == "PT-BR"
    assert DEEPL_LANG_MAP["zh"] == "ZH-HANS"
    assert DEEPL_LANG_MAP["zt"] == "ZH-HANT"


def test_deepl_source_lang_map():
    """Source language mapping uses base codes (no region)."""
    from translation.config import DEEPL_SOURCE_LANG_MAP
    assert DEEPL_SOURCE_LANG_MAP["en"] == "EN"
    assert DEEPL_SOURCE_LANG_MAP["es"] == "ES"
    assert DEEPL_SOURCE_LANG_MAP["pt"] == "PT"
    assert DEEPL_SOURCE_LANG_MAP["zh"] == "ZH"
```

**Step 2: Run tests to verify they fail**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/test_config.py -v`
Expected: FAIL (ModuleNotFoundError)

**Step 3: Write implementation**

```python
# automation/scripts/translation/config.py
"""Configuration for the DeepL subtitle translator."""

import os
from dataclasses import dataclass

DEFAULT_BAZARR_URL = "http://127.0.0.1:6767/bazarr"
DEFAULT_BAZARR_DB = "/opt/bazarr/data/db/bazarr.db"
DEFAULT_STATE_DIR = "/APPBOX_DATA/storage/.translation-state"

# Bazarr 2-letter code → DeepL target language code
# DeepL requires region-specific codes for some languages
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


@dataclass
class Config:
    deepl_api_key: str
    bazarr_api_key: str = ""
    discord_webhook_url: str = ""
    bazarr_url: str = DEFAULT_BAZARR_URL
    bazarr_db: str = DEFAULT_BAZARR_DB
    state_dir: str = DEFAULT_STATE_DIR


def load_config(
    bazarr_db=None,
    bazarr_url=None,
    state_dir=None,
) -> Config:
    """Load config from environment variables with optional CLI overrides."""
    deepl_key = os.environ.get("DEEPL_API_KEY", "")
    if not deepl_key:
        raise ValueError("DEEPL_API_KEY environment variable is required")

    return Config(
        deepl_api_key=deepl_key,
        bazarr_api_key=os.environ.get("BAZARR_API_KEY", ""),
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        bazarr_url=bazarr_url or os.environ.get("BAZARR_URL", DEFAULT_BAZARR_URL),
        bazarr_db=bazarr_db or DEFAULT_BAZARR_DB,
        state_dir=state_dir or DEFAULT_STATE_DIR,
    )
```

**Step 4: Run tests to verify they pass**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/test_config.py -v`
Expected: all 5 tests PASS

**Step 5: Commit**

```bash
git add automation/scripts/translation/config.py automation/scripts/translation/tests/test_config.py
git commit -m "feat(translation): config module with DeepL language mapping"
```

---

### Task 3: State Database

**Files:**
- Create: `automation/scripts/translation/db.py`
- Create: `automation/scripts/translation/tests/test_db.py`

**Step 1: Write the failing tests**

```python
# automation/scripts/translation/tests/test_db.py
"""Tests for translation state database."""

import time
from translation.db import (
    init_db,
    record_translation,
    is_on_cooldown,
    get_monthly_chars,
    get_recent_translations,
)


def test_init_db_creates_table(tmp_db):
    """init_db creates translation_log table."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='translation_log'"
    )
    assert cursor.fetchone() is not None
    conn.close()


def test_record_translation(tmp_db):
    """record_translation inserts a row."""
    record_translation(
        tmp_db,
        media_path="/path/to/video.mkv",
        source_lang="en",
        target_lang="es",
        chars_used=1500,
        status="success",
    )
    rows = get_recent_translations(tmp_db, limit=10)
    assert len(rows) == 1
    assert rows[0]["media_path"] == "/path/to/video.mkv"
    assert rows[0]["chars_used"] == 1500
    assert rows[0]["status"] == "success"


def test_cooldown_active(tmp_db):
    """is_on_cooldown returns True within cooldown window."""
    record_translation(tmp_db, "/path/video.mkv", "en", "es", 100, "success")
    assert is_on_cooldown(tmp_db, "/path/video.mkv", "es", cooldown_hours=24) is True


def test_cooldown_inactive_different_lang(tmp_db):
    """is_on_cooldown returns False for different target language."""
    record_translation(tmp_db, "/path/video.mkv", "en", "es", 100, "success")
    assert is_on_cooldown(tmp_db, "/path/video.mkv", "fr", cooldown_hours=24) is False


def test_cooldown_inactive_different_path(tmp_db):
    """is_on_cooldown returns False for different file."""
    record_translation(tmp_db, "/path/video1.mkv", "en", "es", 100, "success")
    assert is_on_cooldown(tmp_db, "/path/video2.mkv", "es", cooldown_hours=24) is False


def test_get_monthly_chars(tmp_db):
    """get_monthly_chars sums chars_used for current month."""
    record_translation(tmp_db, "/path/v1.mkv", "en", "es", 1000, "success")
    record_translation(tmp_db, "/path/v2.mkv", "en", "fr", 2000, "success")
    record_translation(tmp_db, "/path/v3.mkv", "en", "es", 500, "failed")
    total = get_monthly_chars(tmp_db)
    assert total == 3500  # includes failed — chars were still consumed


def test_get_recent_translations_limit(tmp_db):
    """get_recent_translations respects limit."""
    for i in range(5):
        record_translation(tmp_db, f"/path/v{i}.mkv", "en", "es", 100, "success")
    rows = get_recent_translations(tmp_db, limit=3)
    assert len(rows) == 3
```

**Step 2: Run tests to verify they fail**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/test_db.py -v`
Expected: FAIL (ImportError)

**Step 3: Write implementation**

```python
# automation/scripts/translation/db.py
"""SQLite state database for translation tracking."""

import os
import sqlite3
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path):
    """Create database directory and tables if they don't exist."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = _connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS translation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_path TEXT NOT NULL,
            source_lang TEXT NOT NULL,
            target_lang TEXT NOT NULL,
            chars_used INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_translation_cooldown
            ON translation_log (media_path, target_lang, created_at);
    """)
    conn.close()


def record_translation(db_path, media_path, source_lang, target_lang,
                        chars_used, status):
    """Record a translation attempt."""
    conn = _connect(db_path)
    conn.execute(
        """INSERT INTO translation_log
           (media_path, source_lang, target_lang, chars_used, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (media_path, source_lang, target_lang, chars_used, status, _now_iso()),
    )
    conn.commit()
    conn.close()


def is_on_cooldown(db_path, media_path, target_lang, cooldown_hours=24):
    """Check if a (media_path, target_lang) pair is within cooldown."""
    conn = _connect(db_path)
    cursor = conn.execute(
        """SELECT 1 FROM translation_log
           WHERE media_path = ? AND target_lang = ?
             AND created_at > datetime('now', ?)
           LIMIT 1""",
        (media_path, target_lang, f"-{cooldown_hours} hours"),
    )
    result = cursor.fetchone() is not None
    conn.close()
    return result


def get_monthly_chars(db_path):
    """Get total characters used this calendar month."""
    conn = _connect(db_path)
    cursor = conn.execute(
        """SELECT COALESCE(SUM(chars_used), 0) as total
           FROM translation_log
           WHERE created_at >= date('now', 'start of month')"""
    )
    total = cursor.fetchone()["total"]
    conn.close()
    return total


def get_recent_translations(db_path, limit=20):
    """Get most recent translation log entries."""
    conn = _connect(db_path)
    cursor = conn.execute(
        "SELECT * FROM translation_log ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows
```

**Step 4: Run tests to verify they pass**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/test_db.py -v`
Expected: all 7 tests PASS

**Step 5: Commit**

```bash
git add automation/scripts/translation/db.py automation/scripts/translation/tests/test_db.py
git commit -m "feat(translation): state database with cooldown tracking"
```

---

### Task 4: SRT Parser

**Files:**
- Create: `automation/scripts/translation/srt_parser.py`
- Create: `automation/scripts/translation/tests/test_srt_parser.py`

**Step 1: Write the failing tests**

```python
# automation/scripts/translation/tests/test_srt_parser.py
"""Tests for SRT parser."""

from translation.srt_parser import parse_srt, write_srt, Cue


SAMPLE_SRT = """1
00:00:01,000 --> 00:00:04,000
Hello, world!

2
00:00:05,000 --> 00:00:08,500
This is a <i>test</i>
with multiple lines.

3
00:00:10,000 --> 00:00:12,000
Final cue.
"""


def test_parse_srt_basic():
    """Parse a simple SRT into cues."""
    cues = parse_srt(SAMPLE_SRT)
    assert len(cues) == 3
    assert cues[0].index == 1
    assert cues[0].start == "00:00:01,000"
    assert cues[0].end == "00:00:04,000"
    assert cues[0].text == "Hello, world!"


def test_parse_srt_multiline():
    """Parse preserves multiline cue text."""
    cues = parse_srt(SAMPLE_SRT)
    assert cues[1].text == "This is a <i>test</i>\nwith multiple lines."


def test_parse_srt_empty():
    """Parse empty string returns empty list."""
    assert parse_srt("") == []
    assert parse_srt("   \n\n  ") == []


def test_write_srt_roundtrip():
    """write_srt produces valid SRT that re-parses identically."""
    cues = parse_srt(SAMPLE_SRT)
    output = write_srt(cues)
    reparsed = parse_srt(output)
    assert len(reparsed) == len(cues)
    for orig, reparse in zip(cues, reparsed):
        assert orig.start == reparse.start
        assert orig.end == reparse.end
        assert orig.text == reparse.text


def test_write_srt_reindexes():
    """write_srt assigns sequential indices starting from 1."""
    cues = [
        Cue(index=5, start="00:00:01,000", end="00:00:02,000", text="A"),
        Cue(index=99, start="00:00:03,000", end="00:00:04,000", text="B"),
    ]
    output = write_srt(cues)
    assert output.startswith("1\n")
    assert "\n2\n" in output


def test_parse_srt_with_bom():
    """Parse handles UTF-8 BOM."""
    srt_with_bom = "\ufeff1\n00:00:01,000 --> 00:00:02,000\nHello\n"
    cues = parse_srt(srt_with_bom)
    assert len(cues) == 1
    assert cues[0].text == "Hello"


def test_total_chars():
    """Total character count of all cue text."""
    cues = parse_srt(SAMPLE_SRT)
    total = sum(len(c.text) for c in cues)
    assert total > 0
```

**Step 2: Run tests to verify they fail**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/test_srt_parser.py -v`
Expected: FAIL (ImportError)

**Step 3: Write implementation**

```python
# automation/scripts/translation/srt_parser.py
"""Parse and write SRT subtitle files."""

import re
from dataclasses import dataclass
from typing import List

TIMESTAMP_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})"
)


@dataclass
class Cue:
    index: int
    start: str
    end: str
    text: str


def parse_srt(content: str) -> List[Cue]:
    """Parse SRT content into a list of Cue objects.

    Handles BOM, Windows line endings, and various spacing quirks.
    """
    # Strip BOM and normalize line endings
    content = content.lstrip("\ufeff").replace("\r\n", "\n").strip()
    if not content:
        return []

    cues = []
    # Split on blank lines (two or more newlines)
    blocks = re.split(r"\n\n+", content)

    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue

        # Find the timestamp line (may or may not have index before it)
        ts_line_idx = None
        for i, line in enumerate(lines):
            if TIMESTAMP_RE.search(line):
                ts_line_idx = i
                break

        if ts_line_idx is None:
            continue

        match = TIMESTAMP_RE.search(lines[ts_line_idx])
        start, end = match.group(1), match.group(2)

        # Index is the line before the timestamp (if present and numeric)
        index = 0
        if ts_line_idx > 0 and lines[ts_line_idx - 1].strip().isdigit():
            index = int(lines[ts_line_idx - 1].strip())

        # Text is everything after the timestamp line
        text_lines = lines[ts_line_idx + 1:]
        text = "\n".join(text_lines).strip()

        if text:
            cues.append(Cue(index=index, start=start, end=end, text=text))

    return cues


def write_srt(cues: List[Cue]) -> str:
    """Write cues to SRT format string with sequential indices."""
    blocks = []
    for i, cue in enumerate(cues, 1):
        blocks.append(f"{i}\n{cue.start} --> {cue.end}\n{cue.text}")
    return "\n\n".join(blocks) + "\n"
```

**Step 4: Run tests to verify they pass**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/test_srt_parser.py -v`
Expected: all 7 tests PASS

**Step 5: Commit**

```bash
git add automation/scripts/translation/srt_parser.py automation/scripts/translation/tests/test_srt_parser.py
git commit -m "feat(translation): SRT parser with roundtrip fidelity"
```

---

### Task 5: DeepL Client

**Files:**
- Create: `automation/scripts/translation/deepl_client.py`
- Create: `automation/scripts/translation/tests/test_deepl_client.py`

**Step 1: Write the failing tests**

```python
# automation/scripts/translation/tests/test_deepl_client.py
"""Tests for DeepL client (mocked — no real API calls)."""

from unittest.mock import MagicMock, patch
from translation.deepl_client import translate_texts, translate_srt_cues
from translation.srt_parser import Cue


def test_translate_texts_basic():
    """translate_texts sends text to DeepL and returns translations."""
    mock_translator = MagicMock()
    mock_result1 = MagicMock()
    mock_result1.text = "Hola, mundo!"
    mock_result2 = MagicMock()
    mock_result2.text = "Esto es una prueba."
    mock_translator.translate_text.return_value = [mock_result1, mock_result2]

    results = translate_texts(
        mock_translator,
        texts=["Hello, world!", "This is a test."],
        source_lang="EN",
        target_lang="ES",
    )
    assert results == ["Hola, mundo!", "Esto es una prueba."]
    mock_translator.translate_text.assert_called_once()


def test_translate_srt_cues():
    """translate_srt_cues returns new cues with translated text."""
    cues = [
        Cue(index=1, start="00:00:01,000", end="00:00:02,000", text="Hello"),
        Cue(index=2, start="00:00:03,000", end="00:00:04,000", text="World"),
    ]
    mock_translator = MagicMock()
    mock_r1 = MagicMock(text="Hola")
    mock_r2 = MagicMock(text="Mundo")
    mock_translator.translate_text.return_value = [mock_r1, mock_r2]

    translated, chars = translate_srt_cues(
        mock_translator, cues, source_lang="EN", target_lang="ES"
    )
    assert len(translated) == 2
    assert translated[0].text == "Hola"
    assert translated[0].start == "00:00:01,000"
    assert translated[1].text == "Mundo"
    assert chars == 10  # len("Hello") + len("World")


def test_translate_srt_cues_batching():
    """Large cue lists are batched to stay under size limit."""
    # Create 100 cues with 100-char text each (10KB total)
    cues = [
        Cue(index=i, start="00:00:01,000", end="00:00:02,000", text="A" * 100)
        for i in range(100)
    ]
    mock_translator = MagicMock()
    # Return matching number of results for each batch call
    def side_effect(texts, **kwargs):
        return [MagicMock(text=f"T{i}") for i in range(len(texts))]
    mock_translator.translate_text.side_effect = side_effect

    translated, chars = translate_srt_cues(
        mock_translator, cues, source_lang="EN", target_lang="ES",
        batch_size=4000,  # 4KB batches → ~40 cues per batch → 3 calls
    )
    assert len(translated) == 100
    assert chars == 10000
    assert mock_translator.translate_text.call_count >= 2


def test_translate_texts_empty():
    """translate_texts with empty list returns empty list."""
    mock_translator = MagicMock()
    results = translate_texts(mock_translator, [], "EN", "ES")
    assert results == []
    mock_translator.translate_text.assert_not_called()
```

**Step 2: Run tests to verify they fail**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/test_deepl_client.py -v`
Expected: FAIL (ImportError)

**Step 3: Write implementation**

```python
# automation/scripts/translation/deepl_client.py
"""DeepL API client for subtitle translation."""

import logging
from typing import List, Tuple

import deepl

from translation.srt_parser import Cue

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 4000  # chars per batch (~4KB)


def create_translator(api_key: str) -> deepl.Translator:
    """Create a DeepL Translator instance."""
    return deepl.Translator(api_key)


def get_usage(translator: deepl.Translator) -> dict:
    """Get current API usage stats."""
    usage = translator.get_usage()
    return {
        "character_count": usage.character.count if usage.character else 0,
        "character_limit": usage.character.limit if usage.character else 0,
    }


def translate_texts(
    translator: deepl.Translator,
    texts: List[str],
    source_lang: str,
    target_lang: str,
) -> List[str]:
    """Translate a list of texts via DeepL API. Returns translated strings."""
    if not texts:
        return []
    results = translator.translate_text(
        texts,
        source_lang=source_lang,
        target_lang=target_lang,
    )
    return [r.text for r in results]


def translate_srt_cues(
    translator: deepl.Translator,
    cues: List[Cue],
    source_lang: str,
    target_lang: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Tuple[List[Cue], int]:
    """Translate SRT cues via DeepL, batching to stay under size limits.

    Returns (translated_cues, total_chars_used).
    """
    if not cues:
        return [], 0

    # Build batches by character count
    batches = []
    current_batch = []
    current_size = 0
    for cue in cues:
        text_len = len(cue.text)
        if current_batch and current_size + text_len > batch_size:
            batches.append(current_batch)
            current_batch = []
            current_size = 0
        current_batch.append(cue)
        current_size += text_len
    if current_batch:
        batches.append(current_batch)

    total_chars = 0
    translated_cues = []

    for batch in batches:
        texts = [c.text for c in batch]
        total_chars += sum(len(t) for t in texts)
        translated_texts = translate_texts(
            translator, texts, source_lang, target_lang
        )
        for cue, translated_text in zip(batch, translated_texts):
            translated_cues.append(Cue(
                index=cue.index,
                start=cue.start,
                end=cue.end,
                text=translated_text,
            ))

    return translated_cues, total_chars
```

**Step 4: Run tests to verify they pass**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/test_deepl_client.py -v`
Expected: all 4 tests PASS

**Step 5: Commit**

```bash
git add automation/scripts/translation/deepl_client.py automation/scripts/translation/tests/test_deepl_client.py
git commit -m "feat(translation): DeepL client with batched cue translation"
```

---

### Task 6: Subtitle Scanner

**Files:**
- Create: `automation/scripts/translation/subtitle_scanner.py`
- Create: `automation/scripts/translation/tests/test_subtitle_scanner.py`

**Step 1: Write the failing tests**

```python
# automation/scripts/translation/tests/test_subtitle_scanner.py
"""Tests for subtitle scanner."""

import os
from translation.subtitle_scanner import (
    find_best_source_srt,
    find_missing_langs_on_disk,
    parse_missing_subtitles,
)


def test_parse_missing_subtitles_python_repr():
    """Parse Bazarr's Python-repr missing_subtitles format."""
    assert parse_missing_subtitles("['es']") == ["es"]
    assert parse_missing_subtitles("['en', 'es']") == ["en", "es"]
    assert parse_missing_subtitles("[]") == []


def test_parse_missing_subtitles_json():
    """Parse JSON-format missing_subtitles."""
    assert parse_missing_subtitles('["es"]') == ["es"]
    assert parse_missing_subtitles('["en", "es"]') == ["en", "es"]


def test_parse_missing_subtitles_with_forced():
    """Parse missing_subtitles with forced/hi suffixes."""
    result = parse_missing_subtitles("['es:forced', 'en:hi']")
    assert result == ["es:forced", "en:hi"]


def test_parse_missing_subtitles_empty():
    """Parse empty/null missing_subtitles."""
    assert parse_missing_subtitles("") == []
    assert parse_missing_subtitles(None) == []


def test_find_best_source_srt(tmp_path):
    """find_best_source_srt returns largest non-target, non-forced SRT."""
    stem = "Movie.2024.1080p"
    # Create SRTs of different sizes
    en_srt = tmp_path / f"{stem}.en.srt"
    en_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n" * 100)
    es_srt = tmp_path / f"{stem}.es.srt"
    es_srt.write_text("small")
    forced = tmp_path / f"{stem}.en.forced.srt"
    forced.write_text("forced sub")

    # Looking for French — should pick English (largest non-forced)
    result = find_best_source_srt(str(tmp_path), stem, target_lang="fr")
    assert result is not None
    assert result.endswith(".en.srt")


def test_find_best_source_srt_skips_target(tmp_path):
    """find_best_source_srt skips SRTs matching target language."""
    stem = "Movie"
    en_srt = tmp_path / f"{stem}.en.srt"
    en_srt.write_text("English text " * 50)
    es_srt = tmp_path / f"{stem}.es.srt"
    es_srt.write_text("Spanish text " * 100)  # larger but it's the target

    result = find_best_source_srt(str(tmp_path), stem, target_lang="es")
    assert result.endswith(".en.srt")


def test_find_best_source_srt_prefers_english(tmp_path):
    """find_best_source_srt prefers English source when sizes are similar."""
    stem = "Movie"
    en_srt = tmp_path / f"{stem}.en.srt"
    en_srt.write_text("A" * 1000)
    fr_srt = tmp_path / f"{stem}.fr.srt"
    fr_srt.write_text("B" * 1050)  # slightly larger but not English

    result = find_best_source_srt(str(tmp_path), stem, target_lang="es")
    # English preferred when within 20% size of largest
    assert result.endswith(".en.srt")


def test_find_best_source_srt_none(tmp_path):
    """find_best_source_srt returns None when no candidates exist."""
    result = find_best_source_srt(str(tmp_path), "nonexistent", "es")
    assert result is None


def test_find_missing_langs_on_disk(tmp_path):
    """find_missing_langs_on_disk returns profile langs without SRT on disk."""
    stem = "Movie"
    # English SRT exists, Spanish does not
    (tmp_path / f"{stem}.en.srt").write_text("English")
    profile_langs = ["en", "es", "fr"]
    missing = find_missing_langs_on_disk(str(tmp_path), stem, profile_langs)
    assert "es" in missing
    assert "fr" in missing
    assert "en" not in missing
```

**Step 2: Run tests to verify they fail**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/test_subtitle_scanner.py -v`
Expected: FAIL (ImportError)

**Step 3: Write implementation**

```python
# automation/scripts/translation/subtitle_scanner.py
"""Find missing subtitles and source SRTs for translation."""

import ast
import glob
import json
import logging
import os
import re
import sqlite3
from typing import List, Optional

log = logging.getLogger(__name__)


def parse_missing_subtitles(raw: Optional[str]) -> List[str]:
    """Parse Bazarr's missing_subtitles field (Python repr or JSON)."""
    if not raw or raw.strip() in ("", "[]"):
        return []
    raw = raw.strip()
    try:
        result = ast.literal_eval(raw)
        if isinstance(result, list):
            return [str(x) for x in result]
    except (ValueError, SyntaxError):
        pass
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(x) for x in result]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def find_best_source_srt(
    directory: str, stem: str, target_lang: str
) -> Optional[str]:
    """Find the best source SRT for translation.

    Picks the largest non-forced, non-target-language SRT.
    Prefers English when within 20% of the largest candidate.
    """
    pattern = os.path.join(glob.escape(directory), f"{glob.escape(stem)}.*.srt")
    candidates = []

    for path in glob.glob(pattern):
        basename = os.path.basename(path)
        # Extract language code: stem.LANG.srt or stem.LANG.forced.srt
        parts = basename[len(stem) + 1:].split(".")
        if not parts:
            continue
        lang = parts[0].lower()
        if lang == target_lang.lower():
            continue
        if "forced" in [p.lower() for p in parts]:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size == 0:
            continue
        candidates.append((path, lang, size))

    if not candidates:
        return None

    # Sort by size descending
    candidates.sort(key=lambda x: x[2], reverse=True)
    largest_size = candidates[0][2]

    # Prefer English if within 20% of largest
    for path, lang, size in candidates:
        if lang == "en" and size >= largest_size * 0.8:
            return path

    return candidates[0][0]


def find_missing_langs_on_disk(
    directory: str, stem: str, profile_langs: List[str]
) -> List[str]:
    """Return profile languages that don't have an SRT on disk."""
    missing = []
    for lang in profile_langs:
        srt_path = os.path.join(directory, f"{stem}.{lang}.srt")
        if not os.path.isfile(srt_path):
            missing.append(lang)
    return missing


def get_profile_langs(bazarr_db: str, profile_id: int) -> List[str]:
    """Get language codes from a Bazarr language profile."""
    conn = sqlite3.connect(bazarr_db)
    conn.execute("PRAGMA busy_timeout=30000")
    cursor = conn.execute(
        "SELECT items FROM table_languages_profiles WHERE profileId=? LIMIT 1",
        (profile_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row or not row[0]:
        return []
    try:
        items = json.loads(row[0])
        return [item["language"] for item in items
                if item.get("forced", "False") != "True"]
    except (json.JSONDecodeError, KeyError):
        return []


def scan_recent_missing(bazarr_db: str, since_minutes: int) -> list:
    """Scan Bazarr DB for media with missing subtitles and recent mtime.

    Returns list of dicts: {path, media_type, media_id, profile_id, missing_subtitles}
    """
    results = []
    conn = sqlite3.connect(bazarr_db)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row

    # Episodes
    cursor = conn.execute("""
        SELECT e.path, e.missing_subtitles, e.sonarrSeriesId, s.profileId
        FROM table_episodes e
        JOIN table_shows s ON s.sonarrSeriesId = e.sonarrSeriesId
        WHERE e.missing_subtitles != '[]'
          AND e.missing_subtitles IS NOT NULL
          AND e.path IS NOT NULL
          AND s.profileId IS NOT NULL
    """)
    for row in cursor.fetchall():
        path = row["path"]
        if not os.path.isfile(path):
            continue
        try:
            mtime_age = (os.path.getmtime(path))
            import time
            age_minutes = (time.time() - mtime_age) / 60
            if age_minutes > since_minutes:
                continue
        except OSError:
            continue
        results.append({
            "path": path,
            "media_type": "series",
            "media_id": row["sonarrSeriesId"],
            "profile_id": row["profileId"],
            "missing_subtitles": row["missing_subtitles"],
        })

    # Movies
    cursor = conn.execute("""
        SELECT m.path, m.missing_subtitles, m.radarrId, m.profileId
        FROM table_movies m
        WHERE m.missing_subtitles != '[]'
          AND m.missing_subtitles IS NOT NULL
          AND m.path IS NOT NULL
          AND m.profileId IS NOT NULL
    """)
    for row in cursor.fetchall():
        path = row["path"]
        if not os.path.isfile(path):
            continue
        try:
            import time
            age_minutes = (time.time() - os.path.getmtime(path)) / 60
            if age_minutes > since_minutes:
                continue
        except OSError:
            continue
        results.append({
            "path": path,
            "media_type": "movie",
            "media_id": row["radarrId"],
            "profile_id": row["profileId"],
            "missing_subtitles": row["missing_subtitles"],
        })

    conn.close()
    return results
```

**Step 4: Run tests to verify they pass**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/test_subtitle_scanner.py -v`
Expected: all 10 tests PASS

**Step 5: Commit**

```bash
git add automation/scripts/translation/subtitle_scanner.py automation/scripts/translation/tests/test_subtitle_scanner.py
git commit -m "feat(translation): subtitle scanner with Bazarr DB integration"
```

---

### Task 7: Discord Notifications

**Files:**
- Create: `automation/scripts/translation/discord.py`
- Create: `automation/scripts/translation/tests/test_discord.py`

**Step 1: Write the failing tests**

```python
# automation/scripts/translation/tests/test_discord.py
"""Tests for Discord notifications."""

from unittest.mock import patch, MagicMock
from translation.discord import notify_translations, notify_quota_warning


@patch("translation.discord.requests.post")
def test_notify_translations(mock_post):
    """notify_translations sends embed with summary."""
    mock_post.return_value = MagicMock(status_code=204)
    notify_translations(
        webhook_url="https://discord.com/api/webhooks/test",
        translated=[
            {"file": "Movie.mkv", "target": "es", "chars": 1500},
            {"file": "Show.S01E01.mkv", "target": "fr", "chars": 2000},
        ],
        failed=[],
        total_chars=3500,
        monthly_chars=45000,
    )
    mock_post.assert_called_once()
    payload = mock_post.call_args[1]["json"]
    assert "2 translated" in payload["embeds"][0]["description"].lower() or \
           len(payload["embeds"][0].get("fields", [])) > 0


@patch("translation.discord.requests.post")
def test_notify_translations_skips_empty(mock_post):
    """notify_translations does nothing when no translations."""
    notify_translations("https://hook", [], [], 0, 0)
    mock_post.assert_not_called()


@patch("translation.discord.requests.post")
def test_notify_quota_warning(mock_post):
    """notify_quota_warning sends red embed."""
    mock_post.return_value = MagicMock(status_code=204)
    notify_quota_warning("https://hook", 490000, 500000)
    mock_post.assert_called_once()
    payload = mock_post.call_args[1]["json"]
    assert payload["embeds"][0]["color"] == 15158332  # RED


def test_notify_no_webhook():
    """Functions handle empty webhook gracefully."""
    # Should not raise
    notify_translations("", [{"file": "x", "target": "es", "chars": 1}], [], 1, 1)
    notify_quota_warning("", 100, 500000)
```

**Step 2: Run tests to verify they fail**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/test_discord.py -v`
Expected: FAIL (ImportError)

**Step 3: Write implementation**

```python
# automation/scripts/translation/discord.py
"""Discord webhook notifications for translation results."""

import logging
from typing import List

import requests

log = logging.getLogger(__name__)

GREEN = 3066993
ORANGE = 15105570
YELLOW = 15844367
BLUE = 3447003
RED = 15158332


def _send_embed(webhook_url, title, description, color, fields=None):
    """Send a Discord embed message."""
    if not webhook_url:
        return
    embed = {"title": title, "description": description, "color": color}
    if fields:
        embed["fields"] = fields
    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        log.warning("Discord webhook failed: %s", e)


def notify_translations(
    webhook_url: str,
    translated: List[dict],
    failed: List[dict],
    total_chars: int,
    monthly_chars: int,
):
    """Send translation summary to Discord.

    translated: list of {file, target, chars}
    failed: list of {file, target, error}
    """
    if not translated and not failed:
        return
    if not webhook_url:
        return

    desc_parts = []
    if translated:
        desc_parts.append(f"**{len(translated)}** translated ({total_chars:,} chars)")
    if failed:
        desc_parts.append(f"**{len(failed)}** failed")
    desc_parts.append(f"Monthly usage: {monthly_chars:,} / 500,000 chars")

    fields = []
    if translated:
        lines = []
        for t in translated[:10]:
            fname = t["file"]
            if len(fname) > 40:
                fname = "..." + fname[-37:]
            lines.append(f"- `{fname}` → {t['target']} ({t['chars']:,} chars)")
        if len(translated) > 10:
            lines.append(f"...and {len(translated) - 10} more")
        fields.append({"name": "Translated", "value": "\n".join(lines), "inline": False})

    if failed:
        lines = []
        for f in failed[:5]:
            fname = f["file"]
            if len(fname) > 40:
                fname = "..." + fname[-37:]
            lines.append(f"- `{fname}` → {f['target']}: {f['error']}")
        fields.append({"name": "Failed", "value": "\n".join(lines), "inline": False})

    color = GREEN if not failed else ORANGE
    _send_embed(webhook_url, "DeepL Translation", "\n".join(desc_parts), color, fields)


def notify_quota_warning(webhook_url: str, chars_used: int, chars_limit: int):
    """Send quota warning/exceeded notification."""
    if not webhook_url:
        return
    pct = (chars_used / chars_limit * 100) if chars_limit > 0 else 100
    _send_embed(
        webhook_url,
        "DeepL Quota Warning",
        f"**{chars_used:,}** / {chars_limit:,} characters used ({pct:.0f}%)\n"
        "Translation paused until next month.",
        RED,
    )
```

**Step 4: Run tests to verify they pass**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/test_discord.py -v`
Expected: all 4 tests PASS

**Step 5: Commit**

```bash
git add automation/scripts/translation/discord.py automation/scripts/translation/tests/test_discord.py
git commit -m "feat(translation): Discord notifications for translation results"
```

---

### Task 8: CLI (translator.py)

**Files:**
- Create: `automation/scripts/translation/translator.py`
- Create: `automation/scripts/translation/tests/test_cli.py`

**Step 1: Write the failing tests**

```python
# automation/scripts/translation/tests/test_cli.py
"""Tests for translator CLI."""

import os
from unittest.mock import patch, MagicMock
from click.testing import CliRunner
from translation.translator import cli


@patch("translation.translator.create_translator")
@patch("translation.translator.scan_recent_missing")
def test_translate_since_no_results(mock_scan, mock_create, env_config, tmp_db, monkeypatch):
    """translate --since with no missing subs does nothing."""
    mock_scan.return_value = []
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    runner = CliRunner()
    result = runner.invoke(cli, ["translate", "--since", "60", "--state-dir", os.path.dirname(tmp_db)])
    assert result.exit_code == 0
    assert "no files" in result.output.lower() or "0" in result.output


@patch("translation.translator.translate_file")
def test_translate_file_mode(mock_translate_file, env_config, tmp_path, monkeypatch):
    """translate --file calls translate_file for a single path."""
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"\x00" * 100)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "translate", "--file", str(video),
        "--state-dir", str(tmp_path / "state"),
    ])
    assert result.exit_code == 0


def test_status_command(env_config, tmp_db, monkeypatch):
    """status command shows monthly usage."""
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    runner = CliRunner()
    result = runner.invoke(cli, ["status", "--state-dir", os.path.dirname(tmp_db)])
    assert result.exit_code == 0
    assert "chars" in result.output.lower() or "usage" in result.output.lower() or "0" in result.output


@patch("translation.translator.create_translator")
def test_usage_command(mock_create, env_config, monkeypatch):
    """usage command queries DeepL API."""
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    mock_translator = MagicMock()
    mock_usage = MagicMock()
    mock_usage.character = MagicMock(count=50000, limit=500000)
    mock_translator.get_usage.return_value = mock_usage
    mock_create.return_value = mock_translator
    runner = CliRunner()
    result = runner.invoke(cli, ["usage"])
    assert result.exit_code == 0
```

**Step 2: Run tests to verify they fail**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/test_cli.py -v`
Expected: FAIL (ImportError)

**Step 3: Write implementation**

```python
#!/usr/bin/env python3
# automation/scripts/translation/translator.py
"""DeepL subtitle translator CLI."""

import logging
import os
import sys

import click

from translation.config import (
    Config, load_config, DEEPL_LANG_MAP, DEEPL_SOURCE_LANG_MAP,
)
from translation.db import (
    init_db, record_translation, is_on_cooldown, get_monthly_chars,
    get_recent_translations,
)
from translation.deepl_client import create_translator, translate_srt_cues
from translation.discord import notify_translations, notify_quota_warning
from translation.srt_parser import parse_srt, write_srt
from translation.subtitle_scanner import (
    find_best_source_srt, find_missing_langs_on_disk, get_profile_langs,
    parse_missing_subtitles, scan_recent_missing,
)

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _db_path(state_dir: str) -> str:
    return os.path.join(state_dir, "translation_state.db")


def translate_file(cfg: Config, translator, media_path: str):
    """Translate missing subtitle languages for a single media file.

    Returns list of {file, target, chars} for successful translations
    and list of {file, target, error} for failures.
    """
    db_path = _db_path(cfg.state_dir)
    basename = os.path.basename(media_path)
    stem = os.path.splitext(basename)[0]
    directory = os.path.dirname(media_path)

    # Resolve profile for this file
    profile_id = _resolve_profile_for_path(cfg.bazarr_db, media_path)
    if profile_id is None:
        log.info("No profile found for %s, skipping", basename)
        return [], []

    profile_langs = get_profile_langs(cfg.bazarr_db, profile_id)
    if not profile_langs:
        log.info("Empty profile for %s, skipping", basename)
        return [], []

    missing = find_missing_langs_on_disk(directory, stem, profile_langs)
    if not missing:
        log.info("All profile langs present for %s", basename)
        return [], []

    translated = []
    failed = []

    for target_lang in missing:
        # Skip forced/hi variants for translation
        base_lang = target_lang.split(":")[0]

        if base_lang not in DEEPL_LANG_MAP:
            log.info("No DeepL mapping for '%s', skipping", base_lang)
            continue

        if is_on_cooldown(db_path, media_path, base_lang):
            log.info("Cooldown active for %s → %s, skipping", basename, base_lang)
            continue

        source_srt = find_best_source_srt(directory, stem, base_lang)
        if not source_srt:
            log.info("No source SRT for %s → %s", basename, base_lang)
            record_translation(db_path, media_path, "?", base_lang, 0, "no_source")
            continue

        # Detect source language from filename
        source_basename = os.path.basename(source_srt)
        source_lang_code = source_basename[len(stem) + 1:].split(".")[0].lower()
        deepl_source = DEEPL_SOURCE_LANG_MAP.get(source_lang_code)
        deepl_target = DEEPL_LANG_MAP[base_lang]

        if not deepl_source:
            log.info("No DeepL source mapping for '%s'", source_lang_code)
            continue

        try:
            # Read and parse source SRT
            with open(source_srt, "r", encoding="utf-8", errors="replace") as f:
                source_content = f.read()
            cues = parse_srt(source_content)
            if not cues:
                log.warning("Empty SRT: %s", source_srt)
                continue

            # Translate
            log.info("Translating %s: %s → %s (%d cues)",
                     basename, source_lang_code, base_lang, len(cues))
            translated_cues, chars_used = translate_srt_cues(
                translator, cues, deepl_source, deepl_target
            )

            # Write translated SRT
            output_path = os.path.join(directory, f"{stem}.{base_lang}.srt")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(write_srt(translated_cues))

            record_translation(db_path, media_path, source_lang_code,
                               base_lang, chars_used, "success")
            translated.append({
                "file": basename, "target": base_lang, "chars": chars_used
            })
            log.info("Wrote %s (%d chars)", output_path, chars_used)

        except Exception as e:
            error_msg = str(e)
            # Check for quota exceeded
            if "quota" in error_msg.lower() or "456" in error_msg:
                record_translation(db_path, media_path, source_lang_code,
                                   base_lang, 0, "quota_exceeded")
                failed.append({"file": basename, "target": base_lang,
                               "error": "quota exceeded"})
                raise  # Re-raise to stop all processing
            record_translation(db_path, media_path, source_lang_code,
                               base_lang, 0, f"error: {error_msg[:100]}")
            failed.append({"file": basename, "target": base_lang,
                           "error": error_msg[:100]})
            log.error("Translation failed %s → %s: %s", basename, base_lang, e)

    return translated, failed


def _resolve_profile_for_path(bazarr_db: str, media_path: str):
    """Resolve Bazarr profileId for a media file path."""
    import sqlite3
    if not os.path.isfile(bazarr_db):
        return None
    conn = sqlite3.connect(bazarr_db)
    conn.execute("PRAGMA busy_timeout=30000")

    # Try episodes first
    cursor = conn.execute(
        """SELECT s.profileId FROM table_episodes e
           JOIN table_shows s ON s.sonarrSeriesId = e.sonarrSeriesId
           WHERE e.path = ? LIMIT 1""",
        (media_path,),
    )
    row = cursor.fetchone()
    if row and row[0]:
        conn.close()
        return row[0]

    # Try movies
    cursor = conn.execute(
        "SELECT profileId FROM table_movies WHERE path = ? LIMIT 1",
        (media_path,),
    )
    row = cursor.fetchone()
    if row and row[0]:
        conn.close()
        return row[0]

    # Fallback: directory name match for series
    parent_dir = os.path.basename(os.path.dirname(os.path.dirname(media_path)))
    if parent_dir:
        cursor = conn.execute(
            "SELECT profileId FROM table_shows WHERE path LIKE ? LIMIT 1",
            (f"%{parent_dir}%",),
        )
        row = cursor.fetchone()
        if row and row[0]:
            conn.close()
            return row[0]

    # Fallback: directory name match for movies
    movie_dir = os.path.basename(os.path.dirname(media_path))
    if movie_dir:
        cursor = conn.execute(
            "SELECT profileId FROM table_movies WHERE path LIKE ? LIMIT 1",
            (f"%{movie_dir}%",),
        )
        row = cursor.fetchone()
        if row and row[0]:
            conn.close()
            return row[0]

    conn.close()
    return None


@click.group()
def cli():
    """DeepL subtitle translator."""
    pass


@cli.command()
@click.option("--since", type=int, default=None,
              help="Only process files modified in the last N minutes")
@click.option("--file", "file_path", type=str, default=None,
              help="Translate a single file")
@click.option("--state-dir", type=str, default=None)
@click.option("--bazarr-db", type=str, default=None)
def translate(since, file_path, state_dir, bazarr_db):
    """Translate missing subtitles via DeepL."""
    cfg = load_config(bazarr_db=bazarr_db, state_dir=state_dir)
    db = _db_path(cfg.state_dir)
    init_db(db)
    translator = create_translator(cfg.deepl_api_key)

    all_translated = []
    all_failed = []

    if file_path:
        # Single-file mode (import hook)
        if not os.path.isfile(file_path):
            log.error("File not found: %s", file_path)
            return
        t, f = translate_file(cfg, translator, file_path)
        all_translated.extend(t)
        all_failed.extend(f)

        # Trigger Bazarr rescan
        if all_translated and cfg.bazarr_api_key:
            _trigger_bazarr_rescan(cfg, file_path)
    elif since:
        # Cron mode — scan for recent missing
        results = scan_recent_missing(cfg.bazarr_db, since)
        if not results:
            click.echo(f"No files with missing subtitles in last {since} minutes")
            return
        click.echo(f"Found {len(results)} file(s) with missing subtitles")
        for item in results:
            try:
                t, f = translate_file(cfg, translator, item["path"])
                all_translated.extend(t)
                all_failed.extend(f)
                if t and cfg.bazarr_api_key:
                    _trigger_bazarr_rescan(cfg, item["path"])
            except Exception as e:
                if "quota" in str(e).lower():
                    log.error("Quota exceeded, stopping")
                    monthly = get_monthly_chars(db)
                    notify_quota_warning(cfg.discord_webhook_url, monthly, 500000)
                    break
                log.error("Error processing %s: %s", item["path"], e)
    else:
        click.echo("Must specify --since N or --file PATH")
        return

    # Summary
    total_chars = sum(t["chars"] for t in all_translated)
    monthly_chars = get_monthly_chars(db)
    click.echo(
        f"Done: {len(all_translated)} translated, {len(all_failed)} failed, "
        f"{total_chars:,} chars used ({monthly_chars:,} this month)"
    )

    # Discord notification
    if all_translated or all_failed:
        notify_translations(
            cfg.discord_webhook_url,
            all_translated, all_failed,
            total_chars, monthly_chars,
        )


@cli.command()
@click.option("--state-dir", type=str, default=None)
def status(state_dir):
    """Show translation status and recent activity."""
    cfg = load_config(state_dir=state_dir)
    db = _db_path(cfg.state_dir)
    init_db(db)

    monthly = get_monthly_chars(db)
    click.echo(f"Monthly usage: {monthly:,} / 500,000 chars ({monthly/5000:.1f}%)")

    recent = get_recent_translations(db, limit=10)
    if recent:
        click.echo(f"\nRecent translations ({len(recent)}):")
        for r in recent:
            click.echo(
                f"  {r['created_at']} | {r['status']:15s} | "
                f"{r['source_lang']}→{r['target_lang']} | "
                f"{r['chars_used']:>6,} chars | {os.path.basename(r['media_path'])}"
            )
    else:
        click.echo("\nNo translations recorded yet.")


@cli.command()
def usage():
    """Query DeepL API for remaining quota."""
    cfg = load_config()
    translator = create_translator(cfg.deepl_api_key)
    usage_data = translator.get_usage()
    if usage_data.character:
        count = usage_data.character.count
        limit = usage_data.character.limit
        click.echo(f"DeepL API usage: {count:,} / {limit:,} chars ({count/limit*100:.1f}%)")
    else:
        click.echo("Could not retrieve usage data")


def _trigger_bazarr_rescan(cfg: Config, media_path: str):
    """Trigger Bazarr scan-disk for the file's series/movie."""
    import requests as req
    import sqlite3

    headers = {"X-API-KEY": cfg.bazarr_api_key}
    conn = sqlite3.connect(cfg.bazarr_db)
    conn.execute("PRAGMA busy_timeout=30000")

    # Check if it's a series episode
    cursor = conn.execute(
        "SELECT sonarrSeriesId FROM table_episodes WHERE path = ? LIMIT 1",
        (media_path,),
    )
    row = cursor.fetchone()
    if row:
        series_id = row[0]
        conn.close()
        try:
            req.post(
                f"{cfg.bazarr_url}/api/series/action",
                headers=headers,
                json={"seriesid": series_id, "action": "scan-disk"},
                timeout=30,
            )
        except Exception as e:
            log.warning("Bazarr series rescan failed: %s", e)
        return

    # Check if it's a movie
    cursor = conn.execute(
        "SELECT radarrId FROM table_movies WHERE path = ? LIMIT 1",
        (media_path,),
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        movie_id = row[0]
        try:
            req.post(
                f"{cfg.bazarr_url}/api/movies/action",
                headers=headers,
                json={"radarrid": movie_id, "action": "scan-disk"},
                timeout=30,
            )
        except Exception as e:
            log.warning("Bazarr movie rescan failed: %s", e)


if __name__ == "__main__":
    cli()
```

**Step 4: Run tests to verify they pass**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/test_cli.py -v`
Expected: all 4 tests PASS

**Step 5: Commit**

```bash
git add automation/scripts/translation/translator.py automation/scripts/translation/tests/test_cli.py
git commit -m "feat(translation): CLI with translate, status, and usage commands"
```

---

### Task 9: Integration — .env + Import Hook + Cron

**Files:**
- Modify: `/config/berenstuff/.env` — add `DEEPL_API_KEY`
- Modify: `automation/scripts/subtitles/arr_profile_extract_on_import.sh` — replace Bazarr translate fallback
- Modify: `automation/configs/crontab.env-sourced` — add translation cron job

**Step 1: Add DEEPL_API_KEY to .env**

Append to `/config/berenstuff/.env`:
```bash
export DEEPL_API_KEY="BTLfvlVCXzEWM3eKq"
```

**Step 2: Replace broken Bazarr translate fallback in import hook**

In `automation/scripts/subtitles/arr_profile_extract_on_import.sh`, replace the translation fallback subshell (the `( sleep 5 ... ) >> "${LOG}" 2>&1 </dev/null & disown` block, approximately lines 267-305) with:

```bash
    # DeepL translation fallback — for profile languages still missing an
    # external SRT after Bazarr search, translate via DeepL from best
    # available source.  Runs in background to not block import.
    (
      sleep 10  # let Bazarr search complete + download first
      source /config/berenstuff/.env
      PYTHONPATH=/config/berenstuff/automation/scripts \
        python3 /config/berenstuff/automation/scripts/translation/translator.py \
        translate --file "$MEDIA_PATH" \
        >> /config/berenstuff/automation/logs/deepl_translate.log 2>&1
    ) </dev/null &
    disown
```

**Step 3: Add cron job**

Append to `automation/configs/crontab.env-sourced`:
```
# DeepL subtitle translation: translate missing profile-language SRTs every 30 min
*/30 * * * * /usr/bin/flock -n /tmp/deepl_translate.lock /bin/bash -c 'source /config/berenstuff/.env && PYTHONPATH=/config/berenstuff/automation/scripts python3 /config/berenstuff/automation/scripts/translation/translator.py translate --since 60' >> /config/berenstuff/automation/logs/deepl_translate.log 2>&1
```

**Step 4: Install crontab**

Run: `crontab /config/berenstuff/automation/configs/crontab.env-sourced`
Expected: no error

**Step 5: Verify crontab**

Run: `crontab -l | grep -c deepl`
Expected: 1

**Step 6: Commit**

```bash
git add automation/scripts/subtitles/arr_profile_extract_on_import.sh automation/configs/crontab.env-sourced
git commit -m "feat(translation): integrate DeepL into import hook + cron schedule"
```

Note: Do NOT commit `.env` — it contains secrets.

---

### Task 10: Smoke Test with Real File

**Step 1: Verify deepl SDK works with the API key**

Run:
```bash
cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -c "
from translation.deepl_client import create_translator
t = create_translator('$(grep DEEPL_API_KEY .env | cut -d= -f2 | tr -d '\"')')
u = t.get_usage()
print(f'Usage: {u.character.count}/{u.character.limit}')
"
```
Expected: `Usage: 0/500000` (or similar)

**Step 2: Test status command**

Run:
```bash
source /config/berenstuff/.env && PYTHONPATH=/config/berenstuff/automation/scripts \
  python3 /config/berenstuff/automation/scripts/translation/translator.py status
```
Expected: Shows "Monthly usage: 0 / 500,000 chars"

**Step 3: Test usage command**

Run:
```bash
source /config/berenstuff/.env && PYTHONPATH=/config/berenstuff/automation/scripts \
  python3 /config/berenstuff/automation/scripts/translation/translator.py usage
```
Expected: Shows DeepL API quota

**Step 4: Test translate on a real file with missing subs**

Find a file with missing subtitles and an existing English SRT, then run:
```bash
source /config/berenstuff/.env && PYTHONPATH=/config/berenstuff/automation/scripts \
  python3 /config/berenstuff/automation/scripts/translation/translator.py \
  translate --file "<PATH_TO_MEDIA_FILE>"
```
Expected: Creates `<stem>.<lang>.srt` with translated content, logs translation to state DB.

**Step 5: Run full test suite**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/translation/tests/ -v`
Expected: All tests pass

**Step 6: Final commit with any fixes**

```bash
git add -A automation/scripts/translation/
git commit -m "feat(translation): complete DeepL translation system"
```
