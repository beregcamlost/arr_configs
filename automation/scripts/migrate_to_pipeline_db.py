#!/usr/bin/env python3
"""
migrate_to_pipeline_db.py — Phase 6 I-A: consolidate 10 non-codec SQLite
state databases into a single /APPBOX_DATA/storage/pipeline.db.

Usage:
    python3 migrate_to_pipeline_db.py [--dry-run]
"""

import argparse
import os
import re
import sqlite3
import sys

STORAGE = "/APPBOX_DATA/storage"

MIGRATIONS = [
    {
        "source": STORAGE + "/.translation-state/translation_state.db",
        "tables": {
            "translation_log": "translation_log",
        },
    },
    {
        "source": STORAGE + "/.subtitle-quality-state/subtitle_quality_state.db",
        "tables": {
            "file_audits":        "sqm_file_audits",
            "needs_upgrade":      "sqm_needs_upgrade",
            "quality_checks":     "sqm_quality_checks",
            "pending_work":       "sqm_pending_work",
            "nuclear_attempts":   "sqm_nuclear_attempts",
            "watermark_patterns": "sqm_watermark_patterns",
            "sync_drift_cache":   "sqm_sync_drift_cache",
        },
    },
    {
        "source": STORAGE + "/.subtitle-dedupe-state/subtitle_dedupe.db",
        "tables": {
            "media_state": "dedupe_media_state",
        },
    },
    {
        "source": STORAGE + "/.streaming-checker-state/streaming_state.db",
        "tables": {
            "streaming_status":     "streaming_status",
            "scan_history":         "streaming_scan_history",
            "streaming_exclusions": "streaming_exclusions",
        },
    },
    {
        "source": STORAGE + "/.subtitle-recovery-state/recovery_state.db",
        "tables": {
            "recovery_state": "recovery_state",
        },
    },
    {
        "source": STORAGE + "/.subtitle-audit-state/track_audit.db",
        "tables": {
            "videos": "audit_videos",
            "tracks": "audit_tracks",
        },
    },
    {
        "source": STORAGE + "/.subtitle-audit-state/bulk_remediate.db",
        "tables": {
            "work": "remediate_work",
        },
    },
    {
        "source": STORAGE + "/.metrics-state/pipeline_metrics.db",
        "tables": {
            "subsystem_runs":   "metrics_subsystem_runs",
            "daily_aggregates": "metrics_daily_aggregates",
        },
    },
    {
        "source": STORAGE + "/.grab-monitor-state/seen.db",
        "tables": {
            "seen_grabs": "grabmon_seen_grabs",
        },
    },
    {
        "source": STORAGE + "/.transcode-state-media/library_codec_state.db",
        "tables": {
            "pipeline_state": "pipeline_state",
        },
    },
]

TARGET_DB = STORAGE + "/pipeline.db"


def log(msg):
    print(msg, flush=True)


def die(msg):
    print("FATAL: " + msg, file=sys.stderr, flush=True)
    sys.exit(1)


def connect_pipeline(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def get_source_schema(source_db, orig_table):
    conn = sqlite3.connect(source_db, timeout=30)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        cur = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
            ("table", orig_table),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            raise ValueError("Table  + orig_table +  not found in " + source_db)
        table_sql = row[0]
        cur = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type=? AND tbl_name=? AND sql IS NOT NULL",
            ("index", orig_table),
        )
        index_sqls = [r[0] for r in cur.fetchall() if r[0]]
    finally:
        conn.close()
    return table_sql, index_sqls


def rename_table_in_sql(sql, orig, target):
    sql = re.sub(
        r"(?i)(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)" + re.escape(orig) + r"\b",
        r"\g<1>" + target,
        sql,
        count=1,
    )
    sql = re.sub(
        r"(?i)(\bON\s+)" + re.escape(orig) + r"\b",
        r"\g<1>" + target,
        sql,
        count=1,
    )
    sql = re.sub(
        r"(?i)(CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?)(\S+)",
        lambda m: m.group(1) + m.group(2).replace(orig, target),
        sql,
        count=1,
    )
    return sql


def get_row_count(conn, table):
    cur = conn.execute("SELECT COUNT(*) FROM \"" + table + "\"")
    return cur.fetchone()[0]


def get_source_row_count(source_db, orig_table):
    conn = sqlite3.connect(source_db, timeout=30)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        cur = conn.execute("SELECT COUNT(*) FROM \"" + orig_table + "\"")
        return cur.fetchone()[0]
    finally:
        conn.close()


def migrate(dry_run):
    mode_label = "[DRY-RUN] " if dry_run else ""
    log("\n" + "="*60)
    log(mode_label + "Phase 6 I-A: migrate_to_pipeline_db.py")
    log("Target: " + TARGET_DB)
    log("="*60 + "\n")

    for migration in MIGRATIONS:
        src = migration["source"]
        if not os.path.isfile(src):
            die("Source DB not found: " + src)

    os.makedirs(os.path.dirname(TARGET_DB), exist_ok=True)

    if os.path.isfile(TARGET_DB) and not dry_run:
        die("Target DB already exists: " + TARGET_DB + "\nRemove it or use --dry-run.")

    pipeline_conn = connect_pipeline(TARGET_DB)
    results = []

    for migration in MIGRATIONS:
        source_db = migration["source"]
        table_map = migration["tables"]
        log("--- Source: " + source_db)

        for orig_table, target_table in table_map.items():
            log("  " + orig_table + " -> " + target_table)

            try:
                table_sql, index_sqls = get_source_schema(source_db, orig_table)
            except ValueError as e:
                log("  ERROR: " + str(e))
                results.append((source_db, orig_table, target_table, 0, 0, "SCHEMA_MISSING"))
                continue

            target_table_sql = rename_table_in_sql(table_sql, orig_table, target_table)
            target_index_sqls = [rename_table_in_sql(s, orig_table, target_table) for s in index_sqls]

            try:
                pipeline_conn.execute(target_table_sql)
                for idx_sql in target_index_sqls:
                    try:
                        pipeline_conn.execute(idx_sql)
                    except sqlite3.OperationalError as e:
                        log("  WARN: index creation failed: " + str(e))
                pipeline_conn.commit()
            except sqlite3.OperationalError as e:
                log("  ERROR creating table " + target_table + ": " + str(e))
                results.append((source_db, orig_table, target_table, 0, 0, "CREATE_FAIL"))
                continue

            src_rows = get_source_row_count(source_db, orig_table)
            log("    Source rows: " + str(src_rows))

            if dry_run:
                log("    [DRY-RUN] Would copy " + str(src_rows) + " rows")
                results.append((source_db, orig_table, target_table, src_rows, src_rows, "DRY-RUN"))
                continue

            try:
                attach_name = "src_" + re.sub(r"[^a-zA-Z0-9_]", "_", orig_table)
                pipeline_conn.execute("ATTACH DATABASE ? AS \"" + attach_name + "\"", (source_db,))
                cur = pipeline_conn.execute(
                    "PRAGMA \"" + attach_name + "\".table_info(\"" + orig_table + "\")"
                )
                columns = [row[1] for row in cur.fetchall()]
                col_list = ", ".join("\"" + c + "\"" for c in columns)
                pipeline_conn.execute(
                    "INSERT INTO \"" + target_table + "\" (" + col_list + ") "
                    "SELECT " + col_list + " FROM \"" + attach_name + "\".\"" + orig_table + "\""
                )
                pipeline_conn.commit()
                pipeline_conn.execute("DETACH DATABASE \"" + attach_name + "\"")
            except sqlite3.Error as e:
                log("  ERROR during copy: " + str(e))
                results.append((source_db, orig_table, target_table, src_rows, 0, "INSERT_FAIL"))
                pipeline_conn.close()
                die("Migration aborted at " + orig_table + " -> " + target_table + ": " + str(e))

            dst_rows = get_row_count(pipeline_conn, target_table)
            log("    Target rows: " + str(dst_rows))

            if src_rows != dst_rows:
                pipeline_conn.close()
                die(
                    "Row count mismatch for " + orig_table + " -> " + target_table +
                    ": source=" + str(src_rows) + " target=" + str(dst_rows)
                )

            results.append((source_db, orig_table, target_table, src_rows, dst_rows, "OK"))
            log("    OK (" + str(dst_rows) + " rows)")

    pipeline_conn.close()

    log("\n" + "="*60)
    log(mode_label + "Migration Summary")
    log("="*60)
    any_fail = False
    for (src, orig, tgt, src_rows, dst_rows, status) in results:
        mark = "OK" if status in ("OK", "DRY-RUN") else "FAIL"
        log("  [" + mark + "] " + orig + " -> " + tgt + ": " + str(src_rows) + " rows (" + status + ")")
        if mark == "FAIL":
            any_fail = True

    total_tables = len(results)
    ok_tables = sum(1 for r in results if r[5] in ("OK", "DRY-RUN"))
    total_rows = sum(r[3] for r in results)
    log("\n  Tables: " + str(ok_tables) + "/" + str(total_tables) + " OK")
    log("  Total rows migrated: " + str(total_rows))

    if any_fail:
        pipeline_conn_cleanup = sqlite3.connect(TARGET_DB, timeout=5)
        pipeline_conn_cleanup.close()
        die("One or more migrations failed.")

    if dry_run:
        log("\n[DRY-RUN] All checks passed. Re-run without --dry-run to execute.")
        for f in (TARGET_DB, TARGET_DB + "-wal", TARGET_DB + "-shm"):
            if os.path.isfile(f):
                os.remove(f)
        return

    log("\n" + "="*60)
    log("Renaming source DBs to .db.pre-migration")
    log("="*60)
    renamed_dbs = set()
    for migration in MIGRATIONS:
        src = migration["source"]
        if src in renamed_dbs:
            continue
        backup = src + ".pre-migration"
        try:
            os.rename(src, backup)
            log("  Renamed: " + src + " -> " + backup)
            renamed_dbs.add(src)
            for suffix in ("-wal", "-shm"):
                wf = src + suffix
                if os.path.isfile(wf):
                    os.rename(wf, backup + suffix)
        except OSError as e:
            log("  WARN: Could not rename " + src + ": " + str(e))

    log("\n" + mode_label + "Migration complete. Target DB: " + TARGET_DB)
    log("Run: sqlite3 " + TARGET_DB + " .tables  to verify.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Phase 6 I-A: Consolidate pipeline state DBs into pipeline.db"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Verify without writing or renaming")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
