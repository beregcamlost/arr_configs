"""Tests for per-provider monthly budget caps (Fix 2).

Key invariant: exhausting one provider's budget must not uncap (chars_remaining=None)
the remaining providers.
"""

import os
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, call
import translation.translator as translator_mod
from translation.db import init_db, record_translation, get_monthly_chars, get_daily_requests
from translation.config import Config, PROVIDER_GEMINI


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(deepl_key="test:fx", google_enabled=True, gemini_keys=None):
    return Config(
        deepl_api_keys=[deepl_key] if deepl_key else [],
        google_translate_enabled=google_enabled,
        gemini_api_keys=gemini_keys or [],
        state_dir="/tmp/test-state",
        bazarr_db="/tmp/test-bazarr.db",
    )


def _db_path(tmp_path):
    db = str(tmp_path / "state" / "translation_state.db")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    init_db(db)
    return db


# ---------------------------------------------------------------------------
# Fix 2: per-provider budget isolation
# ---------------------------------------------------------------------------

class TestPerProviderBudgetFlags:
    """Budget checks set per-provider flags independently."""

    def test_deepl_budget_exceeded_sets_only_deepl_flag(self, tmp_path, monkeypatch):
        """When DeepL exceeds budget, only _deepl_quota_exceeded is set."""
        from click.testing import CliRunner
        from translation.translator import cli

        db = _db_path(tmp_path)
        # Seed DeepL usage above 400k default CLI budget
        record_translation(db, "/p/v.mkv", "en", "es", 450_000, "success", "deepl")

        monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
        monkeypatch.setenv("GEMINI_API_KEYS", "gem-key")
        monkeypatch.setenv("GEMINI_MONTHLY_BUDGET", "500000")
        monkeypatch.setenv("GOOGLE_MONTHLY_BUDGET", "500000")

        with patch("translation.translator.scan_recent_missing", return_value=[]), \
             patch("translation.translator.deepl_get_usage",
                   return_value={"character_count": 0, "character_limit": 500000}), \
             patch("translation.gemini_client.reset_exhausted_keys"), \
             patch("translation.translator.deepl_reset_exhausted_keys"):
            runner = CliRunner()
            runner.invoke(cli, [
                "translate", "--since", "60",
                "--monthly-budget", "400000",
                "--state-dir", str(tmp_path / "state"),
            ])

        # DeepL must be flagged; Gemini and Google must NOT be flagged
        assert translator_mod._deepl_quota_exceeded is True
        assert translator_mod._gemini_quota_exceeded is False
        assert translator_mod._google_quota_exceeded is False

    def test_gemini_budget_exceeded_sets_only_gemini_flag(self, tmp_path, monkeypatch):
        """When Gemini exceeds budget, only _gemini_quota_exceeded is set."""
        from click.testing import CliRunner
        from translation.translator import cli

        db = _db_path(tmp_path)
        record_translation(db, "/p/v.mkv", "en", "es", 600_000, "success", "gemini")

        monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
        monkeypatch.setenv("GEMINI_API_KEYS", "gem-key")
        monkeypatch.setenv("GEMINI_MONTHLY_BUDGET", "500000")
        monkeypatch.setenv("GOOGLE_MONTHLY_BUDGET", "500000")

        with patch("translation.translator.scan_recent_missing", return_value=[]), \
             patch("translation.translator.deepl_get_usage",
                   return_value={"character_count": 0, "character_limit": 500000}), \
             patch("translation.gemini_client.reset_exhausted_keys"), \
             patch("translation.translator.deepl_reset_exhausted_keys"):
            runner = CliRunner()
            runner.invoke(cli, [
                "translate", "--since", "60",
                "--monthly-budget", "400000",
                "--state-dir", str(tmp_path / "state"),
            ])

        assert translator_mod._gemini_quota_exceeded is True
        assert translator_mod._deepl_quota_exceeded is False
        assert translator_mod._google_quota_exceeded is False

    def test_google_budget_exceeded_sets_only_google_flag(self, tmp_path, monkeypatch):
        """When Google exceeds budget, only _google_quota_exceeded is set."""
        from click.testing import CliRunner
        from translation.translator import cli

        db = _db_path(tmp_path)
        record_translation(db, "/p/v.mkv", "en", "es", 600_000, "success", "google")

        monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
        monkeypatch.setenv("GEMINI_API_KEYS", "gem-key")
        monkeypatch.setenv("GEMINI_MONTHLY_BUDGET", "500000")
        monkeypatch.setenv("GOOGLE_MONTHLY_BUDGET", "500000")

        with patch("translation.translator.scan_recent_missing", return_value=[]), \
             patch("translation.translator.deepl_get_usage",
                   return_value={"character_count": 0, "character_limit": 500000}), \
             patch("translation.gemini_client.reset_exhausted_keys"), \
             patch("translation.translator.deepl_reset_exhausted_keys"):
            runner = CliRunner()
            runner.invoke(cli, [
                "translate", "--since", "60",
                "--monthly-budget", "400000",
                "--state-dir", str(tmp_path / "state"),
            ])

        assert translator_mod._google_quota_exceeded is True
        assert translator_mod._deepl_quota_exceeded is False
        assert translator_mod._gemini_quota_exceeded is False

    def test_deepl_budget_exceeded_max_chars_honored(self, tmp_path, monkeypatch):
        """When DeepL budget is exceeded and --max-chars is set, chars_remaining equals max-chars.

        This is the core regression test for the old bug: the old code set
        chars_remaining=None when _budget_exceeded=True, silently discarding any
        user-supplied --max-chars and uncapping all remaining providers.
        """
        from click.testing import CliRunner
        from translation.translator import cli

        db = _db_path(tmp_path)
        # Seed DeepL over budget
        record_translation(db, "/p/v.mkv", "en", "es", 450_000, "success", "deepl")

        # Create a real video file so --file mode works
        video = tmp_path / "Movie.mkv"
        video.write_bytes(b"\x00")

        monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
        monkeypatch.setenv("GEMINI_MONTHLY_BUDGET", "500000")
        monkeypatch.setenv("GOOGLE_MONTHLY_BUDGET", "500000")

        calls = []

        def capture_translate_file(cfg, path, chars_remaining=None,
                                   google_translator=None, gemini_daily_budget=None):
            calls.append(chars_remaining)
            return [], []

        with patch("translation.translator.translate_file", side_effect=capture_translate_file), \
             patch("translation.translator.deepl_get_usage",
                   return_value={"character_count": 0, "character_limit": 500000}), \
             patch("translation.gemini_client.reset_exhausted_keys"), \
             patch("translation.translator.deepl_reset_exhausted_keys"):
            runner = CliRunner()
            runner.invoke(cli, [
                "translate", "--file", str(video),
                "--monthly-budget", "400000",
                "--max-chars", "50000",
                "--state-dir", str(tmp_path / "state"),
            ])

        # With DeepL budget exceeded but --max-chars 50000 provided, chars_remaining
        # must be 50000, not None. Old code would override to None.
        assert len(calls) == 1
        assert calls[0] == 50000, (
            f"Expected chars_remaining=50000, got {calls[0]}: "
            "DeepL budget exhaustion must not override user-specified --max-chars"
        )

    def test_mixed_provider_rows_independent_caps(self, tmp_path, monkeypatch):
        """Mixed provider rows: each provider's cap checked from its own slice only."""
        db = _db_path(tmp_path)
        # DeepL: 10k (under 400k budget)
        record_translation(db, "/p/v1.mkv", "en", "es", 10_000, "success", "deepl")
        # Gemini: 200k (under 500k budget)
        record_translation(db, "/p/v2.mkv", "en", "fr", 200_000, "success", "gemini")
        # Google: 600k (over 500k budget)
        record_translation(db, "/p/v3.mkv", "en", "de", 600_000, "success", "google")

        # Verify scoped queries return correct slices
        assert get_monthly_chars(db, provider="deepl") == 10_000
        assert get_monthly_chars(db, provider="gemini") == 200_000
        assert get_monthly_chars(db, provider="google") == 600_000
        # Total must include all three
        assert get_monthly_chars(db) == 810_000


class TestHasGoogleBudgetFlag:
    """_has_google() must respect _google_quota_exceeded flag."""

    def test_has_google_false_when_flag_set(self):
        translator_mod._google_quota_exceeded = True
        cfg = _make_cfg(google_enabled=True)
        assert translator_mod._has_google(cfg) is False
        translator_mod._google_quota_exceeded = False  # cleanup

    def test_has_google_true_when_enabled_and_flag_clear(self):
        translator_mod._google_quota_exceeded = False
        cfg = _make_cfg(google_enabled=True)
        assert translator_mod._has_google(cfg) is True

    def test_has_google_false_when_disabled_regardless_of_flag(self):
        translator_mod._google_quota_exceeded = False
        cfg = _make_cfg(google_enabled=False)
        assert translator_mod._has_google(cfg) is False


class TestPermanentSkipInTranslateFile:
    """is_permanently_failed is checked before is_on_cooldown in translate_file."""

    @patch("translation.translator._translate_cues_with_fallback")
    @patch("translation.translator._resolve_profile_for_path")
    @patch("translation.translator.get_profile_langs")
    @patch("translation.translator.find_missing_langs_on_disk")
    @patch("translation.translator.find_best_source_srt")
    @patch("translation.translator.record_translation")
    def test_permanent_skip_bypasses_cooldown_and_translation(
        self, mock_record, mock_source, mock_missing,
        mock_profile_langs, mock_profile, mock_translate, tmp_path,
    ):
        """Files with NoneType history are skipped without touching cooldown or translation."""
        translator_mod._deepl_quota_exceeded = False
        cfg = _make_cfg()
        cfg.state_dir = str(tmp_path / "state")
        os.makedirs(cfg.state_dir, exist_ok=True)
        db = os.path.join(cfg.state_dir, "translation_state.db")
        init_db(db)
        # Seed a NoneType failure in the DB
        record_translation(
            db, "/path/Movie.mkv", "en", "es", 0,
            "error: 'NoneType' object has no attribute 'encode'",
        )

        video = tmp_path / "Movie.mkv"
        video.touch()

        mock_profile.return_value = 1
        mock_profile_langs.return_value = ["es"]
        mock_missing.return_value = ["es"]
        # find_best_source_srt should never be reached
        mock_source.return_value = None

        from translation.translator import translate_file
        t, f = translate_file(cfg, "/path/Movie.mkv")

        assert t == []
        assert f == []
        # Translation must never have been attempted
        mock_translate.assert_not_called()

    @patch("translation.translator._translate_cues_with_fallback")
    @patch("translation.translator._resolve_profile_for_path")
    @patch("translation.translator.get_profile_langs")
    @patch("translation.translator.find_missing_langs_on_disk")
    @patch("translation.translator.find_best_source_srt")
    @patch("translation.translator.is_on_cooldown")
    @patch("translation.translator.record_translation")
    def test_transient_error_not_permanently_skipped(
        self, mock_record, mock_cooldown, mock_source, mock_missing,
        mock_profile_langs, mock_profile, mock_translate, tmp_path,
    ):
        """A file with only a transient (non-NoneType) error is NOT permanently skipped."""
        translator_mod._deepl_quota_exceeded = False
        cfg = _make_cfg()
        cfg.state_dir = str(tmp_path / "state")
        os.makedirs(cfg.state_dir, exist_ok=True)
        db = os.path.join(cfg.state_dir, "translation_state.db")
        init_db(db)
        # Seed only a transient error
        record_translation(db, "/path/Movie.mkv", "en", "es", 0, "error: timeout")

        video = tmp_path / "Movie.mkv"
        video.touch()

        mock_profile.return_value = 1
        mock_profile_langs.return_value = ["es"]
        mock_missing.return_value = ["es"]
        # Cooldown not active — let it proceed to translation attempt
        mock_cooldown.return_value = False
        mock_source.return_value = None  # no source SRT => no_source record

        from translation.translator import translate_file
        t, f = translate_file(cfg, "/path/Movie.mkv")

        # Translation was not called (no source), but the file was NOT permanently skipped —
        # is_on_cooldown was still consulted
        mock_cooldown.assert_called()
        mock_translate.assert_not_called()


# ---------------------------------------------------------------------------
# Daily Gemini request budget
# ---------------------------------------------------------------------------

def _insert_with_timestamp(db, media_path, provider, created_at_iso):
    """Insert a translation_log row with an explicit created_at timestamp."""
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO translation_log
           (media_path, source_lang, target_lang, chars_used, status, created_at, provider)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (media_path, "en", "es", 1000, "success", created_at_iso, provider),
    )
    conn.commit()
    conn.close()


class TestGetDailyRequests:
    """get_daily_requests counts only today's rows for the specified provider."""

    def test_counts_todays_rows_for_provider(self, tmp_db):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _insert_with_timestamp(tmp_db, "/a.mkv", "gemini", today)
        _insert_with_timestamp(tmp_db, "/b.mkv", "gemini", today)
        assert get_daily_requests(tmp_db, "gemini") == 2

    def test_excludes_other_providers(self, tmp_db):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _insert_with_timestamp(tmp_db, "/a.mkv", "gemini", today)
        _insert_with_timestamp(tmp_db, "/b.mkv", "deepl", today)
        _insert_with_timestamp(tmp_db, "/c.mkv", "google", today)
        assert get_daily_requests(tmp_db, "gemini") == 1
        assert get_daily_requests(tmp_db, "deepl") == 1

    def test_excludes_yesterday_rows(self, tmp_db):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _insert_with_timestamp(tmp_db, "/old.mkv", "gemini", yesterday)
        _insert_with_timestamp(tmp_db, "/new.mkv", "gemini", today)
        # Only today's row counts
        assert get_daily_requests(tmp_db, "gemini") == 1

    def test_fresh_day_resets_count(self, tmp_db):
        """Simulating a new day: only today's rows count, prior days are invisible."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(5):
            _insert_with_timestamp(tmp_db, f"/old{i}.mkv", "gemini", two_days_ago)
        # No rows today yet
        assert get_daily_requests(tmp_db, "gemini") == 0
        # Add one today
        _insert_with_timestamp(tmp_db, "/today.mkv", "gemini", today)
        assert get_daily_requests(tmp_db, "gemini") == 1

    def test_returns_zero_when_no_rows(self, tmp_db):
        assert get_daily_requests(tmp_db, "gemini") == 0


class TestGeminiDailyBudgetFallthrough:
    """When daily budget is hit, Gemini is skipped and the flag is set."""

    def _make_cfg_gemini(self, tmp_path):
        cfg = _make_cfg(deepl_key="test:fx", google_enabled=True, gemini_keys=["gem-key"])
        cfg.state_dir = str(tmp_path / "state")
        os.makedirs(cfg.state_dir, exist_ok=True)
        init_db(os.path.join(cfg.state_dir, "translation_state.db"))
        return cfg

    def test_gemini_skipped_when_daily_budget_reached(self, tmp_path):
        """translate_file sets _gemini_quota_exceeded when daily budget is exhausted."""
        translator_mod._gemini_quota_exceeded = False
        translator_mod._deepl_quota_exceeded = False

        cfg = self._make_cfg_gemini(tmp_path)
        db = os.path.join(cfg.state_dir, "translation_state.db")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Seed 5 gemini rows today — exceeds budget of 5
        for i in range(5):
            _insert_with_timestamp(db, f"/f{i}.mkv", "gemini", today)

        with patch("translation.translator._resolve_profile_for_path", return_value=None):
            from translation.translator import translate_file
            translate_file(cfg, "/any.mkv", gemini_daily_budget=5)

        assert translator_mod._gemini_quota_exceeded is True

    def test_gemini_not_skipped_when_under_budget(self, tmp_path):
        """translate_file does NOT set flag when today's count is below budget."""
        translator_mod._gemini_quota_exceeded = False
        translator_mod._deepl_quota_exceeded = False

        cfg = self._make_cfg_gemini(tmp_path)
        db = os.path.join(cfg.state_dir, "translation_state.db")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Seed 3 gemini rows today — under budget of 9
        for i in range(3):
            _insert_with_timestamp(db, f"/f{i}.mkv", "gemini", today)

        with patch("translation.translator._resolve_profile_for_path", return_value=None):
            from translation.translator import translate_file
            translate_file(cfg, "/any.mkv", gemini_daily_budget=9)

        assert translator_mod._gemini_quota_exceeded is False

    def test_daily_budget_zero_disables_gemini(self, tmp_path):
        """Budget of 0 means Gemini is disabled immediately (0 >= 0)."""
        translator_mod._gemini_quota_exceeded = False
        translator_mod._deepl_quota_exceeded = False

        cfg = self._make_cfg_gemini(tmp_path)

        with patch("translation.translator._resolve_profile_for_path", return_value=None):
            from translation.translator import translate_file
            translate_file(cfg, "/any.mkv", gemini_daily_budget=0)

        assert translator_mod._gemini_quota_exceeded is True

    def test_daily_budget_none_skips_check(self, tmp_path):
        """gemini_daily_budget=None means no daily check — flag stays clear."""
        translator_mod._gemini_quota_exceeded = False
        translator_mod._deepl_quota_exceeded = False

        cfg = self._make_cfg_gemini(tmp_path)
        db = os.path.join(cfg.state_dir, "translation_state.db")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Seed 100 gemini rows — would exceed any reasonable budget
        for i in range(100):
            _insert_with_timestamp(db, f"/f{i}.mkv", "gemini", today)

        with patch("translation.translator._resolve_profile_for_path", return_value=None):
            from translation.translator import translate_file
            translate_file(cfg, "/any.mkv", gemini_daily_budget=None)

        # Flag must NOT be set — budget=None means uncapped
        assert translator_mod._gemini_quota_exceeded is False

    def test_cli_passes_daily_budget_to_translate_file(self, tmp_path, monkeypatch):
        """The translate CLI command reads GEMINI_DAILY_REQUESTS_BUDGET and passes it down."""
        from click.testing import CliRunner
        from translation.translator import cli

        db_dir = tmp_path / "state"
        db_dir.mkdir()
        db = str(db_dir / "translation_state.db")
        init_db(db)

        monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
        monkeypatch.setenv("GEMINI_API_KEYS", "gem-key")
        monkeypatch.setenv("GEMINI_DAILY_REQUESTS_BUDGET", "7")
        monkeypatch.setenv("GEMINI_MONTHLY_BUDGET", "500000")
        monkeypatch.setenv("GOOGLE_MONTHLY_BUDGET", "500000")

        captured = {}

        def capture_translate_file(cfg, path, chars_remaining=None,
                                   google_translator=None, gemini_daily_budget=None):
            captured["gemini_daily_budget"] = gemini_daily_budget
            return [], []

        video = tmp_path / "Movie.mkv"
        video.write_bytes(b"\x00")

        with patch("translation.translator.translate_file", side_effect=capture_translate_file), \
             patch("translation.translator.deepl_get_usage",
                   return_value={"character_count": 0, "character_limit": 500000}), \
             patch("translation.gemini_client.reset_exhausted_keys"), \
             patch("translation.translator.deepl_reset_exhausted_keys"):
            runner = CliRunner()
            runner.invoke(cli, [
                "translate", "--file", str(video),
                "--state-dir", str(db_dir),
            ])

        assert captured.get("gemini_daily_budget") == 7
