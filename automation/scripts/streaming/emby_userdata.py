"""Back up and restore Emby watch state across a file move.

Emby identifies an item by its path: move a title to another folder and it
comes back as a brand new item with an empty history. Everyone's "watched"
marks, resume points and favourites are silently lost. So we snapshot that
state under keys that survive the move (TMDB/TVDB ids, plus season/episode
numbers) and write it back once Emby has re-indexed the new location.
"""
import json, os, urllib.parse, urllib.request

LIBRARY_TYPES = "Movie,Series,Episode"
STATE_FIELDS = ("Played", "PlayCount", "PlaybackPositionTicks", "IsFavorite", "LastPlayedDate")


def _api(base, key, path, **params):
    params["api_key"] = key
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.load(r)


def _post(base, key, path, payload=None):
    url = f"{base}{path}?api_key={key}"
    req = urllib.request.Request(
        url, method="POST",
        data=json.dumps(payload).encode() if payload is not None else b"",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def stable_id(provider_ids):
    """A key that survives a move — the file path does not."""
    p = provider_ids or {}
    if p.get("Tmdb"):
        return f"tmdb{p['Tmdb']}"
    if p.get("Tvdb"):
        return f"tvdb{p['Tvdb']}"
    return None


def item_key(item, series_key_by_id):
    t = item.get("Type")
    if t == "Movie":
        sid = stable_id(item.get("ProviderIds"))
        return f"movie:{sid}" if sid else None
    if t == "Series":
        sid = stable_id(item.get("ProviderIds"))
        return f"series:{sid}" if sid else None
    if t == "Episode":
        sid = series_key_by_id.get(item.get("SeriesId"))
        if not sid:
            return None
        return f"ep:{sid}:{item.get('ParentIndexNumber')}:{item.get('IndexNumber')}"
    return None


def _has_state(d):
    return bool(d.get("Played") or d.get("PlaybackPositionTicks")
                or d.get("IsFavorite") or d.get("PlayCount"))


def collect(base, key, user_ids, lib_ids, only_series_ids=None, only_item_ids=None):
    """Snapshot watch state. Returns {user_id: {stable_key: state}}."""
    out = {}
    for uid in user_ids:
        # map SeriesId -> stable id first, so episodes get a durable key
        series_key = {}
        for lib in lib_ids:
            r = _api(base, key, "/Items", ParentId=lib, Recursive="true", UserId=uid,
                     IncludeItemTypes="Series", Fields="ProviderIds", Limit=20000)
            for s in r.get("Items", []):
                sid = stable_id(s.get("ProviderIds"))
                if sid:
                    series_key[s["Id"]] = sid
        data = {}
        for lib in lib_ids:
            r = _api(base, key, "/Items", ParentId=lib, Recursive="true", UserId=uid,
                     IncludeItemTypes=LIBRARY_TYPES,
                     Fields="UserData,ProviderIds,ParentIndexNumber,IndexNumber,SeriesId",
                     Limit=20000)
            for it in r.get("Items", []):
                d = it.get("UserData") or {}
                if not _has_state(d):
                    continue
                if only_item_ids is not None and it["Id"] not in only_item_ids \
                        and it.get("SeriesId") not in (only_series_ids or set()):
                    continue
                k = item_key(it, series_key)
                if k:
                    data[k] = {f: d.get(f) for f in STATE_FIELDS}
        if data:
            out[uid] = data
    return out


def restore(base, key, backup, lib_ids, report=print):
    """Write the snapshot back. Returns (applied, missing)."""
    applied = missing = 0
    for uid, data in backup.items():
        series_key, current = {}, {}
        for lib in lib_ids:
            r = _api(base, key, "/Items", ParentId=lib, Recursive="true", UserId=uid,
                     IncludeItemTypes="Series", Fields="ProviderIds", Limit=20000)
            for s in r.get("Items", []):
                sid = stable_id(s.get("ProviderIds"))
                if sid:
                    series_key[s["Id"]] = sid
        for lib in lib_ids:
            r = _api(base, key, "/Items", ParentId=lib, Recursive="true", UserId=uid,
                     IncludeItemTypes=LIBRARY_TYPES,
                     Fields="ProviderIds,ParentIndexNumber,IndexNumber,SeriesId",
                     Limit=20000)
            for it in r.get("Items", []):
                k = item_key(it, series_key)
                if k:
                    current.setdefault(k, it["Id"])
        for k, state in data.items():
            item_id = current.get(k)
            if not item_id:
                missing += 1
                continue
            payload = {f: state.get(f) for f in STATE_FIELDS if state.get(f) is not None}
            try:
                _post(base, key, f"/Users/{uid}/Items/{item_id}/UserData", payload)
                applied += 1
            except Exception as e:
                missing += 1
                report(f"      fallo {k}: {e}")
    return applied, missing
