#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# sqlite_backup.sh — Online SQLite backup with rotation and git push
# ---------------------------------------------------------------------------

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LOCK_FILE="/tmp/sqlite_backup.lock"
readonly LOG_FILE="/config/berenstuff/automation/logs/sqlite_backup.log"
readonly BACKUP_ROOT="/config/berenstuff/arr-backups"
readonly CURRENT_DIR="${BACKUP_ROOT}/current"
readonly ARCHIVE_DIR="${BACKUP_ROOT}/archive"
readonly KEEP_ARCHIVES=8
readonly GIT_REMOTE="origin"
readonly GIT_BRANCH="main"

# ---------------------------------------------------------------------------
# Database definitions — parallel arrays: source path and backup name
# ---------------------------------------------------------------------------
readonly DB_PATHS=(
    "/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db"
    "/APPBOX_DATA/storage/.streaming-checker-state/streaming_state.db"
    "/APPBOX_DATA/storage/.translation-state/translation_state.db"
    "/APPBOX_DATA/storage/.subtitle-quality-state/subtitle_quality_state.db"
    "/APPBOX_DATA/storage/.subtitle-dedupe-state/subtitle_dedupe.db"
)
readonly DB_NAMES=(
    "codec_state"
    "streaming_state"
    "translation_state"
    "subtitle_quality_state"
    "subtitle_dedupe_state"
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log() {
    printf '%s [sqlite-backup] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

die() {
    log "FATAL: $*"
    exit 1
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
file_size_human() {
    local path="$1"
    local size
    size="$(stat -c%s "$path" 2>/dev/null)" || { printf 'unknown'; return; }
    if (( size >= 1048576 )); then
        printf '%dMB' "$(( size / 1048576 ))"
    elif (( size >= 1024 )); then
        printf '%dKB' "$(( size / 1024 ))"
    else
        printf '%dB' "$size"
    fi
}

today() {
    date -u '+%Y-%m-%d'
}

# ---------------------------------------------------------------------------
# Core backup logic for a single database
# ---------------------------------------------------------------------------
backup_db() {
    local src="$1"
    local name="$2"
    local today_str
    today_str="$(today)"
    local dest_current="${CURRENT_DIR}/${name}.bkp"
    local dest_archive="${ARCHIVE_DIR}/${today_str}/${name}.bkp"

    if [[ ! -f "$src" ]]; then
        log "WARN: skipping '${name}' — source not found: ${src}"
        return 1
    fi

    mkdir -p "${ARCHIVE_DIR}/${today_str}"

    log "Backing up '${name}' from ${src}"
    sqlite3 "$src" ".backup '${dest_current}'" </dev/null
    cp "${dest_current}" "${dest_archive}"

    local sz
    sz="$(file_size_human "$dest_current")"
    log "  done: ${dest_current} (${sz})"
    return 0
}

# ---------------------------------------------------------------------------
# Rotate old archive directories, keeping the newest KEEP_ARCHIVES entries
# ---------------------------------------------------------------------------
rotate_archives() {
    local count
    count="$(find "$ARCHIVE_DIR" -mindepth 1 -maxdepth 1 -type d | wc -l)"
    if (( count <= KEEP_ARCHIVES )); then
        return
    fi

    local to_delete=$(( count - KEEP_ARCHIVES ))
    log "Rotating archives: removing ${to_delete} oldest (keeping ${KEEP_ARCHIVES})"

    find "$ARCHIVE_DIR" -mindepth 1 -maxdepth 1 -type d \
        | sort \
        | head -n "$to_delete" \
        | while IFS= read -r old_dir; do
            log "  removing archive: ${old_dir}"
            rm -rf "$old_dir"
        done
}

# ---------------------------------------------------------------------------
# Git commit and push — non-fatal on failure
# ---------------------------------------------------------------------------
git_push() {
    local today_str
    today_str="$(today)"
    local commit_msg="backup: ${today_str}"

    log "Committing backups to git"
    (
        cd "$BACKUP_ROOT"
        git add -A
        if git diff --cached --quiet; then
            log "  git: nothing new to commit"
            return
        fi
        git commit -m "$commit_msg"
        log "  git: committed '${commit_msg}'"

        if git push "$GIT_REMOTE" "$GIT_BRANCH"; then
            log "  git: pushed to ${GIT_REMOTE}/${GIT_BRANCH}"
        else
            log "WARN: git push failed (non-fatal)"
        fi
    ) || log "WARN: git operations failed (non-fatal)"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    # Acquire lock — exit silently if already running
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        log "Already running (lock held: ${LOCK_FILE}), exiting"
        exit 0
    fi

    log "=== SQLite backup started ==="

    mkdir -p "$CURRENT_DIR" "$ARCHIVE_DIR"

    local backed_up=0
    local skipped=0
    local total_size_bytes=0

    for i in "${!DB_PATHS[@]}"; do
        local src="${DB_PATHS[$i]}"
        local name="${DB_NAMES[$i]}"

        if backup_db "$src" "$name"; then
            (( backed_up++ )) || true
            local dest="${CURRENT_DIR}/${name}.bkp"
            local sz
            sz="$(stat -c%s "$dest" 2>/dev/null)" || sz=0
            (( total_size_bytes += sz )) || true
        else
            (( skipped++ )) || true
        fi
    done

    rotate_archives
    git_push

    local total_human
    if (( total_size_bytes >= 1048576 )); then
        total_human="$(( total_size_bytes / 1048576 ))MB"
    elif (( total_size_bytes >= 1024 )); then
        total_human="$(( total_size_bytes / 1024 ))KB"
    else
        total_human="${total_size_bytes}B"
    fi

    log "=== Summary: ${backed_up} backed up, ${skipped} skipped, total size: ${total_human} ==="
}

main "$@"
