"""Import a validated SQLite snapshot into the configured Turso database.

Use only with a newly created empty Turso database during the planned cutover.
This replaces tables in that Turso database; it does not modify the SQLite file.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a UtilBot SQLite backup into Turso.")
    parser.add_argument("snapshot", type=Path, help="Path to a .sqlite3 backup made by /backup or /drive_restore")
    parser.add_argument("--replace-existing", action="store_true", help="Allow replacing tables in a non-empty Turso database")
    args = parser.parse_args()
    load_dotenv()
    os.environ["DATABASE_BACKEND"] = "turso"

    if not args.snapshot.is_file():
        raise SystemExit(f"Snapshot not found: {args.snapshot}")
    source = sqlite3.connect(str(args.snapshot))
    try:
        integrity = source.execute("PRAGMA integrity_check").fetchone()
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not integrity or integrity[0] != "ok" or not {"vault_items", "vault_folders", "reminders", "bot_meta"}.issubset(tables):
            raise SystemExit("The source file is damaged or is not a UtilBot database.")
        count = source.execute("SELECT COUNT(*) FROM vault_items").fetchone()[0]
    finally:
        source.close()

    from src.db.database import get_connection, init_db, restore_from_sqlite_snapshot

    remote = get_connection()
    try:
        existing = remote.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    finally:
        remote.close()
    if existing and not args.replace_existing:
        raise SystemExit("Turso database already has tables. Use a new empty test database, or pass --replace-existing to intentionally replace it.")

    print(f"Importing snapshot ({count} archived records) into the Turso database configured in .env…")
    restore_from_sqlite_snapshot(args.snapshot)
    init_db()
    print("Import finished. Compare the archive count and spot-check records before switching Render.")


if __name__ == "__main__":
    main()
