#!/usr/bin/env python3
"""
Migrate agents-platform data: SQLite → PostgreSQL.

Usage (inside the container after building with psycopg2-binary):
    AGENTS_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agents_platform \
        python /app/migrate_to_pg.py

The script:
  1. Creates all tables in PostgreSQL via SQLAlchemy's create_all()
  2. Copies every non-empty table from SQLite into PostgreSQL
  3. Skips rows that already exist (ON CONFLICT DO NOTHING)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# ── Must set before any backend imports — db.py creates the engine at import time ──
PG_URL = os.environ.get(
    "AGENTS_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/agents_platform",
)
os.environ["AGENTS_DATABASE_URL"] = PG_URL

print(f"Target PostgreSQL: {PG_URL}")
print(f"Source SQLite:     /app/data/agents.db\n")

# ── Init PostgreSQL schema ───────────────────────────────────────────────────────
print("Creating PostgreSQL schema via init_db()…")
from backend.app.db import init_db  # noqa: E402

init_db()
print("Schema ready.\n")

# ── Data migration via sqlite3 + psycopg2 ────────────────────────────────────────
import psycopg2  # noqa: E402
from psycopg2.extras import Json  # noqa: E402

SQLITE_PATH = os.environ.get("AGENTS_SQLITE_PATH", "/app/data/agents.db")

# Boolean columns per table — SQLite stores 0/1 integers; PG needs True/False
BOOL_COLS: dict[str, set[str]] = {
    "models":           {"enabled"},
    "agents":           {"builtin"},
    "workflows":        {"builtin"},
    "mcp_servers":      {"enabled"},
    "custom_skills":    {"hidden", "builtin"},
    "targets":          {"enforce_budget"},
    "run_artefacts":    {"is_binary"},
}

# JSON columns per table (must match models.py)
JSON_COLS: dict[str, set[str]] = {
    "agents":               {"tool_specs", "skill_slugs", "params", "use_cases"},
    "models":               {"params"},
    "workflows":            {"graph", "use_cases"},
    "mcp_servers":          {"args", "env", "discovered_tools"},
    "retro_score_weights":  {"weights_json"},
    "settings":             {"value"},
    # empty tables — kept here for reference, not migrated below
    "runs":                 {"input", "output", "retro_score_summary"},
    "targets":              {"tags", "pr_urls"},
    "target_lessons":       {"evidence_run_ids", "applicable_tags"},
    "evals":                {"dataset", "metric_args"},
    "eval_runs":            {"cases"},
    "lesson_applications":  set(),
    "lesson_evidence_runs": set(),
    "custom_skills":        set(),
    "run_events":           {"payload"},
    "run_artefacts":        set(),
    "retro_scores":         {"evidence_json"},
}

# Order matters: parents before children (FK constraints)
TABLES_TO_MIGRATE = [
    "models",
    "agents",
    "workflows",
    "mcp_servers",
    "retro_score_weights",
    "settings",
    # All others are empty — skip
]


def transform(col: str, val, json_cols: set[str], bool_cols: set[str]):
    """Convert a SQLite value to a PostgreSQL-safe value."""
    if val is None:
        return None
    if col in json_cols:
        return Json(json.loads(val))
    if col in bool_cols:
        return bool(val)
    return val


def migrate_table(src_cur: sqlite3.Cursor, dst_cur, table: str,
                  valid_model_slugs: set[str] | None = None) -> int:
    src_cur.execute(f"SELECT * FROM {table}")
    rows = src_cur.fetchall()

    if not rows:
        print(f"  {table:30s}  0 rows — skipped")
        return 0

    cols = [d[0] for d in src_cur.description]
    jcols = JSON_COLS.get(table, set())
    bcols = BOOL_COLS.get(table, set())

    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(f'"{c}"' for c in cols)
    sql = (
        f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) '
        f"ON CONFLICT DO NOTHING"
    )

    model_slug_idx = cols.index("model_slug") if "model_slug" in cols else None

    def transform_row(row):
        vals = []
        for i, v in enumerate(row):
            # Null out dangling model_slug FK references
            if (model_slug_idx is not None and i == model_slug_idx
                    and v is not None
                    and valid_model_slugs is not None
                    and v not in valid_model_slugs):
                vals.append(None)
            else:
                vals.append(transform(cols[i], v, jcols, bcols))
        return tuple(vals)

    data = [transform_row(row) for row in rows]

    dst_cur.executemany(sql, data)
    print(f"  {table:30s}  {len(data)} rows migrated")
    return len(data)


def main() -> None:
    src = sqlite3.connect(SQLITE_PATH)
    src_cur = src.cursor()

    dst = psycopg2.connect(PG_URL)
    dst.autocommit = False
    dst_cur = dst.cursor()

    # Collect valid model slugs so we can NULL out dangling FK refs in agents
    dst_cur.execute('SELECT slug FROM "models"')
    valid_model_slugs = {r[0] for r in dst_cur.fetchall()}

    total = 0
    errors = []

    for table in TABLES_TO_MIGRATE:
        try:
            n = migrate_table(src_cur, dst_cur, table,
                              valid_model_slugs=valid_model_slugs)
            total += n
            dst.commit()
        except Exception as exc:
            dst.rollback()
            errors.append((table, exc))
            print(f"  {table:30s}  ERROR: {exc}", file=sys.stderr)

    src.close()
    dst.close()

    print(f"\n{'─'*50}")
    print(f"Total rows migrated: {total}")
    if errors:
        print(f"Tables with errors:  {[t for t, _ in errors]}", file=sys.stderr)
        sys.exit(1)
    else:
        print("Migration complete ✓")


if __name__ == "__main__":
    main()
