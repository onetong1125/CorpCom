"""Move legacy research fields to JSON and drop their SQLite columns once.

Run with the backend stopped:
    .venv/bin/python scripts/migrate_workspace_research.py data
"""

import argparse
import json
from pathlib import Path
import sqlite3


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    root = parser.parse_args().data_dir.resolve()
    fields = ("goal", "scope", "observation_period", "initial_hypothesis", "preferences")

    # Require an existing database; this script deliberately targets the old schema.
    with sqlite3.connect((root / "corpcom.db").as_uri() + "?mode=rw", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT id, goal, scope, observation_period, initial_hypothesis, preferences FROM workspaces"
        ).fetchall()
        for row in rows:
            info = {field: row[field] for field in fields}
            info["preferences"] = json.loads(info["preferences"])
            path = root / "workspaces" / row["id"] / "research.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(info, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                encoding="utf-8",
            )
        for field in fields:
            db.execute(f"ALTER TABLE workspaces DROP COLUMN {field}")
    print(f"Migrated {len(rows)} workspaces and removed their legacy research columns.")
