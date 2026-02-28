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
