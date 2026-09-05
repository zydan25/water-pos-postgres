from __future__ import annotations

import uuid
from functools import wraps
from pathlib import Path

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from database import (
    get_db,
    process_invoice_payment,
    vouchers_list,
    list_account_balances_flat,
    get_default_cash_account_id,
    get_default_receivable_account_id,
    create_employee_account,
)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_UPLOADS = {"png", "jpg", "jpeg", "gif", "pdf", "webp", "xlsx", "xls", "xml", "csv"}

bp = Blueprint("payments", __name__, template_folder="templates")


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("user_id") is None:
            flash("يرجى تسجيل الدخول أولاً.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@bp.route("/")
@login_required
def index():
    db = get_db()
    vouchers = vouchers_list(db)
    account_rows = list_account_balances_flat(db)
    stats = {
        "invoice_count": db.execute("SELECT COUNT(*) c FROM invoices").fetchone()["c"],
        "payment_count": db.execute("SELECT COUNT(*) c FROM payments").fetchone()["c"],
        "invoice_total": db.execute("SELECT COALESCE(SUM(remaining_amount),0) s FROM invoices").fetchone()["s"],
        "payment_total": db.execute("SELECT COALESCE(SUM(amount),0) s FROM payments").fetchone()["s"],
    }
    return render_template(
        "vouchers.html",
        vouchers=vouchers,
        account_rows=account_rows,
        stats=stats,
        user_role=(g.user["role"] or "").lower() if g.user else None,
    )


@bp.route("/add", methods=["POST"])
@login_required
def add():
    db = get_db()
    invoice_id = request.form.get("invoice_id", type=int)
    try:
        amount = float(request.form.get("amount") or 0)
    except ValueError:
        amount = 0
    if not invoice_id or amount <= 0:
        flash("أدخل فاتورة ومبلغاً صحيحاً.", "danger")
        return redirect(url_for("payments.index"))

    method = request.form.get("method", "cash")
    note = request.form.get("note", "")
    attachment = request.files.get("attachment")
    attachment_path = None
    if attachment and attachment.filename:
        ext = attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else ""
        if ext not in ALLOWED_UPLOADS:
            flash("نوع المرفق غير مسموح به.", "danger")
            return redirect(url_for("payments.index"))
        stored_name = f"{uuid.uuid4().hex}.{ext}"
        file_path = UPLOAD_DIR / stored_name
        attachment.save(str(file_path))
        attachment_path = f"uploads/{stored_name}"

    try:
        if (g.user["role"] or "").lower() != "admin":
            account_id = create_employee_account(
                db,
                session["user_id"],
                full_name=(g.user["username"] if g.user else None),
                job_title=(g.user["role"] or "Collector"),
            )
        else:
            account_id = request.form.get("account_id", type=int) or get_default_cash_account_id(db)

        process_invoice_payment(
            db,
            invoice_id=invoice_id,
            user_id=session["user_id"],
            amount=amount,
            method=method,
            notes=note,
            attachment_path=attachment_path,
            wallet_id=None,
            cash_account_id=account_id,
            receivable_account_id=get_default_receivable_account_id(db),
        )
        db.commit()
        flash("تم تسجيل التسديد بنجاح.", "success")
    except Exception as exc:
        db.rollback()
        flash(f"حدث خطأ أثناء التسديد: {exc}", "danger")
    return redirect(url_for("payments.index"))
