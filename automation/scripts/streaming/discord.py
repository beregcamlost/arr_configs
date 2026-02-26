"""Discord webhook notifications for streaming checker."""

import logging

import requests

log = logging.getLogger(__name__)

# Discord embed colors
GREEN = 3066993
ORANGE = 15105570
YELLOW = 15844367
BLUE = 3447003
RED = 15158332


def format_size(size_bytes):
    """Format bytes to human-readable string."""
    if size_bytes is None or size_bytes == 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def send_embed(webhook_url, title, description, color, fields=None):
    """Send a Discord embed message.

    Args:
        webhook_url: Discord webhook URL
        title: Embed title
        description: Embed description text
        color: Embed color (integer)
        fields: Optional list of {name, value, inline} dicts
    """
    if not webhook_url:
        log.debug("No Discord webhook URL configured, skipping notification")
        return

    embed = {
        "title": title,
        "description": description,
        "color": color,
    }
    if fields:
        embed["fields"] = fields

    payload = {"embeds": [embed]}

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        log.warning("Discord webhook failed: %s", e)


def notify_scan_results(webhook_url, new_items, left_items, stats):
    """Send scan results notification to Discord.

    Args:
        new_items: list of newly streaming items (dicts with title, year, provider_name, library, size_bytes)
        left_items: list of items that left streaming (dicts with title, provider_name)
        stats: dict with movies_checked, series_checked, matches_found, duration_seconds
    """
    if not new_items and not left_items:
        return

    parts = []
    parts.append(
        f"Checked {stats.get('movies_checked', 0)} movies + "
        f"{stats.get('series_checked', 0)} series in "
        f"{stats.get('duration_seconds', 0):.1f}s"
    )

    fields = []

    if new_items:
        # Group by provider
        by_provider = {}
        for item in new_items:
            pname = item.get("provider_name", "Unknown")
            by_provider.setdefault(pname, []).append(item)

        for provider, items in by_provider.items():
            lines = []
            for it in items[:15]:  # Cap at 15 per provider to fit Discord limits
                size = format_size(it.get("size_bytes", 0))
                lines.append(f"- {it['title']} ({it.get('year', '?')}) [{it.get('library', '?')}] {size}")
            if len(items) > 15:
                lines.append(f"...and {len(items) - 15} more")
            fields.append({
                "name": f"New on {provider} ({len(items)})",
                "value": "\n".join(lines),
                "inline": False,
            })

    if left_items:
        lines = []
        for it in left_items[:10]:
            lines.append(f"- {it['title']} (was on {it.get('provider_name', '?')})")
        if len(left_items) > 10:
            lines.append(f"...and {len(left_items) - 10} more")
        fields.append({
            "name": f"Left Streaming ({len(left_items)})",
            "value": "\n".join(lines),
            "inline": False,
        })

    color = GREEN if new_items and not left_items else ORANGE if left_items else YELLOW
    send_embed(webhook_url, "Streaming Availability Scan", "\n".join(parts), color, fields)


def notify_deletion(webhook_url, deleted_items, total_freed_bytes):
    """Send deletion confirmation notification to Discord.

    Args:
        deleted_items: list of dicts with title, year, provider_name
        total_freed_bytes: total bytes freed by deletions
    """
    if not deleted_items:
        return

    lines = []
    for it in deleted_items[:20]:
        lines.append(f"- {it['title']} ({it.get('year', '?')})")
    if len(deleted_items) > 20:
        lines.append(f"...and {len(deleted_items) - 20} more")

    description = "\n".join(lines)
    fields = [
        {
            "name": "Space Freed",
            "value": format_size(total_freed_bytes),
            "inline": True,
        },
        {
            "name": "Items Deleted",
            "value": str(len(deleted_items)),
            "inline": True,
        },
    ]

    send_embed(webhook_url, "Streaming Checker — Deletions", description, BLUE, fields)
