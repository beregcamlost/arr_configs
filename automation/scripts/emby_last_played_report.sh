#!/usr/bin/env bash
set -euo pipefail

EMBY_URL="${EMBY_URL:-http://127.0.0.1:8096}"
EMBY_API_KEY="${EMBY_API_KEY:-}"
PATH_PREFIX="${PATH_PREFIX:-/APPBOX_DATA/storage/media}"
OUTPUT_CSV="${OUTPUT_CSV:-/config/berenstuff/automation/logs/emby_last_played_report.csv}"
INCLUDE_NEVER=1
LIMIT=500
JOBS=4
REQUIRE_IDLE=1
IDLE_RETRIES=10
IDLE_RETRY_SLEEP=300

usage() {
  cat <<'EOF'
Usage: emby_last_played_report.sh [options]

Build a report for Movie/Episode items under a media path prefix showing
the last watched time across all Emby users (any user).

Options:
  --emby-url URL         Emby base URL (default: $EMBY_URL or http://127.0.0.1:8096)
  --api-key KEY          Emby API key (or set EMBY_API_KEY)
  --path-prefix PATH     Restrict to files under this prefix (default: /APPBOX_DATA/storage/media)
  --output-csv PATH      Output CSV path (default: /config/berenstuff/automation/logs/emby_last_played_report.csv)
  --exclude-never        Omit items never watched by any user
  --limit N              Emby page size per request (default: 500)
  --jobs N               Parallel user workers (default: 4)
  --allow-while-playing  Run even if Emby has active playback sessions
  --idle-retries N       Active-playback retries before abort (default: 10)
  --idle-retry-sleep S   Seconds between retries (default: 300)
  --help                 Show this help

Examples:
  EMBY_API_KEY=xxxxx emby_last_played_report.sh
  emby_last_played_report.sh --api-key xxxxx --path-prefix /APPBOX_DATA/storage/media
EOF
}

csv_escape() {
  local value="${1:-}"
  value="${value//\"/\"\"}"
  printf '"%s"' "$value"
}

iso_to_epoch() {
  local iso="${1:-}"
  [[ -z "$iso" ]] && return 1
  date -u -d "$iso" +%s 2>/dev/null
}

fetch_user_items() {
  local user_id="$1"
  local user_name="$2"
  local out_file="$3"
  local start_index=0
  local page_json item_count total_count

  : >"$out_file"
  echo "Processing user: ${user_name} (${user_id})"

  while true; do
    page_json="$(curl -fsS "${EMBY_URL}/Users/${user_id}/Items?api_key=${EMBY_API_KEY}&Recursive=true&IncludeItemTypes=Movie,Episode&Fields=Path&EnableUserData=true&StartIndex=${start_index}&Limit=${LIMIT}")"
    item_count="$(jq -r '.Items | length' <<<"$page_json")"
    total_count="$(jq -r '.TotalRecordCount // 0' <<<"$page_json")"

    if [[ "$item_count" -eq 0 ]]; then
      break
    fi

    jq -r --arg path_prefix "$PATH_PREFIX" '
      .Items[]
      | select((.Path // "") | startswith($path_prefix))
      | [
          (.Id // ""),
          (.Type // ""),
          (.Name // ""),
          (.Path // ""),
          (.SeriesName // ""),
          ((.ParentIndexNumber // "") | tostring),
          ((.IndexNumber // "") | tostring),
          (.UserData.LastPlayedDate // "")
        ]
      | @tsv
    ' <<<"$page_json" >>"$out_file"

    start_index=$((start_index + item_count))
    if [[ "$start_index" -ge "$total_count" ]]; then
      break
    fi
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --emby-url)
      EMBY_URL="${2:-}"
      shift 2
      ;;
    --api-key)
      EMBY_API_KEY="${2:-}"
      shift 2
      ;;
    --path-prefix)
      PATH_PREFIX="${2:-}"
      shift 2
      ;;
    --output-csv)
      OUTPUT_CSV="${2:-}"
      shift 2
      ;;
    --exclude-never)
      INCLUDE_NEVER=0
      shift
      ;;
    --limit)
      LIMIT="${2:-}"
      shift 2
      ;;
    --jobs)
      JOBS="${2:-}"
      shift 2
      ;;
    --allow-while-playing)
      REQUIRE_IDLE=0
      shift
      ;;
    --idle-retries)
      IDLE_RETRIES="${2:-}"
      shift 2
      ;;
    --idle-retry-sleep)
      IDLE_RETRY_SLEEP="${2:-}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$EMBY_API_KEY" ]]; then
  echo "EMBY_API_KEY is required (or pass --api-key)." >&2
  exit 1
fi

if ! [[ "$LIMIT" =~ ^[0-9]+$ ]] || [[ "$LIMIT" -le 0 ]]; then
  echo "--limit must be a positive integer." >&2
  exit 1
fi

if ! [[ "$JOBS" =~ ^[0-9]+$ ]] || [[ "$JOBS" -le 0 ]]; then
  echo "--jobs must be a positive integer." >&2
  exit 1
fi

if ! [[ "$IDLE_RETRIES" =~ ^[0-9]+$ ]] || [[ "$IDLE_RETRIES" -le 0 ]]; then
  echo "--idle-retries must be a positive integer." >&2
  exit 1
fi

if ! [[ "$IDLE_RETRY_SLEEP" =~ ^[0-9]+$ ]] || [[ "$IDLE_RETRY_SLEEP" -le 0 ]]; then
  echo "--idle-retry-sleep must be a positive integer." >&2
  exit 1
fi

EMBY_URL="${EMBY_URL%/}"
mkdir -p "$(dirname "$OUTPUT_CSV")"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

if [[ "$REQUIRE_IDLE" -eq 1 ]]; then
  for attempt in $(seq 1 "$IDLE_RETRIES"); do
    sessions_json="$(curl -fsS "${EMBY_URL}/Sessions?api_key=${EMBY_API_KEY}")"
    active_count="$(jq -r '
      [
        .[]
        | select((.NowPlayingItem? != null) and (((.PlayState // {}).IsPaused // false) == false))
      ] | length
    ' <<<"$sessions_json")"

    if [[ "$active_count" -eq 0 ]]; then
      break
    fi

    echo "Active playback detected on Emby (${active_count} sessions), attempt ${attempt}/${IDLE_RETRIES}." >&2
    if [[ "$attempt" -lt "$IDLE_RETRIES" ]]; then
      sleep "$IDLE_RETRY_SLEEP"
    else
      echo "Report aborted after ${IDLE_RETRIES} retries; Emby still active." >&2
      jq -r '
        .[]
        | select((.NowPlayingItem? != null) and (((.PlayState // {}).IsPaused // false) == false))
        | "- user=\(.UserName // "unknown") item=\(.NowPlayingItem.Name // "unknown") client=\(.Client // "unknown")"
      ' <<<"$sessions_json" >&2
      exit 2
    fi
  done
fi

declare -A ITEM_TYPE ITEM_NAME ITEM_PATH ITEM_SERIES ITEM_SEASON ITEM_EPISODE
declare -A ITEM_LAST_EPOCH ITEM_LAST_ISO

echo "Fetching Emby users from ${EMBY_URL}..."
users_json="$(curl -fsS "${EMBY_URL}/Users?api_key=${EMBY_API_KEY}")"
mapfile -t user_rows < <(jq -r '.[] | [.Id, (.Name // "unknown")] | @tsv' <<<"$users_json")

if [[ "${#user_rows[@]}" -eq 0 ]]; then
  echo "No users returned by Emby API." >&2
  exit 1
fi

declare -a pids=()
fetch_failed=0

for user_row in "${user_rows[@]}"; do
  IFS=$'\t' read -r user_id user_name <<<"$user_row"
  [[ -z "$user_id" ]] && continue
  out_file="${tmpdir}/user_${user_id}.tsv"
  fetch_user_items "$user_id" "$user_name" "$out_file" &
  pids+=("$!")

  while [[ "${#pids[@]}" -ge "$JOBS" ]]; do
    wait "${pids[0]}" || fetch_failed=1
    pids=("${pids[@]:1}")
  done
done

for pid in "${pids[@]}"; do
  wait "$pid" || fetch_failed=1
done

if [[ "$fetch_failed" -ne 0 ]]; then
  echo "At least one parallel user fetch failed. Aborting report." >&2
  exit 1
fi

for item_file in "${tmpdir}"/user_*.tsv; do
  [[ -f "$item_file" ]] || continue
  while IFS=$'\t' read -r item_id item_type item_name item_path series_name season_no episode_no last_played_iso; do
    [[ -z "$item_id" || -z "$item_path" ]] && continue

    ITEM_TYPE["$item_id"]="$item_type"
    ITEM_NAME["$item_id"]="$item_name"
    ITEM_PATH["$item_id"]="$item_path"
    ITEM_SERIES["$item_id"]="$series_name"
    ITEM_SEASON["$item_id"]="$season_no"
    ITEM_EPISODE["$item_id"]="$episode_no"

    if last_epoch="$(iso_to_epoch "$last_played_iso")"; then
      prev_epoch="${ITEM_LAST_EPOCH[$item_id]:-0}"
      if [[ "$last_epoch" -gt "$prev_epoch" ]]; then
        ITEM_LAST_EPOCH["$item_id"]="$last_epoch"
        ITEM_LAST_ISO["$item_id"]="$last_played_iso"
      fi
    fi
  done <"$item_file"
done

tmp_tsv="$(mktemp)"
now_epoch="$(date -u +%s)"

for item_id in "${!ITEM_PATH[@]}"; do
  last_epoch="${ITEM_LAST_EPOCH[$item_id]:-}"
  last_iso="${ITEM_LAST_ISO[$item_id]:-}"
  if [[ -n "$last_epoch" ]]; then
    days_since="$(((now_epoch - last_epoch) / 86400))"
    status="watched"
  else
    if [[ "$INCLUDE_NEVER" -eq 0 ]]; then
      continue
    fi
    days_since=""
    last_iso=""
    status="never_watched"
  fi

  printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
    "$status" \
    "${days_since}" \
    "$item_id" \
    "${ITEM_TYPE[$item_id]}" \
    "${ITEM_NAME[$item_id]}" \
    "${ITEM_SERIES[$item_id]}" \
    "${ITEM_SEASON[$item_id]}" \
    "${ITEM_EPISODE[$item_id]}" \
    "${ITEM_PATH[$item_id]}" \
    "$last_iso" >>"$tmp_tsv"
done

# Sort with never_watched first, then watched items by longest time since watched.
{
  printf "status,days_since_last_watched,item_id,item_type,title,series,season,episode,path,last_played_utc\n"
  sort -t $'\t' -k1,1 -k2,2nr "$tmp_tsv" | while IFS=$'\t' read -r status days_since item_id item_type item_name series_name season_no episode_no item_path last_iso; do
    csv_escape "$status"; printf ","
    csv_escape "$days_since"; printf ","
    csv_escape "$item_id"; printf ","
    csv_escape "$item_type"; printf ","
    csv_escape "$item_name"; printf ","
    csv_escape "$series_name"; printf ","
    csv_escape "$season_no"; printf ","
    csv_escape "$episode_no"; printf ","
    csv_escape "$item_path"; printf ","
    csv_escape "$last_iso"; printf "\n"
  done
} >"$OUTPUT_CSV"

total_items="$(wc -l <"$tmp_tsv" | tr -d ' ')"
never_items="$(awk -F $'\t' '$1=="never_watched"{c++} END{print c+0}' "$tmp_tsv")"
watched_items="$((total_items - never_items))"

rm -f "$tmp_tsv"

echo "Report written: ${OUTPUT_CSV}"
echo "Items in scope: ${total_items}"
echo "Watched at least once: ${watched_items}"
echo "Never watched: ${never_items}"
