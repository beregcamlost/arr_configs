"""Discord webhook notifications for streaming checker."""

import logging
from datetime import datetime, timezone

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


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def send_embed(webhook_url, title, description, color, fields=None,
               footer=None, timestamp=True):
    """Send a Discord embed message.

    Args:
        webhook_url: Discord webhook URL
        title: Embed title
        description: Embed description text
        color: Embed color (integer)
        fields: Optional list of {name, value, inline} dicts
        footer: Optional footer text
        timestamp: If True, add current UTC timestamp
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
    if footer:
        embed["footer"] = {"text": footer}
    if timestamp:
        embed["timestamp"] = _now_iso()

    payload = {"embeds": [embed]}

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        log.warning("Discord webhook failed: %s", e)


def notify_scan_results(webhook_url, new_items, left_items, stats, stale_count=None):
    """Send scan results notification to Discord.

    Args:
        new_items: list of newly streaming items (dicts with title, year, provider_name, library, size_bytes)
        left_items: list of items that left streaming (dicts with title, provider_name)
        stats: dict with movies_checked, series_checked, matches_found, duration_seconds
        stale_count: optional number of items with no plays in 90+ days
    """
    if not new_items and not left_items:
        return

    # Build description with stats summary
    movies = stats.get("movies_checked", 0)
    series = stats.get("series_checked", 0)
    matches = stats.get("matches_found", 0)
    duration = stats.get("duration_seconds", 0)

    desc_lines = []
    desc_lines.append(
        f"Scanned **{movies}** movies + **{series}** series "
        f"({matches} active matches)"
    )

    # Size summary of new items
    if new_items:
        new_size = sum(it.get("size_bytes", 0) or 0 for it in new_items)
        desc_lines.append(
            f"📥 **{len(new_items)}** newly streaming ({format_size(new_size)})"
        )
    if left_items:
        desc_lines.append(f"📤 **{len(left_items)}** left streaming")
    if stale_count:
        desc_lines.append(f"⏳ **{stale_count}** items with no plays in 90+ days")

    fields = []

    if new_items:
        # Group by provider
        by_provider = {}
        for item in new_items:
            pname = item.get("provider_name", "Unknown")
            by_provider.setdefault(pname, []).append(item)

        for provider, items in sorted(by_provider.items()):
            psize = sum(it.get("size_bytes", 0) or 0 for it in items)
            lines = []
            for it in items[:10]:
                size = format_size(it.get("size_bytes", 0))
                lines.append(
                    f"• `{it['title']}` ({it.get('year', '?')}) "
                    f"[{it.get('library', '?')}] {size}"
                )
            if len(items) > 10:
                lines.append(f"…and {len(items) - 10} more")
            fields.append({
                "name": f"📺 New on {provider} — {len(items)} items, {format_size(psize)}",
                "value": "\n".join(lines),
                "inline": False,
            })

    if left_items:
        lines = []
        for it in sorted(left_items, key=lambda x: x.get("title", ""))[:10]:
            lines.append(
                f"• `{it['title']}` — was on {it.get('provider_name', '?')}"
            )
        if len(left_items) > 10:
            lines.append(f"…and {len(left_items) - 10} more")
        fields.append({
            "name": f"🚫 Left Streaming ({len(left_items)})",
            "value": "\n".join(lines),
            "inline": False,
        })

    # Determine color and emoji
    if new_items and left_items:
        color = ORANGE
        emoji = "🔄"
    elif new_items:
        color = GREEN
        emoji = "📺"
    else:
        color = YELLOW
        emoji = "📤"

    title = f"{emoji} Streaming Scan"
    footer = f"Duration: {duration:.1f}s"

    send_embed(webhook_url, title, "\n".join(desc_lines), color, fields, footer)


def notify_deletion(webhook_url, deleted_items, total_freed_bytes):
    """Send deletion confirmation notification to Discord.

    Args:
        deleted_items: list of dicts with title, year, provider_name, library, size_bytes
        total_freed_bytes: total bytes freed by deletions
    """
    if not deleted_items:
        return

    # Group by library
    by_library = {}
    for it in deleted_items:
        lib = it.get("library", "unknown")
        by_library.setdefault(lib, []).append(it)

    desc_lines = [
        f"🗑 Deleted **{len(deleted_items)}** items, "
        f"freed **{format_size(total_freed_bytes)}**"
    ]

    fields = []

    # Items list — split into chunks to fit Discord's 1024-char field limit
    all_lines = []
    for it in deleted_items:
        size = format_size(it.get("size_bytes", 0) or 0)
        all_lines.append(f"• `{it['title']}` ({it.get('year', '?')}) {size}")
    fields.extend(_chunk_item_lines(all_lines, "Deleted Items"))

    # Stats row
    fields.append({
        "name": "Space Freed",
        "value": format_size(total_freed_bytes),
        "inline": True,
    })
    fields.append({
        "name": "Items",
        "value": str(len(deleted_items)),
        "inline": True,
    })
    if by_library:
        lib_parts = [f"{lib}: {len(items)}" for lib, items in sorted(by_library.items())]
        fields.append({
            "name": "By Library",
            "value": ", ".join(lib_parts),
            "inline": True,
        })

    send_embed(
        webhook_url,
        "🗑 Streaming Checker — Deletions",
        "\n".join(desc_lines),
        RED,
        fields,
        footer=f"Freed {format_size(total_freed_bytes)} total",
    )


def _chunk_item_lines(lines, field_label):
    """Split item lines into multiple fields respecting Discord's 1024-char limit."""
    fields = []
    chunk = []
    chunk_len = 0
    chunk_idx = 0
    for line in lines:
        line_len = len(line) + 1
        if chunk and chunk_len + line_len > 1000:
            label = field_label if chunk_idx == 0 else "\u2800"
            fields.append({"name": label, "value": "\n".join(chunk), "inline": False})
            chunk = []
            chunk_len = 0
            chunk_idx += 1
        chunk.append(line)
        chunk_len += line_len
    if chunk:
        label = field_label if chunk_idx == 0 else "\u2800"
        fields.append({"name": label, "value": "\n".join(chunk), "inline": False})
    return fields


def notify_stale_flag(webhook_url, flagged_items, unflagged_items):
    """Notify about newly stale-flagged and unflagged items."""
    if not flagged_items and not unflagged_items:
        return
    total_size = sum(i.get("size_bytes", 0) for i in flagged_items)
    desc_parts = []
    if flagged_items:
        desc_parts.append(f"⚠️ Newly flagged: **{len(flagged_items)}** ({format_size(total_size)})")
    if unflagged_items:
        desc_parts.append(f"✅ Unflagged (watched): **{len(unflagged_items)}**")
    fields = []
    if flagged_items:
        lines = [f"`{i['title']} ({i.get('year', '?')})`" for i in flagged_items[:15]]
        if len(flagged_items) > 15:
            lines.append(f"…and {len(flagged_items) - 15} more")
        fields.append({"name": "⚠️ Flagged for Removal (90d stale + streaming)", "value": "\n".join(lines), "inline": False})
    if unflagged_items:
        lines = [f"`{i['title']} ({i.get('year', '?')})`" for i in unflagged_items[:10]]
        if len(unflagged_items) > 10:
            lines.append(f"…and {len(unflagged_items) - 10} more")
        fields.append({"name": "✅ Unflagged (recently watched)", "value": "\n".join(lines), "inline": False})
    send_embed(
        webhook_url,
        title="⏳ Stale Content Flagging — Tier 1.5",
        description="\n".join(desc_parts),
        color=ORANGE if flagged_items else GREEN,
        fields=fields,
        footer="Grace period: 15 days before deletion",
    )


def notify_stale_cleanup(webhook_url, deleted_items, kept_items,
                         freed_bytes, no_play_days, min_size_gb):
    """Send stale library cleanup notification to Discord.

    Args:
        deleted_items: items that were auto-deleted (> min_size_gb)
        kept_items: items that are stale but below size threshold (report only)
        freed_bytes: total bytes freed
        no_play_days: the N-day threshold used
        min_size_gb: the size threshold for auto-deletion
    """
    if not deleted_items and not kept_items:
        return

    total_stale = len(deleted_items) + len(kept_items)
    kept_size = sum(it.get("size_bytes", 0) or 0 for it in kept_items)

    desc = (
        f"Found **{total_stale}** items not played in **{no_play_days}** days"
    )

    fields = [
        {"name": "🗑 Auto-Deleted", "value": f"{len(deleted_items)} (>{min_size_gb} GB)", "inline": True},
        {"name": "📋 Report Only", "value": f"{len(kept_items)} (<={min_size_gb} GB)", "inline": True},
        {"name": "💾 Space Freed", "value": format_size(freed_bytes), "inline": True},
    ]
    if kept_items:
        fields.append(
            {"name": "📦 Kept Size", "value": format_size(kept_size), "inline": True},
        )

    # Deleted items list
    if deleted_items:
        lines = []
        for it in sorted(deleted_items, key=lambda x: x.get("size_bytes", 0) or 0, reverse=True):
            size = format_size(it.get("size_bytes", 0) or 0)
            lines.append(f"• `{it['title']}` ({it.get('year', '?')}) [{it.get('library', '?')}] {size}")
        fields.extend(_chunk_item_lines(lines, "🗑 Deleted"))

    # Kept items list (report only)
    if kept_items:
        lines = []
        for it in sorted(kept_items, key=lambda x: x.get("size_bytes", 0) or 0, reverse=True):
            size = format_size(it.get("size_bytes", 0) or 0)
            lines.append(f"• `{it['title']}` ({it.get('year', '?')}) [{it.get('library', '?')}] {size}")
        fields.extend(_chunk_item_lines(lines, "📋 Kept (below threshold)"))

    color = RED if deleted_items else YELLOW
    send_embed(
        webhook_url,
        "🧹 Library Stale Cleanup",
        desc,
        color,
        fields,
        footer=f"Threshold: {no_play_days}d no play, >{min_size_gb} GB auto-delete",
    )
