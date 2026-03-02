"""Discord webhook notifications for translation results."""

import logging
from datetime import datetime, timezone
from typing import List

import requests

log = logging.getLogger(__name__)

GREEN = 3066993
ORANGE = 15105570
YELLOW = 15844367
BLUE = 3447003
RED = 15158332


def _send_embed(
    webhook_url, title, description, color, fields=None, footer=None
):
    """Send a Discord embed message."""
    if not webhook_url:
        return
    embed = {"title": title, "description": description, "color": color}
    if fields:
        embed["fields"] = fields
    embed["footer"] = {"text": footer or "DeepL Translation"}
    embed["timestamp"] = datetime.now(timezone.utc).isoformat()
    try:
        requests.post(
            webhook_url,
            json={"embeds": [embed]},
            timeout=20,
        )
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

    fields = [
        {"name": "✅ Translated", "value": str(len(translated)), "inline": True},
        {"name": "❌ Failed", "value": str(len(failed)), "inline": True},
        {"name": "📊 Chars Used", "value": f"{total_chars:,}", "inline": True},
        {"name": "📈 Monthly", "value": f"{monthly_chars:,} / 500,000", "inline": True},
    ]

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
    _send_embed(webhook_url, "🌐 DeepL Translation", "", color, fields)


def notify_quota_warning(webhook_url: str, chars_used: int, chars_limit: int):
    """Send quota warning/exceeded notification."""
    if not webhook_url:
        return
    pct = (chars_used / chars_limit * 100) if chars_limit > 0 else 100
    fields = [
        {"name": "📊 Characters Used", "value": f"{chars_used:,}", "inline": True},
        {"name": "📏 Limit", "value": f"{chars_limit:,}", "inline": True},
        {"name": "📈 Usage", "value": f"{pct:.0f}%", "inline": True},
    ]
    _send_embed(
        webhook_url,
        "⚠️ DeepL Quota Warning",
        "Translation paused until next month.",
        RED,
        fields,
    )
