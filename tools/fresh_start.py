#!/usr/bin/env python3
"""بداية جديدة للنظام: تصفير الحركات مع ترحيل آخر فاتورة كرصيد افتتاحي للمشترك.

ما يفعله:
  1. نسخة احتياطية كاملة للقاعدة + تصدير المشتركين وآخر فاتورة لكل مشترك إلى CSV.
  2. نقل آخر فاتورة لكل مشترك إلى حقوله: آخر قراءة = القراءة الحالية،
     المستحق = إجمالي الفاتورة، المسدد = المسدد فعلاً (فالمتأخرات = المستحق - المسدد).
  3. حذف كل الحركات: الفواتير، المدفوعات، السندات، القيود، القراءات، الجولات، المرفقات.
  4. قيد افتتاحي واحد بتاريخ البدء: مدين ذمم كل عميل بمتأخراته، مع ترحيل أرصدة
     الصناديق/المحصلين كما هي، والطرف المقابل حساب «أرصدة افتتاحية».
     الإيرادات والمصروفات تبدأ من صفر لأن إيراد الفترة السابقة اعتُرف به سابقاً.
  5. تحقق نهائي: القيد متوازن، ورصيد كل عميل في الدفتر = متأخراته في بطاقته.

التشغيل تجريبي افتراضياً؛ لا يكتب شيئاً إلا مع --apply.
"""

import argparse
import csv
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE_DIR / "instance" / "water_billing.sqlite3"
OPENING_CODE = "999900"
OPENING_NAME = "أرصدة افتتاحية"
ADVANCES_CODE = "210900"
ADVANCES_NAME = "دفعات مقدمة من المشتركين"

TRANSACTIONAL_TABLES = [
    "trip_visits",
    "collection_trips",
    "attachments",
    "payments",
    "invoices",
    "bulk_readings",
    "journal_lines",
    "journal_entries",
    "accounting_vouchers",
    "transactions",
    "cash_counts",
    "bank_statements",
]


def money(value):
    return f"{float(value or 0):,.2f}"


def connect(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def collect_last_invoices(conn):
    """آخر فاتورة لكل مشترك (الأحدث تاريخاً ثم رقماً)."""
    rows = conn.execute(
        """
        SELECT i.*
        FROM invoices i
        JOIN (
            SELECT subscriber_id, MAX(COALESCE(invoice_date, '') || '/' || printf('%012d', id)) AS mark
            FROM invoices
            WHERE subscriber_id IS NOT NULL
            GROUP BY subscriber_id
        ) last
          ON last.subscriber_id = i.subscriber_id
         AND last.mark = COALESCE(i.invoice_date, '') || '/' || printf('%012d', i.id)
        ORDER BY i.subscriber_id
        """
    ).fetchall()
    return {int(r["subscriber_id"]): r for r in rows}


def carried_account_balances(conn, customer_account_ids):
    """أرصدة الأصول/الخصوم/حقوق الملكية غير حسابات العملاء (نقدية، محصلون، التزامات)."""
    rows = conn.execute(
        """
        SELECT a.id, a.code, a.name,
               COALESCE(SUM(jl.debit), 0) - COALESCE(SUM(jl.credit), 0) AS bal
        FROM journal_lines jl
        JOIN account_nodes a ON a.id = jl.account_id
        GROUP BY a.id
        HAVING ABS(bal) > 0.005
        ORDER BY a.code
        """
    ).fetchall()
    carried = []
    for r in rows:
        code = str(r["code"] or "")
        if r["id"] in customer_account_ids:
            continue
        if code.startswith("4") or code.startswith("5") or code == OPENING_CODE:
            continue
        carried.append(r)
    return carried


def ensure_account(conn, code, name, node_type, parent_code=None, apply=False):
    row = conn.execute("SELECT id FROM account_nodes WHERE code=?", (code,)).fetchone()
    if row:
        return row["id"]
    if not apply:
        return None
    parent_id = None
    if parent_code:
        parent = conn.execute("SELECT id FROM account_nodes WHERE code=?", (parent_code,)).fetchone()
        parent_id = parent["id"] if parent else None
    now = datetime.now().isoformat()
    cur = conn.execute(
        """
        INSERT INTO account_nodes (code, name, parent_id, node_type, is_postable, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, 1, 1, ?, ?)
        """,
        (code, name, parent_id, node_type, now, now),
    )
    return cur.lastrowid


def backup(conn, db_path, stamp):
    backups_dir = Path(db_path).resolve().parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    db_copy = backups_dir / f"pre_fresh_start_{stamp}.sqlite3"
    with sqlite3.connect(str(db_copy)) as dest:
        conn.backup(dest)
    subs_csv = backups_dir / f"subscribers_{stamp}.csv"
    rows = conn.execute("SELECT * FROM subscribers ORDER BY id").fetchall()
    with subs_csv.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(rows[0].keys() if rows else [])
        for r in rows:
            writer.writerow([r[k] for k in r.keys()])
    inv_csv = backups_dir / f"last_invoices_{stamp}.csv"
    rows = conn.execute(
        """
        SELECT s.id AS subscriber_id, s.account_number, s.name,
               i.invoice_no, i.invoice_date, i.month_label, i.previous_reading, i.current_reading,
               i.consumption, i.opening_balance, i.total_amount, i.paid_amount, i.remaining_amount, i.credit_amount
        FROM subscribers s
        LEFT JOIN invoices i ON i.subscriber_id = s.id
        ORDER BY s.id, i.id
        """
    ).fetchall()
    with inv_csv.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(rows[0].keys() if rows else [])
        for r in rows:
            writer.writerow([r[k] for k in r.keys()])
    # نسخة إضافية بجانب القاعدة يسهل تنزيلها
    shutil.copy2(db_copy, backups_dir / "pre_fresh_start_latest.sqlite3")
    return db_copy, subs_csv, inv_csv


def run(db_path, cutoff, apply, carry_cash, keep_audit):
    conn = connect(db_path)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"القاعدة: {db_path}")
    print(f"تاريخ البدء: {cutoff}")
    print(f"الوضع: {'تنفيذ فعلي' if apply else 'تجريبي (لا يُكتب شيء)'}\n")

    integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        raise SystemExit(f"فحص سلامة القاعدة فشل: {integrity}")

    subscribers = conn.execute("SELECT * FROM subscribers ORDER BY id").fetchall()
    last_invoices = collect_last_invoices(conn)
    customer_account_ids = {s["account_node_id"] for s in subscribers if s["account_node_id"]}

    plan = []          # (subscriber, reading, due, paid, arrears)
    advances = []      # (subscriber, credit_amount)
    missing_account = []
    total_arrears = 0.0
    total_advance = 0.0
    for s in subscribers:
        inv = last_invoices.get(int(s["id"]))
        if inv is not None:
            reading = inv["current_reading"]
            due = round(float(inv["total_amount"] or 0), 2)
            paid = round(float(inv["paid_amount"] or 0), 2)
        else:
            reading = s["last_reading"]
            due = round(float(s["last_due_amount"] or 0), 2)
            paid = round(float(s["last_paid_amount"] or 0), 2)
        arrears = round(due - paid, 2)
        if arrears < 0:
            advances.append((s, abs(arrears)))
            total_advance += abs(arrears)
            arrears = 0.0
        total_arrears += arrears
        if arrears > 0 and not s["account_node_id"]:
            missing_account.append(s)
        plan.append((s, reading, due, paid, arrears))

    carried = carried_account_balances(conn, customer_account_ids) if carry_cash else []

    print(f"المشتركون: {len(subscribers)} — منهم {len(last_invoices)} لهم فواتير")
    print(f"إجمالي المتأخرات المرحّلة: {money(total_arrears)}")
    if advances:
        print(f"دفعات مقدمة (سداد زائد) لـ {len(advances)} مشترك بإجمالي {money(total_advance)}")
    print("الأرصدة المرحّلة كما هي:")
    for r in carried:
        print(f"  {r['code']:<10} {r['name']:<28} {money(r['bal'])}")
    if not carried:
        print("  (لا شيء)")
    if missing_account:
        print(f"تحذير: {len(missing_account)} مشترك عليه متأخرات بلا حساب تفصيلي — سيُنشأ لهم حساب تلقائياً.")

    debit_total = round(total_arrears + sum(max(0.0, float(r["bal"])) for r in carried), 2)
    credit_total = round(total_advance + sum(max(0.0, -float(r["bal"])) for r in carried), 2)
    balancing = round(debit_total - credit_total, 2)
    print(f"\nالقيد الافتتاحي: مدين {money(debit_total)} / دائن {money(credit_total)}")
    print(f"الطرف المقابل ({OPENING_CODE} {OPENING_NAME}): {money(abs(balancing))} "
          f"{'دائن' if balancing > 0 else 'مدين'}")

    if not apply:
        print("\nتشغيل تجريبي — أعد التشغيل مع --apply للتنفيذ.")
        conn.close()
        return

    db_copy, subs_csv, inv_csv = backup(conn, db_path, stamp)
    print(f"\nنسخة احتياطية: {db_copy}\nمشتركون: {subs_csv}\nفواتير: {inv_csv}")

    now = datetime.now().isoformat()
    conn.execute("BEGIN")
    try:
        # 1) ترحيل آخر فاتورة إلى بطاقة المشترك
        for s, reading, due, paid, _arrears in plan:
            conn.execute(
                """
                UPDATE subscribers
                SET last_reading=?, last_due_amount=?, last_paid_amount=?, updated_at=?
                WHERE id=?
                """,
                (reading, due, paid, now, s["id"]),
            )

        # 2) إنشاء حساب تفصيلي لمن ليس له حساب
        customers_root = conn.execute(
            "SELECT id FROM account_nodes WHERE code='1210' OR name LIKE '%عملاء%' ORDER BY id LIMIT 1"
        ).fetchone()
        account_for = {}
        for s, _reading, _due, _paid, arrears in plan:
            node_id = s["account_node_id"]
            if not node_id and arrears > 0:
                safe = "".join(ch for ch in str(s["account_number"] or s["id"]) if ch.isalnum())[-10:]
                code = f"CUST-{safe or int(s['id']):05}"
                existing = conn.execute("SELECT id FROM account_nodes WHERE code=?", (code,)).fetchone()
                if existing:
                    node_id = existing["id"]
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO account_nodes (code, name, parent_id, node_type, is_postable, active, created_at, updated_at)
                        VALUES (?, ?, ?, 'customer', 1, 1, ?, ?)
                        """,
                        (code, s["name"], customers_root["id"] if customers_root else None, now, now),
                    )
                    node_id = cur.lastrowid
                conn.execute("UPDATE subscribers SET account_node_id=? WHERE id=?", (node_id, s["id"]))
            account_for[int(s["id"])] = node_id

        # 3) تصفير الحركات
        deleted = {}
        for table in TRANSACTIONAL_TABLES:
            if not table_exists(conn, table):
                continue
            deleted[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute(f"DELETE FROM {table}")
        if not keep_audit and table_exists(conn, "audit_logs"):
            deleted["audit_logs"] = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
            conn.execute("DELETE FROM audit_logs")
        names = list(deleted.keys())
        if names:
            conn.execute(
                f"DELETE FROM sqlite_sequence WHERE name IN ({','.join('?' * len(names))})",
                names,
            )
        print("\nتم حذف:")
        for t, n in deleted.items():
            print(f"  {t}: {n}")

        # 4) القيد الافتتاحي
        opening_id = ensure_account(conn, OPENING_CODE, OPENING_NAME, "equity", "3000", apply=True)
        lines = []
        for s, _reading, _due, _paid, arrears in plan:
            if arrears <= 0:
                continue
            lines.append((account_for[int(s["id"])], arrears, 0.0,
                          f"رصيد افتتاحي - {s['account_number']} {s['name']}"))
        if advances:
            advances_id = ensure_account(conn, ADVANCES_CODE, ADVANCES_NAME, "liability", "2000", apply=True)
            for s, amount in advances:
                lines.append((advances_id, 0.0, amount,
                              f"دفعة مقدمة - {s['account_number']} {s['name']}"))
        for r in carried:
            bal = float(r["bal"])
            if bal > 0:
                lines.append((r["id"], round(bal, 2), 0.0, f"رصيد افتتاحي - {r['name']}"))
            else:
                lines.append((r["id"], 0.0, round(-bal, 2), f"رصيد افتتاحي - {r['name']}"))
        if balancing > 0:
            lines.append((opening_id, 0.0, abs(balancing), "الطرف المقابل للأرصدة الافتتاحية"))
        elif balancing < 0:
            lines.append((opening_id, abs(balancing), 0.0, "الطرف المقابل للأرصدة الافتتاحية"))

        cur = conn.execute(
            """
            INSERT INTO journal_entries (entry_no, entry_date, source_type, source_id, description, created_by, created_at)
            VALUES (?, ?, 'opening_balance', NULL, ?, NULL, ?)
            """,
            (f"JE-OPEN-{stamp}", cutoff, f"قيد افتتاحي - بداية النظام بتاريخ {cutoff}", now),
        )
        entry_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO journal_lines (entry_id, account_id, debit, credit, description) VALUES (?, ?, ?, ?, ?)",
            [(entry_id, a, d, c, desc) for a, d, c, desc in lines],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    verify(conn, cutoff)
    conn.close()


def verify(conn, cutoff):
    print("\n=== التحقق بعد التنفيذ ===")
    d, c = conn.execute("SELECT COALESCE(SUM(debit),0), COALESCE(SUM(credit),0) FROM journal_lines").fetchone()
    print(f"إجمالي المدين: {money(d)} / الدائن: {money(c)} — {'متوازن' if abs(d - c) < 0.01 else 'غير متوازن!'}")
    print("الفواتير المتبقية:", conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0])
    print("المدفوعات المتبقية:", conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0])
    income = conn.execute(
        """
        SELECT COALESCE(SUM(jl.credit - jl.debit), 0)
        FROM journal_lines jl JOIN account_nodes a ON a.id = jl.account_id
        WHERE a.code LIKE '4%' OR a.code LIKE '5%'
        """
    ).fetchone()[0]
    print(f"رصيد الإيرادات/المصروفات عند البدء: {money(income)} (يجب أن يكون صفراً)")
    mismatch = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT s.id,
                   MAX(COALESCE(s.last_due_amount,0) - COALESCE(s.last_paid_amount,0)) AS card,
                   COALESCE(SUM(jl.debit - jl.credit), 0) AS ledger
            FROM subscribers s
            LEFT JOIN journal_lines jl ON jl.account_id = s.account_node_id
            GROUP BY s.id
            HAVING ABS(MAX(CASE WHEN COALESCE(s.last_due_amount,0) - COALESCE(s.last_paid_amount,0) > 0
                           THEN COALESCE(s.last_due_amount,0) - COALESCE(s.last_paid_amount,0) ELSE 0 END)
                       - COALESCE(SUM(jl.debit - jl.credit), 0)) > 0.01
        )
        """
    ).fetchone()[0]
    print(f"مشتركون يختلف رصيدهم في الدفتر عن بطاقتهم: {mismatch} (يجب أن يكون صفراً)")
    ar = conn.execute(
        """
        SELECT COALESCE(SUM(jl.debit - jl.credit), 0)
        FROM journal_lines jl JOIN subscribers s ON s.account_node_id = jl.account_id
        """
    ).fetchone()[0]
    card = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN COALESCE(last_due_amount,0) - COALESCE(last_paid_amount,0) > 0
                            THEN COALESCE(last_due_amount,0) - COALESCE(last_paid_amount,0) ELSE 0 END), 0)
        FROM subscribers
        """
    ).fetchone()[0]
    print(f"ذمم العملاء في الدفتر: {money(ar)} / في بطاقات المشتركين: {money(card)}")
    print(f"النظام جاهز للبدء من {cutoff}.")


def main():
    parser = argparse.ArgumentParser(description="بداية جديدة: تصفير الحركات مع حفظ متأخرات المشتركين.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--cutoff-date", default=date.today().isoformat())
    parser.add_argument("--apply", action="store_true", help="التنفيذ الفعلي (الافتراضي تجريبي)")
    parser.add_argument("--no-carry-cash", dest="carry_cash", action="store_false",
                        help="عدم ترحيل أرصدة الصناديق/المحصلين (تبدأ بصفر)")
    parser.add_argument("--keep-audit", action="store_true", help="الإبقاء على سجل التدقيق")
    args = parser.parse_args()
    run(Path(args.db), args.cutoff_date, args.apply, args.carry_cash, args.keep_audit)


if __name__ == "__main__":
    main()
