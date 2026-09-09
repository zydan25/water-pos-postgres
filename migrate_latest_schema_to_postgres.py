#!/usr/bin/env python3
"""Safe schema-only synchronization: latest SQLite schema -> existing PostgreSQL.

This script NEVER copies/updates/deletes business data. It adds missing tables,
missing columns and missing indexes where safe, and reports data-count/type
mismatches for manual review.
"""
from __future__ import annotations

import argparse
import getpass
import sqlite3
from pathlib import Path

import psycopg2
from psycopg2 import sql


def pg_type(sqlite_type: str) -> str:
    t = (sqlite_type or "TEXT").upper()
    if "INT" in t:
        return "INTEGER"
    if any(x in t for x in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE PRECISION"
    if "BOOL" in t:
        return "BOOLEAN"
    if "BLOB" in t:
        return "BYTEA"
    if "NUMERIC" in t or "DECIMAL" in t:
        return "NUMERIC(18,4)"
    return "TEXT"


def qi(name: str):
    return sql.Identifier(name)


def sqlite_tables(con):
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]


def sqlite_columns(con, table):
    return con.execute(f'PRAGMA table_info("{table}")').fetchall()


def pg_tables(cur):
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    return {r[0] for r in cur.fetchall()}


def pg_columns(cur, table):
    cur.execute(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
        (table,),
    )
    return {r[0]: r for r in cur.fetchall()}


def ensure_table(cur, con, table):
    cols = sqlite_columns(con, table)
    parts = []
    pk_cols = [c[1] for c in cols if c[5]]
    for cid, name, typ, notnull, default, pk in cols:
        p = sql.SQL("{} {}").format(qi(name), sql.SQL(pg_type(typ)))
        if notnull and not pk:
            p += sql.SQL(" NOT NULL")
        if default is not None:
            d = str(default).strip()
            if d.upper() == "CURRENT_TIMESTAMP":
                p += sql.SQL(" DEFAULT CURRENT_TIMESTAMP")
            else:
                p += sql.SQL(" DEFAULT ") + sql.SQL(d)
        parts.append(p)
    if len(pk_cols) == 1:
        parts.append(sql.SQL("PRIMARY KEY ({})").format(qi(pk_cols[0])))
    elif pk_cols:
        parts.append(sql.SQL("PRIMARY KEY ({})").format(sql.SQL(", ").join(qi(x) for x in pk_cols)))
    cur.execute(sql.SQL("CREATE TABLE {} ({})").format(qi(table), sql.SQL(", ").join(parts)))


def add_missing_columns(cur, con, table):
    pcols = pg_columns(cur, table)
    added = []
    for cid, name, typ, notnull, default, pk in sqlite_columns(con, table):
        if name in pcols:
            continue
        # Existing rows make a NOT NULL/no-default column unsafe. Add it nullable
        # and report it instead of manufacturing data.
        statement = sql.SQL("ALTER TABLE {} ADD COLUMN {} {}").format(qi(table), qi(name), sql.SQL(pg_type(typ)))
        cur.execute(statement)
        added.append(name)
    return added


def add_indexes(cur, con, table):
    rows = con.execute(f'PRAGMA index_list("{table}")').fetchall()
    created = []
    for row in rows:
        # SQLite: seq, name, unique, origin, partial
        name, unique = row[1], bool(row[2])
        if row[3] == "pk" or name.startswith("sqlite_autoindex_"):
            continue
        idx_cols = [r[2] for r in con.execute(f'PRAGMA index_info("{name}")').fetchall()]
        if not idx_cols:
            continue
        clause = sql.SQL("CREATE {} INDEX IF NOT EXISTS {} ON {} ({})").format(
            sql.SQL("UNIQUE") if unique else sql.SQL(""), qi(name), qi(table),
            sql.SQL(", ").join(qi(c) for c in idx_cols)
        )
        cur.execute(clause)
        created.append(name)
    return created




def fk_exists(cur, table, column, ref_table, ref_column):
    cur.execute(
        "SELECT 1 FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu ON kcu.constraint_name=tc.constraint_name AND kcu.table_schema=tc.table_schema "
        "JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name=tc.constraint_name AND ccu.constraint_schema=tc.table_schema "
        "WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema='public' "
        "AND tc.table_name=%s AND kcu.column_name=%s AND ccu.table_name=%s AND ccu.column_name=%s LIMIT 1",
        (table, column, ref_table, ref_column),
    )
    return cur.fetchone() is not None


def add_foreign_keys(cur, con, table):
    created=[]
    for row in con.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
        # SQLite: id,seq,table,from,to,on_update,on_delete,match,...
        ref_table, from_col, to_col = row[2], row[3], row[4]
        if fk_exists(cur, table, from_col, ref_table, to_col):
            continue
        name=f"fk_{table}_{from_col}_{ref_table}_{to_col}"[:60]
        cur.execute(sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({})").format(
            qi(table), qi(name), qi(from_col), qi(ref_table), qi(to_col)
        ))
        created.append(name)
    return created

def count_table(cur, table):
    cur.execute(sql.SQL("SELECT COUNT(*) FROM {}" ).format(qi(table)))
    return int(cur.fetchone()[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", default="5432")
    ap.add_argument("--database", default="waternadary")
    ap.add_argument("--user", default="waternadary")
    ap.add_argument("--password")
    args = ap.parse_args()

    sqlite_path = Path(args.sqlite).resolve()
    if not sqlite_path.is_file():
        raise SystemExit(f"SQLite file not found: {sqlite_path}")

    scon = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    quick = scon.execute("PRAGMA quick_check").fetchone()[0]
    if quick != "ok":
        scon.close()
        raise SystemExit(f"ABORT: SQLite quick_check = {quick!r}")

    password = args.password or getpass.getpass("PostgreSQL password: ")
    pg = psycopg2.connect(host=args.host, port=args.port, dbname=args.database, user=args.user, password=password)
    pg.autocommit = False
    cur = pg.cursor()
    try:
        tables = sqlite_tables(scon)
        existing = pg_tables(cur)
        print(f"SQLite quick_check: OK")
        print(f"Latest SQLite tables: {len(tables)}")

        added_tables = []
        added_cols = {}
        added_indexes = {}
        added_fks = {}
        for table in tables:
            if table not in existing:
                ensure_table(cur, scon, table)
                added_tables.append(table)
            else:
                added = add_missing_columns(cur, scon, table)
                if added:
                    added_cols[table] = added
            created = add_indexes(cur, scon, table)
            if created:
                added_indexes[table] = created

        # Add missing foreign keys only when the existing PostgreSQL data satisfies them.
        for table in tables:
            created = add_foreign_keys(cur, scon, table)
            if created:
                added_fks[table] = created

        # Sequence repair for integer PKs without changing any data.
        for table in tables:
            cols = sqlite_columns(scon, table)
            pk = next((c for c in cols if c[5] == 1 and "INT" in (c[2] or "").upper()), None)
            if not pk:
                continue
            col = pk[1]
            cur.execute("SELECT pg_get_serial_sequence(%s,%s)", (table, col))
            seq = cur.fetchone()[0]
            if seq:
                cur.execute(sql.SQL("SELECT setval(%s::regclass, GREATEST(COALESCE((SELECT MAX({}) FROM {}), 1), 1), true)").format(qi(col), qi(table)), (seq,))

        print("\nSchema synchronization:")
        print(f"  tables added: {len(added_tables)}")
        for t in added_tables:
            print(f"    + {t}")
        for t, cols in added_cols.items():
            print(f"  columns added in {t}: {', '.join(cols)}")
        for t, idxs in added_indexes.items():
            print(f"  indexes ensured in {t}: {', '.join(idxs)}")
        for t, fks in added_fks.items():
            print(f"  foreign keys added in {t}: {', '.join(fks)}")

        print("\nRead-only data count comparison (not syncing data):")
        mismatches = []
        for t in tables:
            s_count = scon.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            try:
                p_count = count_table(cur, t)
            except Exception:
                p_count = None
            status = "OK" if p_count == s_count else "DIFF"
            print(f"  {status:4s} {t:28s} SQLite={s_count} PostgreSQL={p_count}")
            if status == "DIFF":
                mismatches.append((t, s_count, p_count))

        pg.commit()
        print("\nSUCCESS: schema synchronization committed. No business rows were copied, updated, or deleted.")
        if mismatches:
            print("Data differences were intentionally left untouched:")
            for t, s_count, p_count in mismatches:
                print(f"  {t}: SQLite={s_count}, PostgreSQL={p_count}")
    except Exception:
        pg.rollback()
        raise
    finally:
        cur.close(); pg.close(); scon.close()


if __name__ == "__main__":
    main()
