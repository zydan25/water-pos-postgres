#!/usr/bin/env python3
from __future__ import annotations

import getpass
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from werkzeug.security import generate_password_hash

BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "instance" / "water_billing.sqlite3"


def ask(name: str, hidden: bool = False) -> str:
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    if hidden:
        return getpass.getpass(f"{name}: ").strip()
    return input(f"{name}: ").strip()


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}. Run init_schema.py first.")

    username = ask("ADMIN_USERNAME")
    password = ask("ADMIN_PASSWORD", hidden=True)

    if not username:
        raise SystemExit("ADMIN_USERNAME cannot be empty.")
    if not password:
        raise SystemExit("ADMIN_PASSWORD cannot be empty.")

    now = datetime.now().isoformat()
    password_hash = generate_password_hash(password)

    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        row = conn.execute(
            "SELECT id FROM users WHERE username = ? LIMIT 1",
            (username,),
        ).fetchone()

        if row:
            conn.execute(
                """
                UPDATE users
                SET password_hash = ?, role = 'Admin', active = 1, updated_at = ?
                WHERE id = ?
                """,
                (password_hash, now, row[0]),
            )
            user_id = row[0]
            action = "updated"
        else:
            cur = conn.execute(
                """
                INSERT INTO users
                    (username, password_hash, role, active, created_at, updated_at)
                VALUES (?, ?, 'Admin', 1, ?, ?)
                """,
                (username, password_hash, now, now),
            )
            user_id = cur.lastrowid
            action = "created"

        conn.commit()
        print(f"Admin user {action}: {username} (id={user_id})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
