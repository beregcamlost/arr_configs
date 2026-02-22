#!/usr/bin/env bash
set -euo pipefail

LOCK_FILE="/tmp/extract_zh_chinese.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another instance is already running"; exit 0; }

DB="/opt/bazarr/data/db/bazarr.db"
LOG="/config/berenstuff/automation/logs/extract_zh_embedded_for_chinese_profiles.log"

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

# Find language profiles that include Chinese (zh or zt)
mapfile -t PROFILE_IDS < <(
  sqlite3 "$DB" "
    SELECT profileId
    FROM table_languages_profiles
    WHERE (items LIKE '%\"language\":\"zh\"%' OR items LIKE '%\"language\": \"zh\"%' OR items LIKE '%\"language\":\"zt\"%' OR items LIKE '%\"language\": \"zt\"%')
    ORDER BY profileId;
  "
)

if [ "${#PROFILE_IDS[@]}" -eq 0 ]; then
  log "No Chinese profiles found (zh/zt)."
  exit 0
fi

id_csv="$(IFS=,; echo "${PROFILE_IDS[*]}")"
log "Chinese profile IDs: $id_csv"

mapfile -t EPISODES < <(
  sqlite3 "$DB" "
    SELECT e.path
    FROM table_episodes e
    JOIN table_shows s ON s.sonarrSeriesId = e.sonarrSeriesId
    WHERE s.profileId IN ($id_csv)
      AND s.monitored = 'True'
      AND e.monitored = 'True'
    ORDER BY e.path;
  "
)

if [ "${#EPISODES[@]}" -eq 0 ]; then
  log "No monitored episodes found for Chinese profiles: $id_csv"
  exit 0
fi

log "Episodes to process: ${#EPISODES[@]}"

extract_stream() {
  local file="$1"
  local stream_index="$2"
  local out_file="$3"

  if [ -f "$out_file" ]; then
    log "SKIP exists: $out_file"
    return 0
  fi

  ffmpeg -nostdin -loglevel error -y -i "$file" -map "0:${stream_index}" -c:s srt "$out_file"
  log "WROTE: $out_file"
}

for file in "${EPISODES[@]}"; do
  if [ ! -f "$file" ]; then
    log "MISS file: $file"
    continue
  fi

  json="$(ffprobe -v error -print_format json -show_streams -select_streams s "$file" 2>/dev/null || true)"
  if [ -z "$json" ] || [ "$json" = "{}" ]; then
    log "NO-SUBS: $file"
    continue
  fi

  # Identify Chinese subtitle streams by language tag or title markers.
  # Classify to zt (Traditional) when markers indicate Hant/CHT/Traditional.
  forced_idx_zt="$(printf '%s' "$json" | jq -r '
    [ .streams[]
      | . as $s
      | ($s.tags.language // "") as $lang
      | ($s.tags.title // "") as $title
      | select(($lang | test("^(zho|chi|zh|zht|cht)$"; "i"))
               or ($title | test("(chinese|mandarin|cantonese|中文|國語|粵語|繁體|traditional|hant|cht)"; "i")))
      | select(($title | test("(繁體|traditional|hant|cht|big5)"; "i"))
               or ($lang | test("^(zht|cht)$"; "i")))
      | select((.disposition.forced // 0) == 1)
      | .index
    ] | first // empty
  ')"

  full_idx_zt="$(printf '%s' "$json" | jq -r '
    [ .streams[]
      | . as $s
      | ($s.tags.language // "") as $lang
      | ($s.tags.title // "") as $title
      | select(($lang | test("^(zho|chi|zh|zht|cht)$"; "i"))
               or ($title | test("(chinese|mandarin|cantonese|中文|國語|粵語|繁體|traditional|hant|cht)"; "i")))
      | select(($title | test("(繁體|traditional|hant|cht|big5)"; "i"))
               or ($lang | test("^(zht|cht)$"; "i")))
      | select((.disposition.forced // 0) == 0)
      | .index
    ] | first // empty
  ')"

  forced_idx_zh="$(printf '%s' "$json" | jq -r '
    [ .streams[]
      | . as $s
      | ($s.tags.language // "") as $lang
      | ($s.tags.title // "") as $title
      | select(($lang | test("^(zho|chi|zh|zhs|chs)$"; "i"))
               or ($title | test("(chinese|mandarin|cantonese|中文|国语|粤语|簡體|简体|simplified|hans|chs)"; "i")))
      | select((.disposition.forced // 0) == 1)
      | .index
    ] | first // empty
  ')"

  full_idx_zh="$(printf '%s' "$json" | jq -r '
    [ .streams[]
      | . as $s
      | ($s.tags.language // "") as $lang
      | ($s.tags.title // "") as $title
      | select(($lang | test("^(zho|chi|zh|zhs|chs)$"; "i"))
               or ($title | test("(chinese|mandarin|cantonese|中文|国语|粤语|簡體|简体|simplified|hans|chs)"; "i")))
      | select((.disposition.forced // 0) == 0)
      | .index
    ] | first // empty
  ')"

  # Generic Chinese fallback when no zt/zh classified index was found.
  generic_forced_idx="$(printf '%s' "$json" | jq -r '
    [ .streams[]
      | (.tags.language // "") as $lang
      | (.tags.title // "") as $title
      | select(($lang | test("^(zho|chi|zh|zht|cht|zhs|chs)$"; "i"))
               or ($title | test("(chinese|mandarin|cantonese|中文|國語|国语|粵語|粤语)"; "i")))
      | select((.disposition.forced // 0) == 1)
      | .index
    ] | first // empty
  ' 2>/dev/null || true)"

  generic_full_idx="$(printf '%s' "$json" | jq -r '
    [ .streams[]
      | (.tags.language // "") as $lang
      | (.tags.title // "") as $title
      | select(($lang | test("^(zho|chi|zh|zht|cht|zhs|chs)$"; "i"))
               or ($title | test("(chinese|mandarin|cantonese|中文|國語|国语|粵語|粤语)"; "i")))
      | select((.disposition.forced // 0) == 0)
      | .index
    ] | first // empty
  ')"

  base="${file%.*}"

  if [ -n "$forced_idx_zt" ]; then
    extract_stream "$file" "$forced_idx_zt" "$base.zt.forced.srt"
  fi
  if [ -n "$full_idx_zt" ]; then
    extract_stream "$file" "$full_idx_zt" "$base.zt.srt"
  fi

  if [ -n "$forced_idx_zh" ]; then
    extract_stream "$file" "$forced_idx_zh" "$base.zh.forced.srt"
  elif [ -z "$forced_idx_zt" ] && [ -n "$generic_forced_idx" ]; then
    extract_stream "$file" "$generic_forced_idx" "$base.zh.forced.srt"
  fi

  if [ -n "$full_idx_zh" ]; then
    extract_stream "$file" "$full_idx_zh" "$base.zh.srt"
  elif [ -z "$full_idx_zt" ] && [ -n "$generic_full_idx" ]; then
    extract_stream "$file" "$generic_full_idx" "$base.zh.srt"
  fi

done
