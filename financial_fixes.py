
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Iterable
import io
import zipfile

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, session, url_for

try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
except Exception as _weasyprint_import_error:
    print("weasyprint unavailable:", _weasyprint_import_error)
    WEASYPRINT_AVAILABLE = False

from database import (
    DEFAULT_SETTINGS,
    get_db,
    get_default_cash_account_id,
    get_default_receivable_account_id,
    get_opening_balance,
    get_setting,
    log_audit,
    post_journal_entry,
    process_invoice_payment,
)

manual_collection_bp = Blueprint("manual_collection", __name__, template_folder="templates")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)



def _render_pdf_bytes(template_name: str, **context) -> bytes:
    """Render a standalone PDF template quickly without loading base.html/bootstrap."""
    if not WEASYPRINT_AVAILABLE:
        raise ValueError(
            "توليد PDF عبر WeasyPrint غير متاح على هذا الجهاز (مكتبات GTK/Pango غير مثبتة). "
            "قم بتثبيت مكتبات GTK من https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#windows."
        )
    html = render_template(template_name, **context)
    return HTML(string=html, base_url=str(BASE_DIR)).write_pdf()


def _send_pdf(pdf_bytes: bytes, download_name: str):
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=download_name,
    )


def _send_zip(build_fn, download_name: str):
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        build_fn(zf)
    mem.seek(0)
    return send_file(
        mem,
        mimetype="application/zip",
        as_attachment=True,
        download_name=download_name,
    )


def _safe_filename(name: str) -> str:
    name = (name or "").strip() or "غير_محدد"
    return name.replace("/", "_").replace("\\", "_").replace(":", "_")
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("user_id") is None:
            flash("يرجى تسجيل الدخول أولاً.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapper


def role_required(allowed_roles: Iterable[str]):
    allowed = {str(role).lower() for role in allowed_roles}

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = getattr(g, "user", None)
            role = str(user["role"]).lower() if user and user.get("role") else ""
            if not role or role not in allowed:
                flash("ليس لديك الصلاحية للوصول إلى هذه الصفحة.", "danger")
                return redirect(url_for("index"))
            return f(*args, **kwargs)

        return wrapper

    return decorator


def _is_truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "نعم", "فعال"}


def invoice_month_key(invoice_date: str | None, month_label: str | None) -> str | None:
    label = (month_label or "").strip()
    if label:
        return label[:7]
    value = (invoice_date or "").strip()
    return value[:7] if value else None


def invoice_month_uniqueness_enabled() -> bool:
    return _is_truthy(get_setting("enforce_one_invoice_per_month", "1"))


def ensure_unique_invoice_month(db, subscriber_id: int, invoice_date: str | None, month_label: str | None, exclude_invoice_id: int | None = None) -> None:
    if not invoice_month_uniqueness_enabled():
        return
    month_key = invoice_month_key(invoice_date, month_label)
    if not month_key:
        return
    sql = """
        SELECT id, invoice_no, invoice_date, month_label
        FROM invoices
        WHERE subscriber_id = ?
          AND substr(COALESCE(month_label, invoice_date), 1, 7) = ?
    """
    params = [subscriber_id, month_key]
    if exclude_invoice_id:
        sql += " AND id != ?"
        params.append(exclude_invoice_id)
    row = db.execute(sql, params).fetchone()
    if row:
        raise ValueError(f"يوجد بالفعل فاتورة لهذا المشترك في شهر {month_key} (الفاتورة رقم {row['invoice_no']}).")


def recalculate_invoice_payment_totals(db, invoice_id: int):
    invoice = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not invoice:
        raise ValueError("الفاتورة غير موجودة.")
    paid = db.execute("SELECT COALESCE(SUM(amount), 0) s FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()["s"] or 0
    total = float(invoice["total_amount"] or 0)
    paid_f = float(paid or 0)
    remaining = max(0.0, total - paid_f)
    credit = max(0.0, paid_f - total)
    db.execute(
        """
        UPDATE invoices
        SET paid_amount=?, remaining_amount=?, credit_amount=?, updated_at=?
        WHERE id=?
        """,
        (paid_f, remaining, credit, datetime.now().isoformat(), invoice_id),
    )
    return paid_f, remaining, credit


def _reverse_journal_entry(db, entry_id: int, *, created_by: int | None, source_type: str, source_id: int | None, description: str):
    entry = db.execute("SELECT * FROM journal_entries WHERE id=?", (entry_id,)).fetchone()
    if not entry:
        return None
    lines = db.execute(
        "SELECT account_id, debit, credit, description FROM journal_lines WHERE entry_id=? ORDER BY id ASC",
        (entry_id,),
    ).fetchall()
    reversed_lines = []
    for line in lines:
        debit = float(line["credit"] or 0)
        credit = float(line["debit"] or 0)
        reversed_lines.append((line["account_id"], debit, credit, f"عكس: {line['description'] or description}"))
    if not reversed_lines:
        return None
    reversed_entry = post_journal_entry(
        db,
        entry_date=(entry["entry_date"] or date.today().isoformat()),
        source_type=source_type,
        source_id=source_id,
        description=description,
        created_by=created_by,
        lines=reversed_lines,
    )
    return reversed_entry



def void_invoice_with_reversal(db, invoice_id: int, user_id: int | None = None):
    invoice = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not invoice:
        raise ValueError("الفاتورة غير موجودة.")

    # إلغاء/عكس سندات الفاتورة الآلية إن وجدت، مع عكس القيد المرتبط بها.
    invoice_vouchers = db.execute(
        "SELECT * FROM accounting_vouchers WHERE source_type='invoice' AND source_id=? ORDER BY id DESC",
        (invoice_id,),
    ).fetchall()
    invoice_voucher = invoice_vouchers[0] if invoice_vouchers else None
    for voucher in invoice_vouchers:
        if voucher["journal_entry_id"]:
            _reverse_journal_entry(
                db,
                int(voucher["journal_entry_id"]),
                created_by=user_id,
                source_type="reversal:invoice",
                source_id=invoice_id,
                description=f"عكس قيد الفاتورة رقم {invoice['invoice_no']}",
            )
        db.execute(
            "UPDATE accounting_vouchers SET status='void', updated_at=? WHERE id=?",
            (datetime.now().isoformat(), voucher["id"]),
        )

    # إذا كان هناك ترحيل قديم على مستوى القيد فقط فنعكسه أيضاً.
    if invoice["journal_entry_id"] and (not invoice_voucher or int(invoice_voucher["journal_entry_id"] or 0) != int(invoice["journal_entry_id"])):
        _reverse_journal_entry(
            db,
            int(invoice["journal_entry_id"]),
            created_by=user_id,
            source_type="reversal:invoice",
            source_id=invoice_id,
            description=f"عكس قيد الفاتورة رقم {invoice['invoice_no']}",
        )

    # عكس سندات التحصيل المرتبطة بالسداد إن وجدت
    payments = db.execute(
        "SELECT * FROM payments WHERE invoice_id=? ORDER BY id ASC",
        (invoice_id,),
    ).fetchall()
    for payment in payments:
        voucher = db.execute(
            "SELECT * FROM accounting_vouchers WHERE source_type='payment' AND source_id=? ORDER BY id DESC LIMIT 1",
            (payment["id"],),
        ).fetchone()
        if voucher and voucher["journal_entry_id"]:
            _reverse_journal_entry(
                db,
                int(voucher["journal_entry_id"]),
                created_by=user_id,
                source_type="reversal:payment",
                source_id=payment["id"],
                description=f"عكس سند قبض السداد رقم {payment['id']}",
            )
            db.execute(
                "UPDATE accounting_vouchers SET status='void', updated_at=? WHERE id=?",
                (datetime.now().isoformat(), voucher["id"]),
            )

    # حذف المرفقات والملفات
    attachments = db.execute("SELECT id, path FROM attachments WHERE invoice_id=?", (invoice_id,)).fetchall()
    for att in attachments:
        try:
            if att["path"]:
                fpath = BASE_DIR / str(att["path"])
                if fpath.exists() and fpath.is_file():
                    fpath.unlink()
        except Exception:
            pass
    db.execute("DELETE FROM attachments WHERE invoice_id=?", (invoice_id,))

    # حذف الزيارات المرتبطة بالجولة حتى لا تبقى مراجع يتيمة
    db.execute("DELETE FROM trip_visits WHERE invoice_id=?", (invoice_id,))

    # حذف المدفوعات وملحقاتها
    for payment in payments:
        try:
            if payment["attachment_path"]:
                fpath = BASE_DIR / str(payment["attachment_path"])
                if fpath.exists() and fpath.is_file():
                    fpath.unlink()
        except Exception:
            pass
        db.execute("DELETE FROM payments WHERE id=?", (payment["id"],))

    db.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
    log_audit(db, user_id, "DELETE", "invoices", invoice_id, f"حذف/إلغاء الفاتورة رقم {invoice['invoice_no']} مع عكس القيود المحاسبية.")
    return True
def _current_village(row) -> str:
    village = ""
    if row is None:
        return "غير محدد"
    try:
        village = (row["village"] or "").strip()
    except Exception:
        village = ""
    if village:
        return village
    try:
        village = (row["address"] or "").strip()
    except Exception:
        village = ""
    return village or "غير محدد"


def _village_rows(db, only_unpaid: bool = False):
    where = "WHERE 1=1"
    if only_unpaid:
        where += " AND COALESCE(i.remaining_amount, 0) > 0"
    rows = db.execute(
        f"""
        SELECT
               s.id,
               COALESCE(s.account_number, '') AS account_number,
               COALESCE(s.name, '') AS name,
               COALESCE(s.phone, '') AS phone,
               COALESCE(s.village, '') AS village,
               COALESCE(s.address, '') AS address,
               COALESCE(s.meter_number, '') AS meter_number,
               s.default_unit_price,
               s.default_subscription_fee,
               i.id AS invoice_id,
               i.invoice_no,
               i.invoice_date,
               i.month_label,
               i.previous_reading,
               i.current_reading,
               COALESCE(i.current_reading, i.previous_reading, s.last_reading, 0) AS last_reading,
               i.total_amount,
               i.paid_amount,
               i.remaining_amount,
               i.credit_amount,
               i.opening_balance
        FROM subscribers s
        LEFT JOIN invoices i ON i.id = (
            SELECT i2.id
            FROM invoices i2
            WHERE i2.subscriber_id = s.id
            ORDER BY i2.id DESC
            LIMIT 1
        )
        {where}
        ORDER BY COALESCE(s.address, s.name), s.account_number
        """
    ).fetchall()
    bucket = defaultdict(list)
    for row in rows:
        bucket[_current_village(row)].append(row)
    return bucket


def _manual_collection_common_context(db, only_unpaid: bool = False):
    villages = _village_rows(db, only_unpaid=only_unpaid)
    village_names = sorted(villages.keys())
    total_subscribers = sum(len(v) for v in villages.values())
    total_invoices = db.execute("SELECT COUNT(*) c FROM invoices").fetchone()["c"]
    total_amount = db.execute("SELECT COALESCE(SUM(total_amount), 0) s FROM invoices").fetchone()["s"]
    total_paid = db.execute("SELECT COALESCE(SUM(paid_amount), 0) s FROM invoices").fetchone()["s"]
    total_remaining = db.execute("SELECT COALESCE(SUM(remaining_amount), 0) s FROM invoices").fetchone()["s"]
    return {
        "villages": villages,
        "village_names": village_names,
        "total_subscribers": total_subscribers,
        "total_invoices": total_invoices,
        "total_amount": float(total_amount or 0),
        "total_paid": float(total_paid or 0),
        "total_remaining": float(total_remaining or 0),
        "currency": get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]),
        "organization_name": get_setting("organization_name", DEFAULT_SETTINGS["organization_name"]),
        "project_name": get_setting("project_name", DEFAULT_SETTINGS["project_name"]),
        "print_date": date.today().isoformat(),
    }


@manual_collection_bp.route("/manual-collection")
@login_required
@role_required(["admin", "staff", "collector", "accountant", "manager", "technician"])
def manual_collection_home():
    db = get_db()
    ctx = _manual_collection_common_context(db, only_unpaid=False)
    latest_unpaid = db.execute(
        """
        SELECT i.id, i.invoice_no, i.invoice_date, i.month_label, i.remaining_amount,
               s.name subscriber_name, s.account_number, s.address
        FROM invoices i
        JOIN subscribers s ON s.id = i.subscriber_id
        WHERE COALESCE(i.remaining_amount, 0) > 0
        ORDER BY i.invoice_date DESC, i.id DESC
        LIMIT 20
        """
    ).fetchall()
    return render_template("manual_collection.html", latest_unpaid=latest_unpaid, **ctx)


@manual_collection_bp.route("/manual-collection/readings")
@login_required
@role_required(["admin", "staff", "collector", "technician"])
def manual_collection_readings():
    db = get_db()
    village = request.args.get("village", "").strip()
    all_rows = _manual_collection_common_context(db)
    if village:
        all_rows["villages"] = {village: all_rows["villages"].get(village, [])}
        all_rows["village_names"] = [village]
    return render_template("manual_readings_sheet.html", print_mode=False, village=village, **all_rows)


@manual_collection_bp.route("/manual-collection/readings/pdf")
@login_required
@role_required(["admin", "staff", "collector", "technician"])
def manual_collection_readings_pdf():
    db = get_db()
    village = request.args.get("village", "").strip()
    mode = (request.args.get("mode", "pdf") or "pdf").strip().lower()
    ctx = _manual_collection_common_context(db)
    if village:
        ctx["villages"] = {village: ctx["villages"].get(village, [])}
        ctx["village_names"] = [village]

    if mode == "zip":
        def build(zf):
            target_items = ctx["villages"].items() if not village else [(village, ctx["villages"].get(village, []))]
            for village_name, rows in target_items:
                local_ctx = dict(ctx)
                local_ctx["village"] = village_name
                local_ctx["villages"] = {village_name: rows}
                local_ctx["village_names"] = [village_name]
                pdf = _render_pdf_bytes("manual_readings_pdf.html", print_mode=True, **local_ctx)
                zf.writestr(f"كشف_قراءة_{_safe_filename(village_name)}.pdf", pdf)
        return _send_zip(build, f"كشف_قراءة_{_safe_filename(village or 'جميع_القرى')}.zip")

    pdf = _render_pdf_bytes("manual_readings_pdf.html", print_mode=True, village=village, **ctx)
    return _send_pdf(pdf, f"كشف_قراءة_{_safe_filename(village or 'جميع_القرى')}.pdf")


@manual_collection_bp.route("/manual-collection/collections")
@login_required
@role_required(["admin", "staff", "collector", "accountant", "manager", "technician"])
def manual_collection_collections():
    db = get_db()
    village = request.args.get("village", "").strip()
    ctx = _manual_collection_common_context(db, only_unpaid=True)
    if village:
        ctx["villages"] = {village: ctx["villages"].get(village, [])}
        ctx["village_names"] = [village]
    return render_template("manual_collections_sheet.html", print_mode=False, village=village, **ctx)


@manual_collection_bp.route("/manual-collection/collections/pdf")
@login_required
@role_required(["admin", "staff", "collector", "accountant", "manager", "technician"])
def manual_collection_collections_pdf():
    db = get_db()
    village = request.args.get("village", "").strip()
    mode = (request.args.get("mode", "pdf") or "pdf").strip().lower()
    ctx = _manual_collection_common_context(db, only_unpaid=True)
    if village:
        ctx["villages"] = {village: ctx["villages"].get(village, [])}
        ctx["village_names"] = [village]

    if mode == "zip":
        def build(zf):
            target_items = ctx["villages"].items() if not village else [(village, ctx["villages"].get(village, []))]
            for village_name, rows in target_items:
                local_ctx = dict(ctx)
                local_ctx["village"] = village_name
                local_ctx["villages"] = {village_name: rows}
                local_ctx["village_names"] = [village_name]
                pdf = _render_pdf_bytes("manual_collections_pdf.html", print_mode=True, **local_ctx)
                zf.writestr(f"كشف_تحصيل_{_safe_filename(village_name)}.pdf", pdf)
        return _send_zip(build, f"كشف_تحصيل_{_safe_filename(village or 'جميع_القرى')}.zip")

    pdf = _render_pdf_bytes("manual_collections_pdf.html", print_mode=True, village=village, **ctx)
    return _send_pdf(pdf, f"كشف_تحصيل_{_safe_filename(village or 'جميع_القرى')}.pdf")


@manual_collection_bp.route("/manual-collection/summary")
@login_required
@role_required(["admin", "staff", "collector", "accountant", "manager", "technician"])
def manual_collection_summary():
    db = get_db()
    villages = _village_rows(db, only_unpaid=False)
    summary = []
    for village, rows in villages.items():
        total = sum(float(r["total_amount"] or 0) for r in rows if r["invoice_id"])
        remaining = sum(float(r["remaining_amount"] or 0) for r in rows if r["invoice_id"])
        paid = sum(float(r["paid_amount"] or 0) for r in rows if r["invoice_id"])
        count = sum(1 for r in rows if r["invoice_id"])
        summary.append({"village": village, "count": count, "total": total, "paid": paid, "remaining": remaining})
    summary.sort(key=lambda x: x["village"])
    return render_template(
        "manual_summary.html",
        summary=summary,
        currency=get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]),
    )




@manual_collection_bp.route("/manual-collection/financial-reset", methods=["POST"])
@login_required
@role_required(["admin"])
def manual_collection_financial_reset():
    db = get_db()
    confirm = (request.form.get("confirm_text") or "").strip()
    if confirm != "RESET":
        flash("اكتب RESET للتأكيد قبل التصفير المالي.", "danger")
        return redirect(url_for("manual_collection.manual_collection_home"))

    try:
        backup_path = financial_reset(db, clear_opening_snapshots=True)
        db.commit()
        flash(f"تم التصفير المالي بنجاح. تم حفظ نسخة احتياطية تلقائية في: {backup_path}", "warning")
    except Exception as exc:
        db.rollback()
        flash(f"تعذر تنفيذ التصفير المالي: {exc}", "danger")
    return redirect(url_for("manual_collection.manual_collection_home"))
@manual_collection_bp.route("/manual-collection/go-bulk-readings")
@login_required
def manual_collection_go_bulk_readings():
    return redirect(url_for("invoices_bulk_readings"))


@manual_collection_bp.route("/manual-collection/go-bulk-payments")
@login_required
@role_required(["admin", "staff", "collector", "accountant", "manager", "technician"])
def manual_collection_go_bulk_payments():
    return redirect(url_for("manual_collection.bulk_payments"))


@manual_collection_bp.route("/manual-collection/bulk-payments", methods=["GET", "POST"])
@login_required
@role_required(["admin", "staff", "collector", "accountant", "manager", "technician"])
def bulk_payments():
    db = get_db()
    if request.method == "POST":
        invoice_ids = request.form.getlist("invoice_id")
        amounts = request.form.getlist("amount")
        methods = request.form.getlist("method")
        notes_list = request.form.getlist("note")
        processed = 0
        for idx, invoice_id in enumerate(invoice_ids):
            try:
                inv_id = int(invoice_id)
            except Exception:
                continue
            try:
                amount = float((amounts[idx] if idx < len(amounts) else "") or 0)
            except Exception:
                amount = 0
            if amount <= 0:
                continue
            method = (methods[idx] if idx < len(methods) else "نقداً") or "نقداً"
            note = (notes_list[idx] if idx < len(notes_list) else "") or ""
            invoice = db.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
            if not invoice:
                continue
            try:
                process_invoice_payment(
                    db,
                    invoice_id=inv_id,
                    user_id=session["user_id"],
                    amount=amount,
                    method=method,
                    notes=note,
                    attachment_path=None,
                    wallet_id=None,
                    cash_account_id=get_default_cash_account_id(db),
                    receivable_account_id=get_default_receivable_account_id(db),
                )
                processed += 1
            except Exception as exc:
                flash(f"تعذر تسجيل السداد للفاتورة {invoice['invoice_no']}: {exc}", "danger")
        db.commit()
        flash(f"تم تسجيل {processed} سداد/سداداً متعددًا بنجاح.", "success" if processed else "warning")
        return redirect(url_for("manual_collection.bulk_payments"))

    invoices = db.execute(
        """
        SELECT i.id, i.invoice_no, i.invoice_date, i.month_label, i.remaining_amount,
               s.name subscriber_name, s.account_number, s.address, s.phone
        FROM invoices i
        JOIN subscribers s ON s.id = i.subscriber_id
        WHERE COALESCE(i.remaining_amount, 0) > 0
        ORDER BY i.invoice_date DESC, i.id DESC
        LIMIT 50
        """
    ).fetchall()
    return render_template("manual_bulk_payments.html", invoices=invoices)
