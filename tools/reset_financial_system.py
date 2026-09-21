#!/usr/bin/env python3
"""إعادة تأسيس النظام المالي مع الإبقاء على بيانات المشتركين وشجرة الحسابات.

الوضع الافتراضي: معاينة فقط.
التنفيذ الفعلي يحتاج: --apply

المحفوظ:
- subscribers
- account_nodes (شجرة الحسابات)
- users / employee_profiles / user_wallets
- settings
- wallets نفسها مع تصفير balance

المصفر:
- invoices / payments
- journal_entries / journal_lines
- accounting_vouchers / transactions
- bulk_readings / collection_trips / trip_visits
- attachments / cash_counts / bank_statements / fiscal_years / audit_logs

حقول البداية للمشتركين تُمسح حتى يتم إدخالها يدويًا لاحقًا:
- last_reading
- last_due_amount
- last_paid_amount
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE_DIR / "instance" / "water_billing.sqlite3"

# ترتيب الحذف يحترم العلاقات بين الجداول.
TRANSACTIONAL_TABLES = [
    "payments",
    "trip_visits",
    "attachments",
    "invoices",
    "bulk_readings",
    "collection_trips",
    "journal_lines",
    "journal_entries",
    "accounting_vouchers",
    "transactions",
    "cash_counts",
    "bank_statements",
    "fiscal_years",
    "audit_logs",
]


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def backup_database(conn: sqlite3.Connection, db_path: Path, stamp: str) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"before_financial_reset_{stamp}.sqlite3"
    with sqlite3.connect(str(backup_path)) as dest:
        conn.backup(dest)
    # سهولة الوصول إلى آخر نسخة تلقائيًا.
    latest = backup_dir / "before_financial_reset_latest.sqlite3"
    try:
        import shutil
        shutil.copy2(backup_path, latest)
    except Exception:
        pass
    return backup_path


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    if not table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def remove_referenced_files(conn: sqlite3.Connection) -> int:
    removed = 0
    paths = []
    for table, column in (("attachments", "path"), ("payments", "attachment_path")):
        if not table_exists(conn, table):
            continue
        for row in conn.execute(
            f"SELECT {column} AS path FROM {table} WHERE {column} IS NOT NULL AND TRIM({column}) <> ''"
        ).fetchall():
            paths.append(row["path"])

    uploads_root = (BASE_DIR / "uploads").resolve()
    for raw in paths:
        try:
            candidate = (BASE_DIR / str(raw)).resolve()
            if uploads_root == candidate or uploads_root in candidate.parents:
                if candidate.is_file():
                    candidate.unlink()
                    removed += 1
        except Exception:
            # ملف مفقود لا يمنع اكتمال التصفير.
            pass
    return removed


def build_preview(conn: sqlite3.Connection) -> dict:
    subscribers = count_rows(conn, "subscribers")
    accounts = count_rows(conn, "account_nodes")
    users = count_rows(conn, "users")

    deleted = {
        table: count_rows(conn, table)
        for table in TRANSACTIONAL_TABLES
        if table_exists(conn, table)
    }

    return {
        "subscribers": subscribers,
        "accounts": accounts,
        "users": users,
        "wallets": count_rows(conn, "wallets"),
        "deleted": deleted,
        "invoice_total": float(
            conn.execute(
                "SELECT COALESCE(SUM(total_amount),0) FROM invoices"
            ).fetchone()[0]
        ) if table_exists(conn, "invoices") else 0.0,
        "payment_total": float(
            conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM payments"
            ).fetchone()[0]
        ) if table_exists(conn, "payments") else 0.0,
        "journal_debit": float(
            conn.execute(
                "SELECT COALESCE(SUM(debit),0) FROM journal_lines"
            ).fetchone()[0]
        ) if table_exists(conn, "journal_lines") else 0.0,
        "journal_credit": float(
            conn.execute(
                "SELECT COALESCE(SUM(credit),0) FROM journal_lines"
            ).fetchone()[0]
        ) if table_exists(conn, "journal_lines") else 0.0,
    }


def reset_database(db_path: Path) -> int:
    if not db_path.exists():
        raise SystemExit(f"قاعدة البيانات غير موجودة: {db_path}")

    conn = connect(db_path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SystemExit(f"فحص سلامة قاعدة البيانات فشل: {integrity}")

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_database(conn, db_path, stamp)
        print(f"BACKUP={backup_path}")

        removed_files = remove_referenced_files(conn)
        if removed_files:
            print(f"ATTACHMENT_FILES_REMOVED={removed_files}")

        before = build_preview(conn)

        conn.execute("BEGIN")
        try:
            for table in TRANSACTIONAL_TABLES:
                if table_exists(conn, table):
                    conn.execute(f"DELETE FROM {table}")

            # الصناديق بيانات رئيسية وليست حركات؛ نحافظ عليها ونصفر أرصدتها.
            if table_exists(conn, "wallets"):
                conn.execute("UPDATE wallets SET balance=0")

            # نقطة بداية يدوية جديدة للمشتركين.
            if table_exists(conn, "subscribers"):
                conn.execute(
                    """
                    UPDATE subscribers
                    SET last_reading=NULL,
                        last_due_amount=0,
                        last_paid_amount=0,
                        updated_at=?
                    """,
                    (datetime.now().isoformat(),),
                )

            # إعادة ترقيم الفواتير من البداية.
            if table_exists(conn, "settings"):
                conn.execute(
                    """
                    INSERT INTO settings(key,value)
                    VALUES('invoice_start_no','1')
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """
                )
                conn.execute(
                    """
                    INSERT INTO settings(key,value)
                    VALUES('financial_reset_at',?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (datetime.now().isoformat(),),
                )

            sequence_names = [
                table for table in TRANSACTIONAL_TABLES
                if table_exists(conn, table)
            ]
            if sequence_names and table_exists(conn, "sqlite_sequence"):
                marks = ",".join("?" for _ in sequence_names)
                conn.execute(
                    f"DELETE FROM sqlite_sequence WHERE name IN ({marks})",
                    sequence_names,
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise

        after = build_preview(conn)

        # تحقق صريح من أن المشتركين والشجرة بقيتا وأن الحركات صُفرت.
        remaining_transactions = sum(after["deleted"].values())
        if after["subscribers"] != before["subscribers"]:
            raise RuntimeError("عدد المشتركين تغير أثناء التصفير.")
        if after["accounts"] != before["accounts"]:
            raise RuntimeError("شجرة الحسابات تغيرت أثناء التصفير.")
        if remaining_transactions != 0:
            raise RuntimeError(f"بقيت حركات مالية: {remaining_transactions}")

        print("=== RESET COMPLETE ===")
        print(f"SUBSCRIBERS_KEPT={after['subscribers']}")
        print(f"ACCOUNT_NODES_KEPT={after['accounts']}")
        print(f"USERS_KEPT={after['users']}")
        print(f"WALLETS_KEPT={after['wallets']}")
        print(f"INVOICES_AFTER={count_rows(conn, 'invoices')}")
        print(f"PAYMENTS_AFTER={count_rows(conn, 'payments')}")
        print(f"JOURNAL_ENTRIES_AFTER={count_rows(conn, 'journal_entries')}")
        print(f"JOURNAL_LINES_AFTER={count_rows(conn, 'journal_lines')}")
        print("SUBSCRIBER_START_FIELDS_RESET=1")
        print("WALLET_BALANCES_RESET=1")
        return 0
    finally:
        conn.close()


def preview(db_path: Path) -> int:
    conn = connect(db_path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SystemExit(f"فحص سلامة قاعدة البيانات فشل: {integrity}")
        data = build_preview(conn)
        print("=== DRY RUN / NO CHANGES ===")
        print(f"SUBSCRIBERS_KEPT={data['subscribers']}")
        print(f"ACCOUNT_NODES_KEPT={data['accounts']}")
        print(f"USERS_KEPT={data['users']}")
        print(f"WALLETS_KEPT={data['wallets']}")
        print(f"OLD_INVOICE_TOTAL={data['invoice_total']:.2f}")
        print(f"OLD_PAYMENT_TOTAL={data['payment_total']:.2f}")
        print(f"OLD_JOURNAL_DEBIT={data['journal_debit']:.2f}")
        print(f"OLD_JOURNAL_CREDIT={data['journal_credit']:.2f}")
        print("--- rows to delete ---")
        for table, count in data["deleted"].items():
            print(f"{table}={count}")
        print("--- rows to keep ---")
        print("subscribers/account_nodes/users/employee_profiles/user_wallets/settings/wallet definitions")
        print("--- subscriber start fields ---")
        print("last_reading=NULL, last_due_amount=0, last_paid_amount=0")
        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="تصفير مالي مع الإبقاء على المشتركين وشجرة الحسابات."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="تنفيذ التصفير الفعلي؛ بدون هذا الخيار تكون معاينة فقط.",
    )
    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    return reset_database(db_path) if args.apply else preview(db_path)


if __name__ == "__main__":
    raise SystemExit(main())
