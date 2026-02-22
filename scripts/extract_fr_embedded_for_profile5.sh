#!/usr/bin/env bash
set -euo pipefail

DB="/opt/bazarr/data/db/bazarr.db"

mapfile -t EPISODES < <(
  sqlite3 "$DB" "
    SELECT e.path
    FROM table_episodes e
    JOIN table_shows s ON s.sonarrSeriesId = e.sonarrSeriesId
    WHERE s.profileId = 5
      AND s.monitored = 'True'
      AND e.monitored = 'True'
    ORDER BY e.path;
  "
)

if [ "${#EPISODES[@]}" -eq 0 ]; then
  echo "No monitored episodes found for profileId=5"
  exit 0
fi

extract_stream() {
  local file="$1"
  local stream_index="$2"
  local out_file="$3"

  if [ -f "$out_file" ]; then
    echo "SKIP exists: $out_file"
    return 0
  fi

  ffmpeg -nostdin -loglevel error -y -i "$file" -map "0:${stream_index}" -c:s srt "$out_file"
  echo "WROTE: $out_file"
}

for file in "${EPISODES[@]}"; do
  if [ ! -f "$file" ]; then
    echo "MISS file: $file"
    continue
  fi

  json="$(ffprobe -v error -print_format json -show_streams -select_streams s "$file" 2>/dev/null || true)"
  if [ -z "$json" ] || [ "$json" = "{}" ]; then
    echo "NO-SUBS: $file"
    continue
  fi

  # Use title/name heuristic because these files often have language=und.
  forced_idx="$(printf '%s' "$json" | jq -r '
    [ .streams[]
      | select(((.tags.title // .tags.language // "") | test("(^|[^a-z])(fr|french)([^a-z]|$)"; "i")))
      | select((.disposition.forced // 0) == 1)
      | .index
    ] | first // empty
  ')"

  full_idx="$(printf '%s' "$json" | jq -r '
    [ .streams[]
      | select(((.tags.title // .tags.language // "") | test("(^|[^a-z])(fr|french)([^a-z]|$)"; "i")))
      | select((.disposition.forced // 0) == 0)
      | .index
    ] | first // empty
  ')"

  base="${file%.*}"

  if [ -n "$forced_idx" ]; then
    extract_stream "$file" "$forced_idx" "$base.fr.forced.srt"
  fi

  if [ -n "$full_idx" ]; then
    extract_stream "$file" "$full_idx" "$base.fr.srt"
  fi

  if [ -z "$forced_idx" ] && [ -z "$full_idx" ]; then
    echo "NO-FR-MATCH: $file"
  fi
done
