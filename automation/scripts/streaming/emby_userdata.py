"""Back up and restore Emby watch state across a file move.

Emby identifies an item by its path: move a title to another folder and it
comes back as a brand new item with an empty history. Everyone's "watched"
marks, resume points and favourites are silently lost.

Matching the snapshot back to the new items is the whole difficulty, and two
obvious keys do not survive:

  * the ItemId is regenerated;
  * the ProviderIds can come back empty, because Emby re-identifies the item
    from scratch and the metadata download may lag or fail (19 series landed
    with no ids at all during the shelf migration);
  * even the name gets re-derived from the path, so "Rainbow (2010)" came
    back as "Rainbow".

What does survive is the title's own folder name - only its parent changed.
So every item is indexed under all three keys and looked up in that order.
"""
import json, urllib.parse, urllib.request

LIBRARY_TYPES = "Movie,Series,Episode"
STATE_FIELDS = ("Played", "PlayCount", "PlaybackPositionTicks", "IsFavorite", "LastPlayedDate")
ITEM_FIELDS = ("UserData,ProviderIds,ParentIndexNumber,IndexNumber,SeriesId,"
               "SeriesName,ProductionYear,Path")


def _api(base, key, path, **params):
    params["api_key"] = key
    url = "%s%s?%s" % (base, path, urllib.parse.urlencode(params))
    with urllib.request.urlopen(url, timeout=300) as r:
        return json.load(r)


def _post(base, key, path, payload=None):
    req = urllib.request.Request(
        "%s%s?api_key=%s" % (base, path, key), method="POST",
        data=json.dumps(payload).encode() if payload is not None else b"",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def stable_id(provider_ids):
    p = provider_ids or {}
    if p.get("Tmdb"):
        return "tmdb%s" % p["Tmdb"]
    if p.get("Tvdb"):
        return "tvdb%s" % p["Tvdb"]
    return None


def _folder(path, is_series):
    """The title's own folder: itself for a series, the parent for a movie."""
    if not path:
        return None
    parts = [x for x in path.split("/") if x]
    if is_series:
        return parts[-1] if parts else None
    return parts[-2] if len(parts) > 1 else None


def item_keys(item, series_folder):
    """Every key this item answers to, most reliable first."""
    keys = []
    t = item.get("Type")
    if t == "Movie":
        sid = stable_id(item.get("ProviderIds"))
        if sid:
            keys.append("movie:%s" % sid)
        f = _folder(item.get("Path"), is_series=False)
        if f:
            keys.append("movie|%s" % f)
    elif t == "Series":
        sid = stable_id(item.get("ProviderIds"))
        if sid:
            keys.append("series:%s" % sid)
        f = _folder(item.get("Path"), is_series=True)
        if f:
            keys.append("series|%s" % f)
    elif t == "Episode":
        s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
        sid = item.get("SeriesId")
        stable = series_folder.get(("id", sid))
        if stable:
            keys.append("ep:%s:%s:%s" % (stable, s, e))
        f = series_folder.get(sid)
        if f:
            keys.append("ep|%s|%s|%s" % (f, s, e))
    return keys


def _series_index(base, key, uid, lib_ids):
    """SeriesId -> folder name, and SeriesId -> stable provider id."""
    folder = {}
    for lib in lib_ids:
        r = _api(base, key, "/Items", ParentId=lib, Recursive="true", UserId=uid,
                 IncludeItemTypes="Series", Fields="ProviderIds,Path", Limit=20000)
        for s in r.get("Items", []):
            f = _folder(s.get("Path"), is_series=True)
            if f:
                folder[s["Id"]] = f
            sid = stable_id(s.get("ProviderIds"))
            if sid:
                folder[("id", s["Id"])] = sid
    return folder


def _has_state(d):
    return bool(d.get("Played") or d.get("PlaybackPositionTicks")
                or d.get("IsFavorite") or d.get("PlayCount"))


def collect(base, key, user_ids, lib_ids, only_series_ids=None, only_item_ids=None):
    """Snapshot watch state. Returns {user_id: {key: state}} with every key."""
    out = {}
    for uid in user_ids:
        series_folder = _series_index(base, key, uid, lib_ids)
        data = {}
        for lib in lib_ids:
            r = _api(base, key, "/Items", ParentId=lib, Recursive="true", UserId=uid,
                     IncludeItemTypes=LIBRARY_TYPES, Fields=ITEM_FIELDS, Limit=20000)
            for it in r.get("Items", []):
                d = it.get("UserData") or {}
                if not _has_state(d):
                    continue
                if only_item_ids is not None and it["Id"] not in only_item_ids \
                        and it.get("SeriesId") not in (only_series_ids or set()):
                    continue
                state = {f: d.get(f) for f in STATE_FIELDS}
                for k in item_keys(it, series_folder):
                    data[k] = state
        if data:
            out[uid] = data
    return out


def restore(base, key, backup, lib_ids, report=print):
    """Write the snapshot back. Returns (items_restored, entries_unclaimed).

    Walks the items that exist now and asks each one which of its keys the
    snapshot knows, rather than walking the snapshot: one item answers to
    several keys, so counting the other way round would report the same piece
    as missing once per key it did not need.
    """
    restored = 0
    unclaimed = 0
    for uid, data in backup.items():
        series_folder = _series_index(base, key, uid, lib_ids)
        claimed = set()
        seen_items = set()
        for lib in lib_ids:
            r = _api(base, key, "/Items", ParentId=lib, Recursive="true", UserId=uid,
                     IncludeItemTypes=LIBRARY_TYPES, Fields=ITEM_FIELDS, Limit=20000)
            for it in r.get("Items", []):
                if it["Id"] in seen_items:
                    continue
                keys = item_keys(it, series_folder)
                hit = next((k for k in keys if k in data), None)
                if not hit:
                    continue
                seen_items.add(it["Id"])
                claimed.update(k for k in keys if k in data)
                state = data[hit]
                payload = {f: state[f] for f in STATE_FIELDS if state.get(f) is not None}
                try:
                    _post(base, key, "/Users/%s/Items/%s/UserData" % (uid, it["Id"]), payload)
                    restored += 1
                except Exception as e:
                    report("      fallo %s: %s" % (hit, e))
        unclaimed += len(set(data) - claimed)
    return restored, unclaimed
