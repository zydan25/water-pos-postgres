import base64
import csv
import io
import os
import secrets
import threading
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, date, timedelta
from functools import wraps
from pathlib import Path
import time

#from payments import bp as payments_bp


import qrcode
import requests
from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request,
    send_file, send_from_directory, session, url_for
)
from openpyxl import Workbook, load_workbook
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

# استيراد الأدوات والدوال المعزولة من ملف database.py المطور
from database import (
    get_db, init_db, get_setting, set_setting, user_name,
    SQLAlchemyDBProxy,
    get_opening_balance, next_invoice_no, next_subscriber_account_number, process_invoice_payment,
    post_journal_entry, log_transaction, log_audit, add_user, link_user_wallet, DEFAULT_SETTINGS,
    get_account_tree, add_account_node, ensure_employee_profile,
    create_employee_account, ensure_subscriber_account, ensure_all_party_accounts, archive_subscriber_account, post_invoice_accounting,
    list_account_balances_flat, get_account_balance_rows,
    create_accounting_voucher, update_accounting_voucher, void_accounting_voucher,
    vouchers_list, get_voucher_by_id, list_journal_entries, get_journal_entry_by_id,
    create_manual_journal_entry, update_manual_journal_entry, delete_manual_journal_entry, find_main_account_direct_balances,
    get_ledger, get_trial_balance, get_income_statement, get_balance_sheet,
    ensure_fiscal_tables, list_fiscal_years, create_fiscal_year, close_fiscal_year, reopen_fiscal_year,
    create_opening_entry,
    is_date_in_closed_year, get_account_closing_balance, save_cash_count, list_cash_counts,
    get_daily_collection_summary, get_receivable_reconciliation,
    get_cash_flow, get_equity_statement,
    save_bank_statement, get_bank_reconciliation,
    review_cash_count, approve_cash_count, get_count_minutes,
    get_accountant_dashboard, auto_adjust_difference,
    ensure_template_tables, list_templates, create_template, delete_template, apply_template,
    get_village_profitability,
    get_default_cash_account_id, get_default_receivable_account_id,
    get_default_expense_account_id, get_default_income_account_id,
    get_employee_account_node_id, get_subscriber_opening_snapshot,
    backup_database_bytes, replace_database_from_file, database_file_path
)
from models import init_accounting, SessionLocal, engine, account_tree, find_account, record_journal_entry, AccountNode, EmployeeProfile, Invoice as SAInvoice, Payment as SAPayment, Subscriber as SASubscriber, Wallet as SAWallet, User as SAUser
from financial_fixes import (
    manual_collection_bp,
    ensure_unique_invoice_month,
    recalculate_invoice_payment_totals,
    void_invoice_with_reversal,
    invoice_month_uniqueness_enabled,
    _render_pdf_bytes,
    _send_pdf,
)

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception as e:
    PLAYWRIGHT_AVAILABLE = False


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"

ALLOWED_UPLOADS = {"png", "jpg", "jpeg", "gif", "pdf", "webp", "xlsx", "xls", "xml", "csv"}


def _load_or_create_secret_key():
    env_key = os.environ.get("SECRET_KEY", "").strip()
    if env_key:
        return env_key
    key_file = BASE_DIR / "instance" / "secret.key"
    try:
        if key_file.exists():
            stored = key_file.read_text(encoding="utf-8").strip()
            if stored:
                return stored
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(key, encoding="utf-8")
    except OSError:
        pass
    return key


_LOGIN_FAILURES = {}
MAX_LOGIN_FAILS = 8
LOGIN_WINDOW_SECONDS = 15 * 60


def _prune_login_failures():
    now = time.time()
    expired = [k for k, (count, last) in _LOGIN_FAILURES.items() if now - last > LOGIN_WINDOW_SECONDS]
    for k in expired:
        _LOGIN_FAILURES.pop(k, None)


# --- الديكورات والدوال المساعدة للمسارات والواجهات ---



# --- مسار واجهة مراقبة الموظفين والنشاط (للمدير والفني) ---



def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("user_id") is None:
            flash("يرجى تسجيل الدخول أولاً.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function
#app.register_blueprint(payments_bp, url_prefix='/payments')
def role_required(allowed_roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login"))
            user_role = g.user["role"] if g.user else None
            if not user_role or user_role.lower() not in [r.lower() for r in allowed_roles]:
                flash("ليس لديك الصلاحية للوصول إلى هذه الصفحة أو تنفيذ هذه العملية.", "danger")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapper
    return decorator

def parse_decimal(value):
    if value is None or str(value).strip() == "":
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


_AR_ONES = [
    "", "واحد", "اثنان", "ثلاثة", "أربعة", "خمسة", "ستة", "سبعة", "ثمانية", "تسعة",
    "عشرة", "أحد عشر", "اثنا عشر", "ثلاثة عشر", "أربعة عشر", "خمسة عشر",
    "ستة عشر", "سبعة عشر", "ثمانية عشر", "تسعة عشر",
]
_AR_TENS = ["", "عشرة", "عشرون", "ثلاثون", "أربعون", "خمسون", "ستون", "سبعون", "ثمانون", "تسعون"]
_AR_HUNDREDS = ["", "مئة", "مئتان", "ثلاثمئة", "أربعمئة", "خمسمئة", "ستمئة", "سبعمئة", "ثمانمئة", "تسعمئة"]


def _tafqit_sub100(n):
    if n < 20:
        return _AR_ONES[n]
    tens, units = divmod(n, 10)
    if units == 0:
        return _AR_TENS[tens]
    u = "أحد" if units == 1 else ("اثنان" if units == 2 else _AR_ONES[units])
    return f"{u} و{_AR_TENS[tens]}"


def _tafqit_sub1000(n):
    parts = []
    h, rest = divmod(n, 100)
    if h:
        parts.append(_AR_HUNDREDS[h])
    if rest:
        parts.append(_tafqit_sub100(rest))
    return " و".join(parts)


def number_in_arabic_words(value):
    """تحويل رقم صحيح إلى كلمات عربية (تفقيط) حتى المليارات."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return "صفر"
    if n == 0:
        return "صفر"
    if n < 0:
        return "ناقص " + number_in_arabic_words(-n)
    groups = []
    for divisor, (one, two, many) in (
        (10**9, ("مليار", "ملياران", "مليارات")),
        (10**6, ("مليون", "مليونان", "ملايين")),
        (10**3, ("ألف", "ألفان", "آلاف")),
    ):
        cnt, n = divmod(n, divisor)
        if cnt == 0:
            continue
        if cnt == 1:
            groups.append(one)
        elif cnt == 2:
            groups.append(two)
        elif cnt <= 10:
            groups.append(f"{number_in_arabic_words(cnt)} {many}")
        else:
            groups.append(f"{number_in_arabic_words(cnt)} {one}")
    if n:
        groups.append(_tafqit_sub1000(n))
    return " و".join(groups)


def tafqit_filter(amount, currency=None):
    words = number_in_arabic_words(amount)
    if currency:
        words = f"{words} {currency}"
    return words


def num_filter(value, decimals=0):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "0"
    if decimals:
        return f"{f:,.{int(decimals)}f}"
    return f"{int(round(f)):,}"


def deadline_filter(iso_date, days=15):
    """حساب تاريخ استحقاق الدفع: تاريخ إصدار الفاتورة + عدد الأيام."""
    if not iso_date:
        return ""
    parsed = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(str(iso_date).strip()[:10], fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return ""
    return (parsed + timedelta(days=int(days))).strftime("%d / %m / %Y")

def now_iso():
    return datetime.now().isoformat()

def _safe_row_value(row, key, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _selected_account_ids_from_request():
    ids = []
    try:
        raw_values = request.values.getlist("account_ids")
    except Exception:
        raw_values = []
    for value in raw_values:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids


_HIDDEN_ROOT_KEYWORDS = (
    "الأصول",
    "الخصوم",
    "حقوق الملكية",
    "assets",
    "liabilities",
    "equity",
)


def _is_hidden_big_root(node):
    node_type = str(_safe_row_value(node, "node_type", "") or "").strip().lower()
    name = str(_safe_row_value(node, "name", "") or "").strip().lower()
    parent_id = _safe_row_value(node, "parent_id", None)
    if parent_id is not None:
        return False
    if node_type != "root":
        return False
    return any(keyword.lower() in name for keyword in _HIDDEN_ROOT_KEYWORDS)


def flatten_account_options(tree, only_postable=False, depth=0):
    options = []
    for node in tree or []:
        if not isinstance(node, dict):
            continue
        children = node.get("children") or []
        if _is_hidden_big_root(node):
            options.extend(flatten_account_options(children, only_postable=only_postable, depth=depth))
            continue
        if only_postable and not node.get("is_postable"):
            options.extend(flatten_account_options(children, only_postable=only_postable, depth=depth + 1))
            continue
        label = f"{'—' * depth} {node.get('name', '')}".strip()
        options.append({
            "id": node.get("id"),
            "label": label,
            "code": node.get("code"),
            "name": node.get("name"),
            "depth": depth,
            "is_postable": node.get("is_postable", False),
        })
        options.extend(flatten_account_options(children, only_postable=only_postable, depth=depth + 1))
    return options


def promote_visible_account_tree(nodes):
    """Hide only the major root groups (assets/liabilities/equity) and keep the rest visible."""
    visible = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        children = promote_visible_account_tree(node.get("children") or [])
        node_copy = dict(node)
        node_copy["children"] = children
        if _is_hidden_big_root(node_copy):
            visible.extend(children)
        else:
            visible.append(node_copy)
    return visible


def flatten_parent_accounts(nodes, depth=0, exclude_id=None):
    """Build a selectable list of parent accounts while hiding the major root groups."""
    options = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if exclude_id is not None and node.get("id") == exclude_id:
            continue
        children = node.get("children") or []
        if _is_hidden_big_root(node):
            options.extend(flatten_parent_accounts(children, depth=depth, exclude_id=exclude_id))
            continue
        is_candidate = (not node.get("is_postable")) or bool(children)
        if is_candidate:
            options.append({
                "id": node.get("id"),
                "label": f"{'—' * depth} {node.get('name', '')}".strip(),
                "code": node.get("code"),
                "name": node.get("name"),
                "node_type": node.get("node_type"),
                "depth": depth,
                "is_postable": bool(node.get("is_postable")),
                "has_children": bool(children),
            })
        options.extend(flatten_parent_accounts(children, depth=depth + 1, exclude_id=exclude_id))
    return options


def infer_child_account_type(parent_row):
    if not parent_row:
        return "account"
    parent_type = str(_safe_row_value(parent_row, "node_type", "") or "").lower()
    if parent_type and parent_type != "root":
        return parent_type
    code = str(_safe_row_value(parent_row, "code", "") or "")
    if code.startswith("11"):
        return "cash"
    if code.startswith("12"):
        return "receivable"
    if code.startswith("13"):
        return "employee"
    if code.startswith("21"):
        return "payable"
    if code.startswith("41"):
        return "income"
    if code.startswith("51"):
        return "expense"
    return "account"


def next_child_account_code(db, parent_id):
    parent = db.execute("SELECT id, code FROM account_nodes WHERE id=? AND active=1", (parent_id,)).fetchone()
    if not parent:
        return ""
    base = str(parent["code"] or "").strip()
    if not base:
        return ""
    siblings = db.execute(
        "SELECT code FROM account_nodes WHERE parent_id=? AND code IS NOT NULL AND TRIM(code)<>'' ORDER BY code",
        (parent_id,),
    ).fetchall()
    max_suffix = 0
    for row in siblings:
        code = str(row["code"] or "").strip()
        if code.startswith(base):
            suffix = code[len(base):]
            if suffix.isdigit():
                max_suffix = max(max_suffix, int(suffix))
    return f"{base}{max_suffix + 1:02d}"


def prepare_account_form_payload(db, form, existing_account=None):
    account_id = existing_account["id"] if existing_account else None
    code = (form.get("code") or "").strip()
    name = (form.get("name") or "").strip()
    parent_id = form.get("parent_id", type=int)
    is_postable = 1 if form.get("is_postable") == "1" else 0
    node_type = (form.get("node_type") or "").strip() or None

    parent_row = None
    if parent_id:
        parent_row = db.execute(
            "SELECT id, code, name, node_type, parent_id, is_postable FROM account_nodes WHERE id=? AND active=1",
            (parent_id,),
        ).fetchone()
        if not parent_row:
            raise ValueError("parent_not_found")

    if parent_id and not code:
        code = next_child_account_code(db, parent_id)
    if not node_type:
        if parent_row:
            node_type = infer_child_account_type(parent_row)
        else:
            node_type = "account"

    # If editing, preserve an explicit code unless left blank.
    if existing_account and not code:
        code = existing_account["code"]

    return {
        "id": account_id,
        "code": code,
        "name": name,
        "parent_id": parent_id,
        "node_type": node_type,
        "is_postable": is_postable,
        "parent_row": parent_row,
    }

def resolve_payment_accounts(db, user_id, requested_cash_account_id=None):
    wallet_id = 1
    row = db.execute("SELECT wallet_id FROM users WHERE id = ?", (user_id,)).fetchone()
    if row and row["wallet_id"]:
        wallet_id = row["wallet_id"]
    cash_account_id = requested_cash_account_id or get_default_cash_account_id(db)
    receivable_account_id = get_default_receivable_account_id(db)
    if not cash_account_id:
        cash_account_id = get_default_cash_account_id(db)
    if not receivable_account_id:
        receivable_account_id = get_default_receivable_account_id(db)
    emp_node = get_employee_account_node_id(db, user_id)
    if emp_node and not cash_account_id:
        cash_account_id = emp_node
    return wallet_id or 1, cash_account_id, receivable_account_id

def safe_current_reading(value, previous_reading=None):
    raw = str(value or "").strip()
    if raw == "":
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    if previous_reading is not None and val < float(previous_reading or 0):
        return None
    return val

def save_upload(file):
    if file and file.filename:
        ext = file.filename.split(".")[-1].lower()
        if ext in ALLOWED_UPLOADS:
            filename = f"{uuid.uuid4().hex}.{ext}"
            file.save(UPLOAD_DIR / filename)
            return filename
    return None

def make_qr_data(invoice):
    return f"Invoice:{invoice['invoice_no']}|Total:{invoice['total_amount']}|Remaining:{invoice['remaining_amount']}"


def qr_image_uri(payload, size=180):
    """توليد صورة QR بصيغة base64 data-URI لعرضها داخل قالب الطباعة."""
    try:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(payload)
        qr.make(fit=True)
        buf = io.BytesIO()
        qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
        return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"
    except Exception:
        return None


def fetch_previous_invoice_summary(db, subscriber_id, current_invoice_id=None):
    """إرجاع آخر فاتورة سابقة للمشترك لعرض جدول الشهر السابق."""
    if current_invoice_id:
        query = """
            SELECT invoice_no, invoice_date, month_label, total_amount, paid_amount, remaining_amount, credit_amount
            FROM invoices
            WHERE subscriber_id = ? AND id < ?
            ORDER BY id DESC
            LIMIT 1
        """
        params = (subscriber_id, current_invoice_id)
    else:
        query = """
            SELECT invoice_no, invoice_date, month_label, total_amount, paid_amount, remaining_amount, credit_amount
            FROM invoices
            WHERE subscriber_id = ?
            ORDER BY id DESC
            LIMIT 1
        """
        params = (subscriber_id,)
    return db.execute(query, params).fetchone()


def previous_reading_date_for(db, subscriber_id, before_date):
    """تاريخ آخر قراءة سابقة معروفة للمشترك قبل تاريخ محدد (من الفواتير أو القراءات الجماعية)."""
    if not subscriber_id or not before_date:
        return None
    row = db.execute(
        """
        SELECT dr.date AS d FROM (
            SELECT current_reading_date AS date, id AS ord, 0 AS pr
            FROM invoices
            WHERE subscriber_id=? AND current_reading_date IS NOT NULL AND current_reading_date < ?
            UNION ALL
            SELECT reading_date AS date, id AS ord, 1 AS pr
            FROM bulk_readings
            WHERE subscriber_id=? AND reading_date < ? AND COALESCE(invoiced,0)=1
        ) dr
        ORDER BY dr.pr, dr.date DESC, dr.ord DESC
        LIMIT 1
        """,
        (subscriber_id, before_date, subscriber_id, before_date),
    ).fetchone()
    return row["d"] if row else None

def whatsapp_settings():
    return {
        "api_url": get_setting("whatsapp_api_url", DEFAULT_SETTINGS["whatsapp_api_url"]),
        "from_number": get_setting("whatsapp_from_number", DEFAULT_SETTINGS["whatsapp_from_number"]),
    }

def whatsapp_payload(to_number, message_body, from_number="", extra=None):
    payload = {
        "fromNumber": from_number or "",
        "messageBody": message_body or "",
        "number": to_number or "",
        "to": to_number or "",
        "recipient": to_number or "",
        "phone": to_number or "",
        "message": message_body or "",
    }
    if extra:
        payload.update(extra)
    return payload

def build_invoice_message(invoice, settings=None):
    settings = settings or {k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}
    inv = dict(invoice)
    lines = [
        f"{settings['organization_name']}",
        f"{settings['project_name']}",
        f"فاتورة رقم: {inv['invoice_no']}",
        f"المشترك: {inv['subscriber_name']} ({inv['account_number']})",
        f"الإجمالي: {inv['total_amount']} {settings['currency_name']}",
        f"المدفوع: {inv['paid_amount']} {settings['currency_name']}",
        f"المتبقي: {inv['remaining_amount']} {settings['currency_name']}",
    ]
    if inv.get('phone'):
        lines.append(f"الهاتف: {inv['phone']}")
    return "\n".join(lines)

def build_payment_message(payment, invoice=None, settings=None):
    settings = settings or {k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}
    pay = dict(payment)
    inv = dict(invoice) if invoice else None
    inv_no = inv['invoice_no'] if inv else pay.get('invoice_no', '-')
    subscriber = inv['subscriber_name'] if inv else pay.get('subscriber_name', '-')
    account = inv['account_number'] if inv else pay.get('account_number', '-')
    lines = [
        f"{settings['organization_name']}",
        f"سند سداد فاتورة رقم: {inv_no}",
        f"المشترك: {subscriber} ({account})",
        f"المبلغ: {pay['amount']} {settings['currency_name']}",
        f"الطريقة: {pay.get('method') or '-'}",
    ]
    return "\n".join(lines)

def generate_invoice_pdf_bytes(app, invoice_id, per_page=1):
    with app.app_context():
        db = get_db()
        invoice = db.execute(
            """
            SELECT i.*, s.name subscriber_name, s.account_number, s.phone, s.village, s.address, s.meter_number,
                   s.active AS subscriber_active,
                   uc.username AS created_by_name, up.username AS printed_by_name, us.username AS sent_by_name
            FROM invoices i
            JOIN subscribers s ON s.id = i.subscriber_id
            LEFT JOIN users uc ON uc.id = i.created_by
            LEFT JOIN users up ON up.id = i.printed_by
            LEFT JOIN users us ON us.id = i.sent_by
            WHERE i.id = ?
            """,
            (invoice_id,),
        ).fetchone()
        if not invoice:
            raise ValueError("invoice not found")
        previous_invoice = fetch_previous_invoice_summary(db, invoice["subscriber_id"], invoice["id"])
        qr_data = qr_image_uri(make_qr_data(invoice))
        html = render_template(
            "invoice_print.html",
            invoice=invoice,
            previous_invoice=previous_invoice,
            qr_data=qr_data,
            per_page=per_page,
            settings={k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS},
        )
        if not PLAYWRIGHT_AVAILABLE:
            raise ValueError("Playwright is not installed. PDF generation is unavailable.")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content(html)
            pdf = page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
            browser.close()
        return pdf, invoice

def post_whatsapp(api_url, from_number, to_number, message_body, file_tuple=None):
    files = None
    if file_tuple:
        files = {"file": file_tuple}
    resp = requests.post(
        api_url,
        data=whatsapp_payload(to_number, message_body, from_number=from_number),
        files=files,
        timeout=60,
    )
    return resp

def form_subscriber_data(req):
    return {
        "account_number": req.form.get("account_number", "").strip(),
        "name":           req.form.get("name", "").strip(),
        "meter_number":   req.form.get("meter_number", "").strip(),
        "phone":          req.form.get("phone", "").strip(),
        "village":        req.form.get("village", "").strip(),
        "address":        req.form.get("address", "").strip(),
        "default_unit_price":       req.form.get("default_unit_price", "").strip(),
        "default_subscription_fee": req.form.get("default_subscription_fee", "").strip(),
        "last_reading":   req.form.get("last_reading", "").strip(),
        "last_due_amount": req.form.get("last_due_amount", "").strip(),
        "last_paid_amount": req.form.get("last_paid_amount", "").strip(),
        "active": str(req.form.get("active", "1")).strip() not in ("0", "false", "False", ""),
        "notes":  req.form.get("notes", "").strip(),
    }

def import_file(file_storage, ext):
    raw_bytes = file_storage.read()
    db = get_db()
    count = 0

    if ext in {"xlsx", "xls"}:
        from io import BytesIO
        wb = load_workbook(BytesIO(raw_bytes), data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        header_row = next(rows_iter, None)
        if not header_row:
            raise ValueError("الملف فارغ أو لا يحتوي على رأس.")
        headers = [normalize_header(c) for c in header_row]
        for row in rows_iter:
            if not any(v is not None and str(v).strip() for v in row):
                continue
            data = {headers[i]: (row[i] if i < len(row) else None) for i in range(len(headers))}
            try:
                upsert_subscriber_from_dict(db, data)
                count += 1
            except Exception:
                continue
        db.commit()
        return count

    if ext == "csv":
        text = io.StringIO(raw_bytes.decode("utf-8-sig", errors="ignore"))
        reader = csv.DictReader(text)
        for data in reader:
            norm = {normalize_header(k): v for k, v in data.items()}
            try:
                upsert_subscriber_from_dict(db, norm)
                count += 1
            except Exception:
                continue
        db.commit()
        return count

    if ext == "xml":
        import xml.etree.ElementTree as ET2
        root = ET2.fromstring(raw_bytes)
        for node in root.findall(".//subscriber"):
            data = {normalize_header(child.tag): (child.text or "") for child in node}
            try:
                upsert_subscriber_from_dict(db, data)
                count += 1
            except Exception:
                continue
        db.commit()
        return count

    raise ValueError(f"نوع الملف '{ext}' غير مدعوم. المدعوم: xlsx, csv, xml")

def upsert_subscriber_from_dict(db, data):
    account_number = str(data.get("account_number") or data.get("account") or "").strip()
    name = str(data.get("name") or data.get("subscriber_name") or "").strip()
    if not name:
        return
    if not account_number:
        account_number = next_subscriber_account_number(db)
    meter_number = str(data.get("meter_number") or data.get("meter") or "").strip()
    phone = str(data.get("phone") or data.get("mobile") or "").strip()
    village = str(data.get("village") or data.get("area") or "").strip()
    address = str(data.get("address") or "").strip()
    default_unit_price = parse_decimal(data.get("default_unit_price") or data.get("unit_price")) or parse_decimal(get_setting("default_unit_price", "0"))
    default_subscription_fee = parse_decimal(data.get("default_subscription_fee") or data.get("subscription_fee")) or parse_decimal(get_setting("default_subscription_fee", "0"))
    last_reading = parse_decimal(data.get("last_reading") or data.get("opening_reading"))
    last_due_amount = parse_decimal(data.get("last_due_amount") or data.get("last_due"))
    last_paid_amount = parse_decimal(data.get("last_paid_amount") or data.get("last_paid"))
    notes = str(data.get("notes") or "").strip()
    active = 0 if str(data.get("active") or data.get("status") or "1").strip() in {"0", "false", "False"} else 1

    existing = db.execute("SELECT id FROM subscribers WHERE account_number = ?", (account_number,)).fetchone()
    if existing:
        db.execute(
            """
            UPDATE subscribers
            SET name=?, meter_number=?, phone=?, village=?, address=?, default_unit_price=?, default_subscription_fee=?, last_reading=?, last_due_amount=?, last_paid_amount=?, active=?, notes=?, updated_at=?
            WHERE account_number=?
            """,
            (name, meter_number, phone, village, address, default_unit_price, default_subscription_fee, last_reading, last_due_amount, last_paid_amount, active, notes, now_iso(), account_number),
        )
    else:
        db.execute(
            """
            INSERT INTO subscribers (account_number, name, meter_number, phone, village, address, default_unit_price, default_subscription_fee, last_reading, last_due_amount, last_paid_amount, active, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (account_number, name, meter_number, phone, village, address, default_unit_price, default_subscription_fee, last_reading, last_due_amount, last_paid_amount, active, notes, now_iso(), now_iso()),
        )

def normalize_header(value):
    s = str(value or "").strip().lower()
    repl = {
        "رقم الحساب": "account_number",
        "الاسم": "name",
        "المشترك": "name",
        "اسم المشترك": "name",
        "العداد": "meter_number",
        "رقم العداد": "meter_number",
        "الهاتف": "phone",
        "الجوال": "phone",
        "تلفون": "phone",
        "القرية": "village",
        "المنطقة": "village",
        "العنوان": "address",
        "آخر قراءة": "last_reading",
        "المستحق السابق": "last_due_amount",
        "المسدد السابق": "last_paid_amount",
        "سعر الوحدة": "default_unit_price",
        "رسوم الاشتراك": "default_subscription_fee",
        "ملاحظات": "notes",
        "حالة": "active",
        "الحالة": "active",
    }
    return repl.get(s, s.replace(" ", "_"))

def build_reports_payload(db, subscriber_id=None, start=None, end=None, q=""):
    start = start or date(date.today().year, date.today().month, 1).isoformat()
    end = end or date.today().isoformat()
    if subscriber_id:
        subscriber = db.execute("SELECT id, name, account_number, phone FROM subscribers WHERE id = ?", (subscriber_id,)).fetchone()
        invoices = db.execute(
            """
            SELECT i.*, u.username AS created_by_name, pu.username AS printed_by_name, su.username AS sent_by_name
            FROM invoices i
            LEFT JOIN users u ON u.id = i.created_by
            LEFT JOIN users pu ON pu.id = i.printed_by
            LEFT JOIN users su ON su.id = i.sent_by
            WHERE i.subscriber_id=? AND i.invoice_date BETWEEN ? AND ?
            ORDER BY i.id DESC
            """,
            (subscriber_id, start, end),
        ).fetchall()
        payments = db.execute(
            """
            SELECT p.*, i.invoice_no, u.username AS created_by_name
            FROM payments p
            LEFT JOIN invoices i ON i.id = p.invoice_id
            LEFT JOIN users u ON u.id = p.created_by
            WHERE p.subscriber_id=? AND p.payment_date BETWEEN ? AND ?
            ORDER BY p.id DESC
            """,
            (subscriber_id, start, end),
        ).fetchall()
        summary = {
            "invoices_total": sum((r["total_amount"] or 0) for r in invoices),
            "payments_total": sum((r["amount"] or 0) for r in payments),
            "remaining_total": sum((r["remaining_amount"] or 0) for r in invoices),
            "consumption_total": sum((r["consumption"] or 0) for r in invoices),
        }
        return {
            "mode": "subscriber",
            "subscriber": subscriber,
            "invoices": invoices,
            "payments": payments,
            "summary": summary,
            "subscriber_summary": [],
            "all_invoices": [],
            "all_payments": [],
        }

    all_invoices = db.execute(
        """
        SELECT i.*, s.name subscriber_name, s.account_number,
               u.username AS created_by_name, pu.username AS printed_by_name, su.username AS sent_by_name
        FROM invoices i
        JOIN subscribers s ON s.id = i.subscriber_id
        LEFT JOIN users u ON u.id = i.created_by
        LEFT JOIN users pu ON pu.id = i.printed_by
        LEFT JOIN users su ON su.id = i.sent_by
        WHERE i.invoice_date BETWEEN ? AND ?
        ORDER BY i.id DESC
        """,
        (start, end),
    ).fetchall()
    all_payments = db.execute(
        """
        SELECT p.*, i.invoice_no, s.name subscriber_name, s.account_number,
               u.username AS created_by_name
        FROM payments p
        LEFT JOIN invoices i ON i.id = p.invoice_id
        LEFT JOIN subscribers s ON s.id = p.subscriber_id
        LEFT JOIN users u ON u.id = p.created_by
        WHERE p.payment_date BETWEEN ? AND ?
        ORDER BY p.id DESC
        """,
        (start, end),
    ).fetchall()
    if q:
        qq = q.lower()
        all_invoices = [r for r in all_invoices if qq in (r["invoice_no"] or "").lower() or qq in (r["subscriber_name"] or "").lower() or qq in (r["account_number"] or "").lower()]
        all_payments = [r for r in all_payments if qq in (r["invoice_no"] or "").lower() or qq in (r["subscriber_name"] or "").lower() or qq in (r["account_number"] or "").lower()]
    subscriber_summary = db.execute(
        """
        SELECT s.id, s.name, s.account_number,
               COUNT(i.id) AS invoices_count,
               COALESCE(SUM(i.total_amount), 0) AS total_amount,
               COALESCE(SUM(i.paid_amount), 0) AS paid_amount,
               COALESCE(SUM(i.remaining_amount), 0) AS remaining_amount,
               COALESCE(SUM(i.consumption), 0) AS consumption_total
        FROM subscribers s
        LEFT JOIN invoices i ON i.subscriber_id = s.id AND i.invoice_date BETWEEN ? AND ?
        GROUP BY s.id
        ORDER BY s.name
        """,
        (start, end),
    ).fetchall()
    if q:
        qq = q.lower()
        subscriber_summary = [r for r in subscriber_summary if qq in (r["name"] or "").lower() or qq in (r["account_number"] or "").lower()]
    summary = {
        "invoices_total": sum((r["total_amount"] or 0) for r in all_invoices),
        "payments_total": sum((r["amount"] or 0) for r in all_payments),
        "remaining_total": sum((r["remaining_amount"] or 0) for r in all_invoices),
        "consumption_total": sum((r["consumption"] or 0) for r in all_invoices),
    }
    return {
        "mode": "all",
        "subscriber": None,
        "invoices": [],
        "payments": [],
        "summary": summary,
        "subscriber_summary": subscriber_summary,
        "all_invoices": all_invoices,
        "all_payments": all_payments,
    }


def create_app():
    app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))
    app.config.update(
        SECRET_KEY=_load_or_create_secret_key(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("FORCE_HTTPS", "0") == "1",
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        TEMPLATES_AUTO_RELOAD=True,
    )

    app.jinja_env.filters.update({
        "tafqit": tafqit_filter,
        "num": num_filter,
        "deadline": deadline_filter,
    })
    app.jinja_env.globals.update({
        "make_qr_data": make_qr_data,
        "qr_image_uri": qr_image_uri,
    })

    def current_csrf_token():
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return token

    @app.context_processor
    def inject_security_context():
        return {"csrf_token": current_csrf_token}

    @app.before_request
    def csrf_protect():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            sent = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token") or ""
            if not sent or not secrets.compare_digest(sent, session.get("_csrf_token", "")):
                abort(400, description="رمز CSRF غير صالح أو مفقود. أعد تحميل الصفحة وحاول مجدداً.")

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    UPLOAD_DIR.mkdir(exist_ok=True)

    # استدعاء تهيئة ومزامنة بنية الخزن والجداول من database.py
    init_db(app)
    init_accounting()
    with app.app_context():
        db = get_db()
        ensure_all_party_accounts(db)
        db.commit()

    try:
        from payments import bp as payments_bp
        app.register_blueprint(payments_bp, url_prefix="/payments")
    except Exception as _payments_import_error:
        print("payments blueprint unavailable:", _payments_import_error)

    try:
        app.register_blueprint(manual_collection_bp)
    except Exception as _manual_collection_import_error:
        print("manual collection blueprint unavailable:", _manual_collection_import_error)

    @app.before_request
    def load_current_user():
        g.user = None
        uid = session.get("user_id")
        if uid:
            db = get_db()
            g.user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

    @app.context_processor
    def inject_globals():
        current_user = getattr(g, "user", None)
        user_role = current_user["role"] if current_user else None
        return {
            "current_user": current_user,
            "setting": get_setting,
            "today": date.today().isoformat(),
            "app_name": get_setting("project_name", DEFAULT_SETTINGS["project_name"]),
            "h_route": True if 'reports' in app.view_functions else False,
            "i_route": True if 'import_data' in app.view_functions else False,
            "user_role": user_role,
            "is_admin": user_role and user_role.lower() == "admin",
            "is_staff": user_role and user_role.lower() == "staff",
            "is_collector": user_role and user_role.lower() == "collector",
            "is_technician": user_role and user_role.lower() == "technician",
        }

    @app.teardown_appcontext
    def close_db(exception=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    # =======================================================
    # --- واجهات ومسارات الفندقة والتحكم بالنظام الفعلي ---
    # =======================================================

    @app.route("/")
    def index():
        current_user = getattr(g, "user", None)
        if not current_user:
            return redirect(url_for("login"))
        if (current_user["role"] or "").lower() in ["collector", "staff", "technician", "accountant", "manager"]:
            return redirect(url_for("employee_portal"))
        db  = get_db()
        today_str = date.today().isoformat()
        cur_month = today_str[:7]
        total_subscribers   = db.execute("SELECT COUNT(*) c FROM subscribers").fetchone()["c"]
        active_subscribers  = db.execute("SELECT COUNT(*) c FROM subscribers WHERE active=1").fetchone()["c"]
        total_invoices      = db.execute("SELECT COUNT(*) c FROM invoices").fetchone()["c"]
        invoices_this_month = db.execute("SELECT COUNT(*) c FROM invoices WHERE substr(invoice_date,1,7)=?", (cur_month,)).fetchone()["c"]
        collected_this_month= db.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE substr(payment_date,1,7)=?", (cur_month,)).fetchone()["s"]
        grand_total         = db.execute("SELECT COALESCE(SUM(total_amount),0) s FROM invoices").fetchone()["s"]
        total_collected     = db.execute("SELECT COALESCE(SUM(paid_amount),0) s FROM invoices").fetchone()["s"]
        grand_remaining     = db.execute("SELECT COALESCE(SUM(remaining_amount),0) s FROM invoices").fetchone()["s"]
        overdue_count       = db.execute("SELECT COUNT(*) c FROM invoices WHERE remaining_amount > 0").fetchone()["c"]
        collection_rate     = round(total_collected / grand_total * 100) if grand_total else 0
        monthly = db.execute("""
            SELECT substr(invoice_date,1,7) m,
                   COALESCE(SUM(total_amount),0)  total,
                   COALESCE(SUM(paid_amount),0)   paid,
                   COALESCE(SUM(consumption),0)   consumption
            FROM invoices GROUP BY m ORDER BY m DESC LIMIT 12
        """).fetchall()
        monthly = list(reversed(monthly))
        month_labels      = [r["m"] for r in monthly]
        month_totals      = [float(r["total"]) for r in monthly]
        month_paid        = [float(r["paid"])  for r in monthly]
        month_consumption = [float(r["consumption"]) for r in monthly]
        top_debtors_raw = db.execute("""
            SELECT s.name, s.account_number,
                   COALESCE(SUM(i.remaining_amount),0) remaining,
                   COUNT(i.id) months
            FROM subscribers s JOIN invoices i ON i.subscriber_id=s.id
            WHERE i.remaining_amount > 0
            GROUP BY s.id ORDER BY remaining DESC LIMIT 7
        """).fetchall()
        top_debtors = [{"name":r["name"],"account_number":r["account_number"],
                        "remaining":float(r["remaining"]),"months":r["months"]} for r in top_debtors_raw]
        recent_invoices = db.execute("""
            SELECT i.*, s.name subscriber_name, s.account_number, uc.username created_by_name
            FROM invoices i JOIN subscribers s ON s.id=i.subscriber_id
            LEFT JOIN users uc ON uc.id=i.created_by
            ORDER BY i.id DESC LIMIT 10
        """).fetchall()
        currency = get_setting("currency_name", "ريال")
        return render_template(
            "dashboard.html",
            stats={
                "total_subscribers":   total_subscribers,
                "active_subscribers":  active_subscribers,
                "total_invoices":      total_invoices,
                "invoices_this_month": invoices_this_month,
                "collected_this_month":float(collected_this_month),
                "grand_total":         float(grand_total),
                "total_collected":     float(total_collected),
                "grand_remaining":     float(grand_remaining),
                "overdue_count":       overdue_count,
                "collection_rate":     collection_rate,
                "total_due":           float(grand_remaining),
            },
            month_labels=month_labels,
            month_totals=month_totals,
            month_paid=month_paid,
            month_consumption=month_consumption,
            top_debtors=top_debtors,
            recent_invoices=recent_invoices,
            currency=currency,
        )

    @app.route("/login", methods=["GET", "POST"])
    def login():
        current_user = getattr(g, "user", None)
        if current_user:
            current_role = (current_user["role"] or "").lower()
            return redirect(url_for("employee_portal" if current_role in ["collector", "staff", "technician", "accountant", "manager"] else "index"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            _prune_login_failures()
            lock_key = f"{request.remote_addr}|{username.lower()}"
            fails, last_fail = _LOGIN_FAILURES.get(lock_key, (0, 0))
            if fails >= MAX_LOGIN_FAILS and time.time() - last_fail < LOGIN_WINDOW_SECONDS:
                abort(429, description="محاولات دخول كثيرة. انتظر 15 دقيقة وحاول مجدداً.")
            db = get_db()
            user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if user and check_password_hash(user["password_hash"], password):
                _LOGIN_FAILURES.pop(lock_key, None)
                session.clear()
                session["user_id"] = user["id"]
                flash("تم تسجيل الدخول بنجاح.", "success")
                current_role = (user["role"] or "").lower()
                return redirect(url_for("employee_portal" if current_role in ["collector", "staff", "technician", "accountant", "manager"] else "index"))
            _LOGIN_FAILURES[lock_key] = (fails + 1, time.time())
            flash("اسم المستخدم أو كلمة المرور غير صحيحة.", "danger")
        return render_template(
            "login.html",
            engineer_name=get_setting("engineer_name", DEFAULT_SETTINGS["engineer_name"]),
            ui_theme=get_setting("ui_theme", DEFAULT_SETTINGS["ui_theme"]),
        )

    @app.route("/logout")
    @login_required
    def logout():
        session.clear()
        flash("تم تسجيل الخروج.", "info")
        return redirect(url_for("login"))

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    @role_required(["admin"])
    def settings():
        if request.method == "POST":
            for key in DEFAULT_SETTINGS:
                set_setting(key, request.form.get(key, "").strip())
            flash("تم حفظ الإعدادات.", "success")
            return redirect(url_for("settings"))
        settings_dict = {k: get_setting(k, v) for k, v in DEFAULT_SETTINGS.items()}
        return render_template("settings.html", settings=settings_dict)

    @app.route("/subscribers")
    @login_required
    def subscribers():
            q = request.args.get("q", "").strip()
            status = request.args.get("status", "").strip()

            db = get_db()

            sql = "SELECT * FROM subscribers WHERE 1=1"
            params = []

            if q:
                sql += """
                    AND (
                        name LIKE ?
                        OR account_number LIKE ?
                        OR phone LIKE ?
                        OR meter_number LIKE ?
                    )
                """
                like = f"%{q}%"
                params.extend([like, like, like, like])

            if status == "active":
                sql += " AND active = 1"
            elif status == "inactive":
                sql += " AND active = 0"

            sql += " ORDER BY id DESC"

            rows = db.execute(sql, params).fetchall()

            return render_template(
                "subscribers_list.html",
                subscribers=rows,
                q=q,
            selected_account_ids=_selected_account_ids_from_request(),
                status=status
            )
    @app.route("/subscribers/new", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "staff", "collector"])
    def subscriber_new():
        db = get_db()
        if request.method == "POST":
            data = form_subscriber_data(request)
            if not data["name"]:
                flash("اسم المشترك مطلوب.", "danger")
                return render_template("subscriber_form.html", subscriber=data, mode="new")
            if not data["account_number"]:
                data["account_number"] = next_subscriber_account_number(db)
            try:
                db.execute(
                    """
                    INSERT INTO subscribers (account_number, name, meter_number, phone, village, address, default_unit_price,
                                             default_subscription_fee, last_reading, last_due_amount, last_paid_amount, active, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data["account_number"], data["name"], data["meter_number"], data["phone"], data["village"], data["address"],
                        data["default_unit_price"] or get_setting("default_unit_price", DEFAULT_SETTINGS["default_unit_price"]),
                        data["default_subscription_fee"] or get_setting("default_subscription_fee", DEFAULT_SETTINGS["default_subscription_fee"]),
                        parse_decimal(data["last_reading"]),
                        parse_decimal(data["last_due_amount"]),
                        parse_decimal(data["last_paid_amount"]),
                        1 if data["active"] else 0, data["notes"],
                        now_iso(), now_iso(),
                    ),
                )
                subscriber_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                ensure_subscriber_account(db, subscriber_id, data["account_number"], data["name"])
                db.commit()
                flash("تمت إضافة المشترك وربطه محاسبياً.", "success")
                return redirect(url_for("subscribers"))
            except IntegrityError:
                flash("رقم الحساب موجود مسبقاً.", "danger")
        return render_template("subscriber_form.html", subscriber={}, mode="new")

    @app.route("/subscribers/<int:subscriber_id>/edit", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "staff", "technician"])
    def subscriber_edit(subscriber_id):
        db = get_db()
        subscriber = db.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)).fetchone()
        if not subscriber:
            abort(404)
        if request.method == "POST":
            data = form_subscriber_data(request)
            db.execute(
                """
                UPDATE subscribers
                SET account_number=?, name=?, meter_number=?, phone=?, village=?, address=?, default_unit_price=?,
                    default_subscription_fee=?, last_reading=?, last_due_amount=?, last_paid_amount=?, active=?, notes=?, updated_at=?
                WHERE id=?
                """,
                (
                    data["account_number"], data["name"], data["meter_number"], data["phone"], data["village"], data["address"],
                    data["default_unit_price"] or subscriber["default_unit_price"],
                    data["default_subscription_fee"] or subscriber["default_subscription_fee"],
                    parse_decimal(data["last_reading"]) if data["last_reading"] else subscriber["last_reading"],
                    parse_decimal(data["last_due_amount"]) if data["last_due_amount"] else subscriber["last_due_amount"],
                    parse_decimal(data["last_paid_amount"]) if data["last_paid_amount"] else subscriber["last_paid_amount"],
                    1 if data["active"] else 0, data["notes"], now_iso(), subscriber_id,
                ),
            )
            ensure_subscriber_account(db, subscriber_id, data["account_number"], data["name"])
            db.commit()
            flash("تم تحديث بيانات المشترك وربط حسابه المحاسبي.", "success")
            return redirect(url_for("subscribers"))
        return render_template("subscriber_form.html", subscriber=subscriber, mode="edit")

    @app.route("/subscribers/<int:subscriber_id>/delete", methods=["POST"])
    @login_required
    @role_required(["admin"])
    def subscriber_delete(subscriber_id):
        db = get_db()
        archive_subscriber_account(db, subscriber_id)
        db.commit()
        flash("تمت أرشفة المشترك وحسابه المحاسبي بدل الحذف الفيزيائي.", "warning")
        return redirect(url_for("subscribers"))

    @app.route("/api/subscribers/search")
    @login_required
    @role_required(["admin", "staff", "collector"])
    def api_subscribers_search():
        q = request.args.get("q", "").strip()
        db = get_db()
        if q:
            like = f"%{q}%"
            rows = db.execute(
                """
                SELECT s.*,
                       COALESCE(
                           (SELECT i.current_reading FROM invoices i WHERE i.subscriber_id = s.id ORDER BY i.id DESC LIMIT 1),
                           s.last_reading,
                           0
                       ) AS last_reading,
                       COALESCE(
                           (SELECT COALESCE(SUM(remaining_amount),0) FROM invoices i WHERE i.subscriber_id = s.id AND remaining_amount > 0),
                           0
                       ) AS overdue_balance,
                       COALESCE(s.last_due_amount, 0) AS last_due_amount,
                       COALESCE(s.last_paid_amount, 0) AS last_paid_amount
                FROM subscribers s
                WHERE s.name LIKE ? OR s.account_number LIKE ? OR s.phone LIKE ? OR s.meter_number LIKE ?
                ORDER BY s.id DESC LIMIT 20
                """,
                (like, like, like, like),
            ).fetchall()
        else:
            rows = []
        return jsonify([dict(r) for r in rows])

    @app.route("/api/invoices/unpaid")
    @login_required
    @role_required(["admin", "staff", "collector"])
    def api_invoices_unpaid():
        """جلب الفواتير غير المسددة لمشترك معين — للبحث الذكي في مركز التحصيل"""
        subscriber_id = request.args.get("subscriber_id", type=int)
        q             = request.args.get("q", "").strip()
        db = get_db()
        params = []
        if subscriber_id:
            sql = """
                SELECT i.*, s.name subscriber_name, s.account_number, s.phone, s.address
                FROM invoices i JOIN subscribers s ON s.id = i.subscriber_id
                WHERE i.subscriber_id = ? AND i.remaining_amount > 0
                ORDER BY i.invoice_date ASC
            """
            params = [subscriber_id]
        elif q:
            like = f"%{q}%"
            sql = """
                SELECT i.*, s.name subscriber_name, s.account_number, s.phone, s.address
                FROM invoices i JOIN subscribers s ON s.id = i.subscriber_id
                WHERE i.remaining_amount > 0
                  AND (i.invoice_no LIKE ? OR s.name LIKE ? OR s.account_number LIKE ?)
                ORDER BY i.invoice_date ASC LIMIT 30
            """
            params = [like, like, like]
        else:
            return jsonify([])
        rows = db.execute(sql, params).fetchall()
        return jsonify([dict(r) for r in rows])



    @app.route("/invoices")
    @login_required
    @role_required(["admin", "staff", "collector"])
    def invoices():
        q = request.args.get("q", "").strip()
        month = request.args.get("month", "").strip()
        db = get_db()
        user_role = (g.user["role"] or "").lower()
        sql = """
            SELECT i.*, s.name subscriber_name, s.account_number, s.phone,
                   uc.username AS created_by_name,
                   up.username AS printed_by_name,
                   us.username AS sent_by_name
            FROM invoices i
            JOIN subscribers s ON s.id = i.subscriber_id
            LEFT JOIN users uc ON uc.id = i.created_by
            LEFT JOIN users up ON up.id = i.printed_by
            LEFT JOIN users us ON us.id = i.sent_by
        """
        params = []
        filters = []
        if user_role == "collector":
            filters.append("i.created_by = ?")
            params.append(g.user["id"])
        if q:
            filters.append("(i.invoice_no LIKE ? OR s.name LIKE ? OR s.account_number LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        if month:
            filters.append("substr(coalesce(i.month_label, i.invoice_date), 1, 7) = ?")
            params.append(month)
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " ORDER BY i.id DESC"
        rows = db.execute(sql, params).fetchall()
        return render_template("invoices_list.html", invoices=rows, q=q, month=month, user_role=user_role)

    @app.route("/invoices/new", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "staff", "collector"])
    def invoice_new():
        db = get_db()
        subscribers_list = db.execute("SELECT id, name, account_number FROM subscribers WHERE active=1 ORDER BY name").fetchall()
        selected_subscriber = None
        sid = request.args.get("subscriber_id", type=int)
        if sid:
            selected_subscriber = db.execute("SELECT * FROM subscribers WHERE id = ?", (sid,)).fetchone()
        if request.method == "POST":
            subscriber_id = int(request.form.get("subscriber_id") or 0)
            subscriber = db.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)).fetchone()
            if not subscriber:
                flash("اختر مشتركاً صحيحاً.", "danger")
                return render_template("invoice_form.html", subscribers=subscribers_list, mode="new", settings={k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS})
            current_reading = safe_current_reading(request.form.get("current_reading"), None)
            subscriber_account_id = ensure_subscriber_account(db, subscriber_id, subscriber["account_number"], subscriber["name"])
            unit_price = parse_decimal(request.form.get("unit_price")) or parse_decimal(subscriber["default_unit_price"]) or parse_decimal(get_setting("default_unit_price", "0"))
            subscription_fee = parse_decimal(request.form.get("subscription_fee")) or parse_decimal(subscriber["default_subscription_fee"]) or parse_decimal(get_setting("default_subscription_fee", "0"))
            other_charges = parse_decimal(request.form.get("other_charges"))
            notes = request.form.get("notes", "").strip()
            invoice_date = request.form.get("invoice_date") or date.today().isoformat()
            month_label = request.form.get("month_label", "").strip()
            if is_date_in_closed_year(db, invoice_date):
                flash("السنة المالية مقفلة لهذا التاريخ ولا يمكن إنشاء فاتورة.", "danger")
                return render_template("invoice_form.html", subscribers=subscribers_list, mode="new", settings={k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS})
            if not month_label:
                month_label = invoice_date[:7]
            try:
                ensure_unique_invoice_month(db, subscriber_id, invoice_date, month_label)
            except Exception as exc:
                flash(str(exc), "danger")
                return render_template("invoice_form.html", subscribers=subscribers_list, mode="new", settings={k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS})
            last_invoice = db.execute(
                "SELECT current_reading, current_reading_date FROM invoices WHERE subscriber_id=? ORDER BY id DESC LIMIT 1",
                (subscriber_id,),
            ).fetchone()
            opening_snapshot = get_subscriber_opening_snapshot(db, subscriber_id)
            previous_reading = parse_decimal(request.form.get("previous_reading"))
            previous_arrears = parse_decimal(request.form.get("previous_arrears"))
            if previous_reading is None:
                if last_invoice:
                    previous_reading = parse_decimal(last_invoice["current_reading"]) if last_invoice else 0
                else:
                    previous_reading = opening_snapshot["previous_reading"] or 0
            if previous_arrears is None:
                previous_arrears = opening_snapshot["previous_arrears"] if not last_invoice else 0.0
            if current_reading is None:
                flash("القراءة الحالية مطلوبة.", "danger")
                return render_template("invoice_form.html", subscribers=subscribers_list, mode="new", settings={k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS})
            previous_reading_date = (request.form.get("previous_reading_date") or "").strip() or None
            current_reading_date = (request.form.get("current_reading_date") or "").strip() or invoice_date
            if previous_reading_date is None:
                if last_invoice and last_invoice["current_reading_date"]:
                    previous_reading_date = last_invoice["current_reading_date"]
                else:
                    previous_reading_date = previous_reading_date_for(db, subscriber_id, current_reading_date)
            consumption = max(0, current_reading - (previous_reading or 0))
            opening_balance = get_opening_balance(db, subscriber_id) + max(0.0, previous_arrears)
            consumption_amount = consumption * unit_price
            total_amount = opening_balance + consumption_amount + subscription_fee + other_charges
            paid_amount = 0.0
            remaining_amount = max(0, total_amount - paid_amount)
            invoice_no = next_invoice_no(db)
            try:
                db.execute(
                    """
                    INSERT INTO invoices (
                        invoice_no, subscriber_id, invoice_date, month_label, previous_reading, current_reading,
                        previous_reading_date, current_reading_date,
                        consumption, unit_price, consumption_amount, subscription_fee, other_charges, opening_balance,
                        total_amount, paid_amount, remaining_amount, credit_amount, notes, created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        invoice_no, subscriber_id, invoice_date, month_label, previous_reading, current_reading,
                        previous_reading_date, current_reading_date,
                        consumption, unit_price, consumption_amount, subscription_fee, other_charges, opening_balance,
                        total_amount, paid_amount, remaining_amount, max(0.0, paid_amount - total_amount), notes, session["user_id"], now_iso(), now_iso(),
                    ),
                )
                invoice_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                # ترحيل محاسبي إلزامي: ذمم العميل مدين / إيراد المياه دائن.
                post_invoice_accounting(db, invoice_id, session["user_id"], reverse_existing=False)
                db.commit()
                flash("تم إنشاء الفاتورة بنجاح.", "success")
                return redirect(url_for("invoice_detail", invoice_id=invoice_id))
            except Exception as e:
                db.rollback()
                flash(f"حدث خطأ أثناء إنشاء الفاتورة: {e}", "danger")
        return render_template(
            "invoice_form.html",
            subscribers=subscribers_list,
            mode="new",
            selected_subscriber=selected_subscriber,
            defaults={
                "unit_price": get_setting("default_unit_price", DEFAULT_SETTINGS["default_unit_price"]),
                "subscription_fee": get_setting("default_subscription_fee", DEFAULT_SETTINGS["default_subscription_fee"]),
                "invoice_date": date.today().isoformat(),
                "month_label": date.today().isoformat()[:7],
            },
            settings={k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS},
        )

    @app.route("/invoices/<int:invoice_id>")
    @login_required
    @role_required(["admin", "staff", "collector"])
    def invoice_detail(invoice_id):
        db = get_db()
        invoice = db.execute(
            """
            SELECT i.*, s.name subscriber_name, s.account_number, s.phone, s.village, s.address, s.meter_number,
                   s.default_unit_price, s.default_subscription_fee,
                   uc.username AS created_by_name,
                   up.username AS printed_by_name,
                   us.username AS sent_by_name
            FROM invoices i
            JOIN subscribers s ON s.id = i.subscriber_id
            LEFT JOIN users uc ON uc.id = i.created_by
            LEFT JOIN users up ON up.id = i.printed_by
            LEFT JOIN users us ON us.id = i.sent_by
            WHERE i.id = ?
            """,
            (invoice_id,),
        ).fetchone()
        if not invoice:
            abort(404)
        previous_invoice = fetch_previous_invoice_summary(db, invoice["subscriber_id"], invoice["id"])
        if (g.user["role"] or "").lower() == "collector" and invoice["created_by"] != g.user["id"]:
            abort(403)
        payments = db.execute(
            """
            SELECT p.*, u.username AS created_by_name
            FROM payments p
            LEFT JOIN users u ON u.id = p.created_by
            WHERE p.invoice_id = ?
            ORDER BY p.id DESC
            """,
            (invoice_id,),
        ).fetchall()
        attachments = db.execute(
            "SELECT * FROM attachments WHERE invoice_id = ? ORDER BY id DESC",
            (invoice_id,),
        ).fetchall()
        qr_data = qr_image_uri(make_qr_data(invoice))
        return render_template(
            "invoice_detail.html",
            invoice=invoice,
            previous_invoice=previous_invoice,
            payments=payments,
            attachments=attachments,
            qr_data=qr_data,
            settings={k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS},
            current_user_name=user_name(db, session.get("user_id")),
        )

    @app.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "staff"])
    def invoice_edit(invoice_id):
        db = get_db()
        invoice = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if not invoice:
            abort(404)
        subscribers_list = db.execute("SELECT id, name, account_number FROM subscribers WHERE active=1 ORDER BY name").fetchall()
        if request.method == "POST":
            subscriber_id = int(request.form.get("subscriber_id") or invoice["subscriber_id"])
            subscriber = db.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)).fetchone()
            if not subscriber:
                flash("اختر مشتركاً صحيحاً.", "danger")
                return redirect(url_for("invoice_detail", invoice_id=invoice_id))
            subscriber_account_id = ensure_subscriber_account(db, subscriber_id, subscriber["account_number"], subscriber["name"])
            previous_reading = parse_decimal(request.form.get("previous_reading"))
            previous_arrears = parse_decimal(request.form.get("previous_arrears"))
            opening_snapshot = get_subscriber_opening_snapshot(db, subscriber_id)
            if previous_reading is None:
                previous_reading = parse_decimal(invoice["previous_reading"]) if invoice and invoice["previous_reading"] is not None else (opening_snapshot["previous_reading"] or 0)
            if previous_arrears is None:
                previous_arrears = opening_snapshot["previous_arrears"]
            current_reading = safe_current_reading(request.form.get("current_reading"), previous_reading)
            unit_price = parse_decimal(request.form.get("unit_price")) or parse_decimal(subscriber["default_unit_price"])
            subscription_fee = parse_decimal(request.form.get("subscription_fee")) or parse_decimal(subscriber["default_subscription_fee"])
            other_charges = parse_decimal(request.form.get("other_charges"))
            paid_amount = 0.0
            invoice_date = request.form.get("invoice_date") or invoice["invoice_date"]
            month_label = request.form.get("month_label") or invoice["month_label"]
            if is_date_in_closed_year(db, invoice_date) or is_date_in_closed_year(db, invoice["invoice_date"]):
                flash("الفاتورة في سنة مالية مقفلة ولا يمكن تعديلها.", "danger")
                return redirect(url_for("invoice_detail", invoice_id=invoice_id))
            try:
                ensure_unique_invoice_month(db, subscriber_id, invoice_date, month_label, exclude_invoice_id=invoice_id)
            except Exception as exc:
                flash(str(exc), "danger")
                return render_template("invoice_form.html", subscribers=subscribers_list, mode="edit", invoice=invoice, selected_subscriber=subscriber, defaults={}, settings={k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS})
            notes = request.form.get("notes", "")
            previous_reading_date = (request.form.get("previous_reading_date") or "").strip() or None
            current_reading_date = (request.form.get("current_reading_date") or "").strip() or invoice_date
            opening_balance = get_opening_balance(db, subscriber_id, exclude_invoice_id=invoice_id) + max(0.0, previous_arrears)
            consumption = max(0, current_reading - (previous_reading or 0))
            consumption_amount = consumption * unit_price
            total_amount = opening_balance + consumption_amount + subscription_fee + other_charges
            remaining_amount = max(0, total_amount - paid_amount)
            db.execute(
                """
                UPDATE invoices
                SET subscriber_id=?, invoice_date=?, month_label=?, previous_reading=?, current_reading=?,
                    previous_reading_date=?, current_reading_date=?,
                    consumption=?, unit_price=?, consumption_amount=?, subscription_fee=?, other_charges=?,
                    opening_balance=?, total_amount=?, paid_amount=?, remaining_amount=?, credit_amount=?, notes=?, updated_at=?
                WHERE id=?
                """,
                (
                    subscriber_id, invoice_date, month_label, previous_reading, current_reading,
                    previous_reading_date, current_reading_date,
                    consumption, unit_price, consumption_amount, subscription_fee, other_charges,
                    opening_balance, total_amount, paid_amount, remaining_amount, max(0.0, paid_amount - total_amount), notes, now_iso(), invoice_id,
                ),
            )
            # السداد مصدر الحقيقة؛ ثم إعادة ترحيل قيد الفاتورة بالقيمة الجديدة.
            recalculate_invoice_payment_totals(db, invoice_id)
            post_invoice_accounting(db, invoice_id, session["user_id"], reverse_existing=True)
            db.commit()
            flash("تم تحديث الفاتورة.", "success")
            return redirect(url_for("invoice_detail", invoice_id=invoice_id))
        selected_subscriber = db.execute("SELECT * FROM subscribers WHERE id = ?", (invoice["subscriber_id"],)).fetchone()
        return render_template(
            "invoice_form.html",
            subscribers=subscribers_list,
            mode="edit",
            invoice=invoice,
            selected_subscriber=selected_subscriber,
            defaults={},
        )

    @app.route("/invoices/<int:invoice_id>/delete", methods=["POST"])
    @login_required
    @role_required(["admin"])
    def invoice_delete(invoice_id):
        db = get_db()
        try:
            inv = db.execute("SELECT invoice_date FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            if inv and is_date_in_closed_year(db, inv["invoice_date"]):
                flash("الفاتورة في سنة مالية مقفلة ولا يمكن حذفها.", "danger")
                return redirect(url_for("invoices"))
            void_invoice_with_reversal(db, invoice_id, session.get("user_id"))
            db.commit()
            flash("تم حذف الفاتورة بعد عكس قيودها المحاسبية.", "warning")
        except Exception as exc:
            db.rollback()
            flash(f"تعذر حذف الفاتورة: {exc}", "danger")
        return redirect(url_for("invoices"))

    @app.route("/invoices/<int:invoice_id>/payment", methods=["POST"])
    @login_required
    @role_required(["admin", "collector"])
    def add_payment(invoice_id):
        amount = parse_decimal(request.form.get("amount"))
        if amount <= 0:
            flash("أدخل مبلغاً صحيحاً للسداد أولاً.", "danger")
            return redirect(url_for("invoice_detail", invoice_id=invoice_id))
        method = request.form.get("method", "نقداً")
        notes = request.form.get("notes", "")
        attachment_path = save_upload(request.files.get("attachment"))
        db = get_db()
        try:
            process_invoice_payment(
                db, invoice_id=invoice_id, user_id=session["user_id"], 
                amount=amount, method=method, notes=notes, attachment_path=attachment_path
            )
            db.commit()
            flash("تم تسجيل السداد وربطه بحساب الموظف المسجّل بنجاح.", "success")
        except Exception as e:
            db.rollback()
            flash(f"حدث خطأ مالي أثناء معالجة السداد: {e}", "danger")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.route("/invoices/<int:invoice_id>/attachments", methods=["POST"])
    @login_required
    @role_required(["admin", "staff"])
    def add_invoice_attachment(invoice_id):
        db = get_db()
        invoice = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if not invoice:
            abort(404)
        file = request.files.get("file")
        if not file or not file.filename:
            flash("اختر ملفاً.", "danger")
            return redirect(url_for("invoice_detail", invoice_id=invoice_id))
        try:
            filename = save_upload(file)
            db.execute(
                "INSERT INTO attachments (invoice_id, filename, path, mime_type, uploaded_at) VALUES (?, ?, ?, ?, ?)",
                (invoice_id, file.filename, filename, file.mimetype, now_iso()),
            )
            db.commit()
            flash("تمت إضافة المرفق.", "success")
        except Exception as e:
            flash(f"تعذر حفظ المرفق: {e}", "danger")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.route("/payments")
    @login_required
    @role_required(["admin", "staff", "collector"])
    def payments():
        q = request.args.get("q", "").strip()
        db = get_db()
        sql = """
            SELECT p.*, i.invoice_no, s.name subscriber_name, s.account_number,
                   u.username AS created_by_name
            FROM payments p
            LEFT JOIN invoices i ON i.id = p.invoice_id
            LEFT JOIN subscribers s ON s.id = p.subscriber_id
            LEFT JOIN users u ON u.id = p.created_by
        """
        params = []
        if q:
            sql += " WHERE s.name LIKE ? OR s.account_number LIKE ? OR i.invoice_no LIKE ?"
            like = f"%{q}%"
            params = [like, like, like]
        sql += " ORDER BY p.id DESC"
        rows = db.execute(sql, params).fetchall()
        return render_template("payments_list.html", payments=rows, q=q)

    @app.route("/reports")
    @login_required
    @role_required(["admin"])
    def reports():
        db = get_db()
        subscriber_id = request.args.get("subscriber_id", type=int)
        start = request.args.get("start", date(date.today().year, date.today().month, 1).isoformat())
        end = request.args.get("end", date.today().isoformat())
        q = request.args.get("q", "").strip()
        subscribers_list = db.execute("SELECT id, name, account_number FROM subscribers ORDER BY name").fetchall()
        payload = build_reports_payload(db, subscriber_id=subscriber_id, start=start, end=end, q=q)
        return render_template("reports.html", subscribers=subscribers_list, request=request, **payload)

    @app.route("/import", methods=["GET", "POST"])
    @login_required
    @role_required(["admin"])
    def import_data():
        if request.method == "POST":
            file = request.files.get("file")
            if not file or not file.filename:
                flash("اختر ملفاً للاستيراد.", "danger")
                return redirect(url_for("import_data"))
            ext = file.filename.rsplit(".", 1)[-1].lower()
            try:
                count = import_file(file, ext)
                flash(f"✅ تم استيراد {count} سجل بنجاح.", "success")
            except Exception as e:
                flash(f"❌ فشل الاستيراد: {e}", "danger")
            return redirect(url_for("import_data"))
        return render_template("import.html")

    @app.route("/import/template/<fmt>")
    @login_required
    @role_required(["admin"])
    def download_import_template(fmt):
        """تحميل قالب استيراد المشتركين بصيغة xlsx أو csv"""
        headers_ar = ["رقم الحساب", "الاسم", "رقم العداد", "الجوال", "العنوان",
                      "القرائة السابقة","القراءة الحالية","سعر الوحدة", "رسوم الاشتراك", "ملاحظات", "الحالة"]
        sample = [
            ["WS-0001", "أحمد محمد علي", "M-100001", "9677XXXXXXXX", "صنعاء - حدة", "3500", "500", "", "1","000000","000000"],
            ["WS-0002", "خالد إبراهيم",  "M-100002", "9671XXXXXXXX", "عدن - كريتر",  "3500", "500", "", "1","00000","000000"],
        ]
        if fmt == "xlsx":
            from io import BytesIO
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws = wb.active
            ws.title = "مشتركون"
            ws.append(headers_ar)
            for row in sample:
                ws.append(row)
            buf = BytesIO()
            wb.save(buf)
            buf.seek(0)
            return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             as_attachment=True, download_name="subscribers_template.xlsx")
        elif fmt == "csv":
            import csv as csv_mod
            from io import StringIO
            buf = StringIO()
            w = csv_mod.writer(buf)
            w.writerow(headers_ar)
            for row in sample:
                w.writerow(row)
            from flask import Response
            return Response(
                "\ufeff" + buf.getvalue(),   # BOM for Arabic Excel
                mimetype="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=subscribers_template.csv"},
            )
        abort(404)

    @app.route("/backup/db")
    @login_required
    @role_required(["admin"])
    def backup_db():
        """تحميل نسخة احتياطية من قاعدة البيانات"""
        from io import BytesIO
        db_path = app.config.get("DATABASE") or (app.instance_path + "/water_billing.sqlite3")
        buf = BytesIO()
        with open(db_path, "rb") as f:
            buf.write(f.read())
        buf.seek(0)
        ts = date.today().isoformat()
        return send_file(buf, mimetype="application/octet-stream",
                         as_attachment=True, download_name=f"water_billing_backup_{ts}.sqlite3")

    @app.route("/backup/db/restore", methods=["POST"])
    @login_required
    @role_required(["admin"])
    def restore_db():
        uploaded = request.files.get("backup_file")
        if not uploaded or not uploaded.filename:
            flash("اختر ملف النسخة الاحتياطية أولاً.", "danger")
            return redirect(url_for("settings"))
        tmp_path = BASE_DIR / "instance" / f"restore_{uuid.uuid4().hex}.sqlite3"
        tmp_path.parent.mkdir(exist_ok=True)
        uploaded.save(tmp_path)
        try:
            conn = g.pop("db", None)
            if conn is not None:
                conn.close()
            replace_database_from_file(tmp_path)
            try:
                SessionLocal.remove()
            except Exception:
                pass
            try:
                engine.dispose()
            except Exception:
                pass
            flash("تم استرجاع النسخة الاحتياطية بنجاح. أعد تشغيل التطبيق لتفعيلها بالكامل.", "success")
        except Exception as exc:
            flash(f"تعذر استرجاع النسخة الاحتياطية: {exc}", "danger")
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        return redirect(url_for("settings"))

    @app.route("/export/subscribers")
    @login_required
    @role_required(["admin", "staff"])
    def export_subscribers():
        """تصدير المشتركين إلى Excel"""
        from io import BytesIO
        db = get_db()
        rows = db.execute("SELECT * FROM subscribers ORDER BY account_number").fetchall()
        wb = Workbook()
        ws = wb.active
        ws.title = "مشتركون"
        headers = ["رقم الحساب", "الاسم", "رقم العداد", "الجوال", "العنوان",
                   "سعر الوحدة", "رسوم الاشتراك", "ملاحظات", "الحالة", "تاريخ الإنشاء"]
        ws.append(headers)
        for r in rows:
            ws.append([
                r["account_number"], r["name"], r["meter_number"] or "",
                r["phone"] or "", r["address"] or "",
                r["default_unit_price"] or "", r["default_subscription_fee"] or "",
                r["notes"] or "", "نشط" if r["active"] else "متوقف",
                (r["created_at"] or "")[:10],
            ])
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        ts = date.today().isoformat()
        return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"subscribers_{ts}.xlsx")



    @app.route("/uploads/<path:filename>")
    @login_required
    def uploaded_file(filename):
        return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)

    
    @app.route("/invoices/bulk-readings", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "staff"])
    def invoices_bulk_readings():
        """إدخال القراءات دفعة واحدة وحفظها كقراءات جماعية مؤقتة."""
        db = get_db()
        zone = request.args.get("zone", "").strip() or request.form.get("zone", "").strip()

        zones = [
            r["address"].split("-")[0].strip()
            for r in db.execute(
                "SELECT DISTINCT address FROM subscribers WHERE active=1 AND address IS NOT NULL AND address != '' ORDER BY address"
            ).fetchall()
            if r["address"]
        ]
        zones = sorted(set(z for z in zones if z))

        if request.method == "POST":
            saved = 0
            skipped = 0
            invoice_date = request.form.get("invoice_date") or date.today().isoformat()
            month_label = request.form.get("month_label", "").strip() or invoice_date[:7]

            subscriber_ids = request.form.getlist("subscriber_id")
            for sid in subscriber_ids:
                curr_str = request.form.get(f"current_reading_{sid}", "").strip()
                if not curr_str:
                    skipped += 1
                    continue

                try:
                    curr_val = float(curr_str)
                except ValueError:
                    skipped += 1
                    continue

                subscriber = db.execute("SELECT * FROM subscribers WHERE id=?", (sid,)).fetchone()
                if not subscriber:
                    skipped += 1
                    continue

                last_inv = db.execute(
                    "SELECT current_reading FROM invoices WHERE subscriber_id=? ORDER BY id DESC LIMIT 1",
                    (sid,),
                ).fetchone()
                if last_inv and last_inv["current_reading"] is not None:
                    prev_reading = float(last_inv["current_reading"] or 0)
                else:
                    prev_reading = float(subscriber["last_reading"] or 0)

                if curr_val < prev_reading:
                    skipped += 1
                    continue

                existing = db.execute(
                    "SELECT id FROM bulk_readings WHERE subscriber_id=? AND invoiced=0 ORDER BY id DESC LIMIT 1",
                    (sid,),
                ).fetchone()

                if existing:
                    db.execute(
                        """
                        UPDATE bulk_readings
                        SET previous_reading=?, current_reading=?, reading_date=?, month_label=?, created_by=?, updated_at=?
                        WHERE id=?
                        """,
                        (prev_reading, curr_val, invoice_date, month_label, session.get("user_id"), now_iso(), existing["id"]),
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO bulk_readings
                            (subscriber_id, previous_reading, current_reading, reading_date, month_label, created_by, created_at)
                        VALUES (?,?,?,?,?,?,?)
                        """,
                        (sid, prev_reading, curr_val, invoice_date, month_label, session.get("user_id"), now_iso()),
                    )
                saved += 1

            db.commit()
            flash(f"تم حفظ {saved} قراءة. تم تخطي {skipped}.", "success" if saved else "warning")
            return redirect(url_for("invoices_bulk_readings", zone=zone))

        if zone:
            subscribers_rows = db.execute(
                "SELECT * FROM subscribers WHERE active=1 AND address LIKE ? ORDER BY account_number",
                (f"%{zone}%",),
            ).fetchall()
        else:
            subscribers_rows = db.execute(
                "SELECT * FROM subscribers WHERE active=1 ORDER BY account_number"
            ).fetchall()

        last_readings = {}
        for sub in subscribers_rows:
            lr = db.execute(
                "SELECT current_reading FROM invoices WHERE subscriber_id=? ORDER BY id DESC LIMIT 1",
                (sub["id"],),
            ).fetchone()
            if lr and lr["current_reading"] is not None:
                last_readings[sub["id"]] = float(lr["current_reading"] or 0)
            else:
                last_readings[sub["id"]] = float(sub["last_reading"] or 0)

        return render_template(
            "invoices_bulk_readings.html",
            subscribers=subscribers_rows,
            last_readings=last_readings,
            zones=zones,
            zone=zone,
            today=date.today().isoformat(),
            month_label=date.today().isoformat()[:7],
        )


    def _create_invoice_from_reading(db, rid, user_id):
        """إنشاء فاتورة واحدة من قراءة جماعية. يعيد (created|skipped|error, رسالة)."""
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            return "error", "رقم قراءة غير صالح."
        reading = db.execute("SELECT * FROM bulk_readings WHERE id=?", (rid,)).fetchone()
        if not reading:
            return "error", "القراءة غير موجودة."
        subscriber = db.execute(
            "SELECT * FROM subscribers WHERE id=?",
            (reading["subscriber_id"],),
        ).fetchone()
        if not subscriber:
            return "error", "المشترك غير موجود."
        try:
            if is_date_in_closed_year(db, reading["reading_date"]):
                return "error", "السنة المالية مقفلة."
            ensure_unique_invoice_month(
                db, subscriber["id"], reading["reading_date"], reading["month_label"]
            )
        except ValueError as _dup_exc:
            return "skipped", str(_dup_exc)
        try:
            unit_price = float(
                subscriber["default_unit_price"]
                or get_setting("default_unit_price", "0")
                or 0
            )
            subscription_fee = float(
                subscriber["default_subscription_fee"]
                or get_setting("default_subscription_fee", "0")
                or 0
            )
            start_ctx = get_subscriber_opening_snapshot(db, subscriber["id"])
            last_inv = db.execute(
                "SELECT current_reading, current_reading_date FROM invoices WHERE subscriber_id=? ORDER BY id DESC LIMIT 1",
                (subscriber["id"],),
            ).fetchone()
            if last_inv and last_inv["current_reading"] is not None:
                previous_reading = float(last_inv["current_reading"] or 0)
                previous_arrears = 0.0
            else:
                previous_reading = float(start_ctx.get("previous_reading") or subscriber["last_reading"] or 0)
                previous_arrears = float(start_ctx.get("previous_arrears") or 0)
            previous_reading_date = None
            if last_inv and last_inv["current_reading_date"]:
                previous_reading_date = last_inv["current_reading_date"]
            else:
                previous_reading_date = previous_reading_date_for(db, subscriber["id"], reading["reading_date"])
            current_reading_date = reading["reading_date"]
            opening_balance = float(get_opening_balance(db, subscriber["id"]) or 0) + previous_arrears
            consumption = max(0.0, float(reading["current_reading"]) - float(previous_reading or 0))
            consumption_amount = consumption * unit_price
            total_amount = opening_balance + consumption_amount + subscription_fee
            invoice_no = next_invoice_no(db)
            db.execute(
                """
                INSERT INTO invoices (
                    invoice_no, subscriber_id, invoice_date, month_label,
                    previous_reading, current_reading, previous_reading_date, current_reading_date,
                    consumption, unit_price,
                    consumption_amount, subscription_fee, other_charges, opening_balance,
                    total_amount, paid_amount, remaining_amount, notes,
                    created_by, created_at, updated_at
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    invoice_no,
                    subscriber["id"],
                    reading["reading_date"],
                    reading["month_label"],
                    previous_reading,
                    reading["current_reading"],
                    previous_reading_date,
                    current_reading_date,
                    consumption,
                    unit_price,
                    consumption_amount,
                    subscription_fee,
                    0,
                    opening_balance,
                    total_amount,
                    0,
                    total_amount,
                    "",
                    user_id,
                    now_iso(),
                    now_iso(),
                ),
            )
            invoice_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            # ترحيل محاسبي إلزامي: سند إثبات + قيد مرتبط به.
            post_invoice_accounting(db, invoice_id, user_id, reverse_existing=False)
            db.execute(
                "UPDATE bulk_readings SET invoiced=1, updated_at=? WHERE id=?",
                (now_iso(), rid),
            )
            return "created", None
        except Exception as exc:
            return "error", str(exc)

    @app.route("/invoices/bulk-create", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "staff"])
    def invoices_bulk_create():
        """إنشاء فواتير جماعية من القراءات المحفوظة."""
        db = get_db()

        tbl = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bulk_readings'"
        ).fetchone()
        if not tbl:
            flash("لا توجد قراءات محفوظة. أدخل القراءات أولاً.", "warning")
            return redirect(url_for("invoices_bulk_readings"))

        pending = db.execute(
            """
            SELECT br.*, s.name subscriber_name, s.account_number,
                   s.default_unit_price, s.default_subscription_fee
            FROM bulk_readings br
            JOIN subscribers s ON s.id = br.subscriber_id
            WHERE br.invoiced=0 OR br.invoiced IS NULL
            ORDER BY s.account_number
            """
        ).fetchall()

        if request.method == "POST":
            selected_ids = request.form.getlist("reading_id")
            created = 0
            errors = 0

            for rid in selected_ids:
                outcome, msg = _create_invoice_from_reading(db, rid, session.get("user_id"))
                if outcome == "created":
                    created += 1
                elif outcome == "skipped":
                    flash(f"تم تخطي القراءة {rid}: {msg}", "warning")
                else:
                    errors += 1
                    flash(f"تعذر ترحيل الفاتورة الجماعية للقراءة {rid}: {msg}", "danger")

            db.commit()
            flash(f"✅ تم إنشاء {created} فاتورة. أخطاء: {errors}.", "success" if created else "warning")
            return redirect(url_for("invoices"))

        return render_template("invoices_bulk_create.html", pending=pending)

    def _bulk_create_worker(flask_app, job_id, reading_ids, user_id):
        """عامل التوليد الجماعي في خيط خلفي مع تحديث التقدم."""
        import jobs as _jobs
        try:
            with flask_app.test_request_context("/"):
                db = get_db()
                total = len(reading_ids)
                created = 0
                errors = 0
                for idx, rid in enumerate(reading_ids, 1):
                    try:
                        outcome, msg = _create_invoice_from_reading(db, rid, user_id)
                        if outcome == "created":
                            created += 1
                            try:
                                db.commit()
                            except Exception:
                                db.rollback()
                        elif outcome == "skipped":
                            try:
                                db.commit()
                            except Exception:
                                db.rollback()
                        else:
                            errors += 1
                            _jobs.update_job(job_id, error_list=_append_job_error(job_id, rid, msg))
                            try:
                                db.rollback()
                            except Exception:
                                pass
                    except Exception as exc:
                        errors += 1
                        _jobs.update_job(job_id, error_list=_append_job_error(job_id, rid, str(exc)))
                        try:
                            db.rollback()
                        except Exception:
                            pass
                    _jobs.progress_job(job_id, idx, stage=f"فاتورة {idx} من {total}", errors_count=errors)
                    try:
                        _jobs.update_job(job_id, created=created)
                    except Exception:
                        pass
                try:
                    db.commit()
                except Exception:
                    pass
                _jobs.update_job(job_id, created=created)
                _jobs.finish_job(job_id, message=f"تم إنشاء {created} فاتورة. أخطاء: {errors}.")
        except Exception as exc:
            import jobs as _jobs2
            _jobs2.fail_job(job_id, f"تعذر إكمال المهمة: {exc}")
            try:
                flask_app.logger.exception("bulk-create job %s failed", job_id)
            except Exception:
                pass

    def _append_job_error(job_id, rid, msg):
        import jobs as _jobs
        job = _jobs.get_job(job_id) or {}
        lst = list(job.get("error_list") or [])
        lst.append(f"قراءة {rid}: {msg}")
        return lst[-50:]

    @app.route("/invoices/bulk-create/start", methods=["POST"])
    @login_required
    @role_required(["admin", "staff"])
    def invoices_bulk_create_start():
        import jobs as _jobs
        payload = request.get_json(silent=True) or {}
        reading_ids = payload.get("reading_ids")
        if reading_ids is None:
            reading_ids = request.form.getlist("reading_id")
        if isinstance(reading_ids, str):
            reading_ids = [reading_ids]
        try:
            reading_ids = [int(x) for x in (reading_ids or [])]
        except (TypeError, ValueError):
            return jsonify({"error": "أرقام القراءات غير صالحة."}), 400
        if not reading_ids:
            return jsonify({"error": "لم تختر أي قراءة."}), 400
        job_id = _jobs.create_job("bulk-create", total=len(reading_ids), label="توليد الفواتير الجماعية")
        thread = threading.Thread(target=_bulk_create_worker, args=(app, job_id, reading_ids, session.get("user_id")), daemon=True)
        thread.start()
        return jsonify({"job_id": job_id})

    @app.route("/jobs/<job_id>")
    @login_required
    def job_status(job_id):
        import jobs as _jobs
        job = _jobs.public_job(job_id)
        if not job:
            return jsonify({"error": "not_found"}), 404
        return jsonify(job)

    @app.route("/jobs/<job_id>/download")
    @login_required
    def job_download(job_id):
        import jobs as _jobs
        data, filename = _jobs.take_result(job_id)
        if not data:
            abort(404)
        return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename or "export.pdf")


    @app.route("/invoices/<int:invoice_id>/print")
    @login_required
    def invoice_print(invoice_id):
        per_page = request.args.get("per_page", type=int, default=1)
        db = get_db()
        invoice = db.execute(
            """
            SELECT i.*, s.name subscriber_name, s.account_number, s.phone, s.village, s.address, s.meter_number,
                   s.active AS subscriber_active,
                   uc.username AS created_by_name,
                   up.username AS printed_by_name,
                   us.username AS sent_by_name
            FROM invoices i JOIN subscribers s ON s.id = i.subscriber_id
            LEFT JOIN users uc ON uc.id = i.created_by
            LEFT JOIN users up ON up.id = i.printed_by
            LEFT JOIN users us ON us.id = i.sent_by
            WHERE i.id = ?
            """,
            (invoice_id,),
        ).fetchone()
        if not invoice:
            abort(404)
        previous_invoice = fetch_previous_invoice_summary(db, invoice["subscriber_id"], invoice["id"])
        db.execute(
            "UPDATE invoices SET printed_by=?, printed_at=?, updated_at=? WHERE id=?",
            (session.get("user_id"), now_iso(), now_iso(), invoice_id),
        )
        db.commit()
        invoice = db.execute(
            """
            SELECT i.*, s.name subscriber_name, s.account_number, s.phone, s.village, s.address, s.meter_number,
                   s.active AS subscriber_active,
                   uc.username AS created_by_name,
                   up.username AS printed_by_name,
                   us.username AS sent_by_name
            FROM invoices i JOIN subscribers s ON s.id = i.subscriber_id
            LEFT JOIN users uc ON uc.id = i.created_by
            LEFT JOIN users up ON up.id = i.printed_by
            LEFT JOIN users us ON us.id = i.sent_by
            WHERE i.id = ?
            """,
            (invoice_id,),
        ).fetchone()
        qr_data = qr_image_uri(make_qr_data(invoice))
        html = render_template(
            "invoice_print.html",
            invoice=invoice,
            previous_invoice=previous_invoice,
            qr_data=qr_data,
            per_page=per_page,
            settings={k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS},
        )
        if request.args.get("pdf") == "1":
            if not PLAYWRIGHT_AVAILABLE:
                return "ميزة تصدير الـ PDF غير متاحة بسبب عدم تثبيت Playwright", 500
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.set_content(html)
                pdf = page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
                browser.close()
            return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name=f"invoice-{invoice['invoice_no']}.pdf")
        return html

    def _get_export_rows(db, ids, q):
        params = []
        sql = """
            SELECT i.*, s.name subscriber_name, s.account_number, s.phone, s.village, s.address, s.meter_number,
                   s.active AS subscriber_active,
                   uc.username AS created_by_name,
                   up.username AS printed_by_name,
                   us.username AS sent_by_name
            FROM invoices i
            JOIN subscribers s ON s.id = i.subscriber_id
            LEFT JOIN users uc ON uc.id = i.created_by
            LEFT JOIN users up ON up.id = i.printed_by
            LEFT JOIN users us ON us.id = i.sent_by
        """
        if ids:
            ids_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
            if not ids_list:
                return None
            placeholders = ",".join(["?"] * len(ids_list))
            sql += f" WHERE i.id IN ({placeholders})"
            params = ids_list
        elif q:
            like = f"%{q}%"
            sql += " WHERE i.invoice_no LIKE ? OR s.name LIKE ? OR s.account_number LIKE ?"
            params = [like, like, like]
        sql += " ORDER BY i.id DESC"
        return db.execute(sql, params).fetchall()

    def _get_by_date_rows(db, date_from, date_to, q):
        sql = """
            SELECT i.*, s.name subscriber_name, s.account_number, s.phone,
                   s.village, s.address, s.meter_number,
                   s.active AS subscriber_active,
                   uc.username AS created_by_name
            FROM invoices i
            JOIN subscribers s ON s.id = i.subscriber_id
            LEFT JOIN users uc ON uc.id = i.created_by
            WHERE date(i.invoice_date) BETWEEN date(?) AND date(?)
        """
        params = [date_from, date_to]
        if q:
            sql += " AND (i.invoice_no LIKE ? OR s.name LIKE ? OR s.account_number LIKE ?)"
            like = f"%{q}%"
            params += [like, like, like]
        sql += " ORDER BY i.invoice_date ASC, i.id ASC"
        invoices = db.execute(sql, params).fetchall()
        previous_invoices = {}
        for inv in invoices:
            previous_invoices[inv["id"]] = fetch_previous_invoice_summary(db, inv["subscriber_id"], inv["id"])
        return invoices, previous_invoices

    def _render_invoices_pdf(html):
        if not PLAYWRIGHT_AVAILABLE:
            raise ValueError("Playwright غير مثبت.")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content(html)
            pdf = page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
            browser.close()
        return bytes(pdf)

    def _export_worker(flask_app, job_id, ids, q, per_page, filename):
        import jobs as _jobs
        try:
            with flask_app.test_request_context("/"):
                db = get_db()
                _jobs.progress_job(job_id, 0, stage="جمع بيانات الفواتير...")
                rows = _get_export_rows(db, ids, q)
                if not rows:
                    _jobs.fail_job(job_id, "لا توجد فواتير للتصدير.")
                    return
                _jobs.progress_job(job_id, 0, stage=f"تجهيز {len(rows)} فاتورة...")
                html = render_template(
                    "export_invoices.html",
                    invoices=rows,
                    per_page=per_page,
                    settings={k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS},
                )
                _jobs.progress_job(job_id, 0, stage="توليد ملف PDF...")
                pdf = _render_invoices_pdf(html)
                _jobs.finish_job(job_id, message=f"تم تجهيز {len(rows)} فاتورة.", result_bytes=pdf, filename=filename)
        except Exception as exc:
            import jobs as _jobs2
            _jobs2.fail_job(job_id, f"تعذر التصدير: {exc}")
            try:
                flask_app.logger.exception("export job %s failed", job_id)
            except Exception:
                pass

    def _by_date_pdf_worker(flask_app, job_id, date_from, date_to, q, filename):
        import jobs as _jobs
        try:
            with flask_app.test_request_context("/"):
                db = get_db()
                _jobs.progress_job(job_id, 0, stage="جمع بيانات الفواتير...")
                invoices, previous_invoices = _get_by_date_rows(db, date_from, date_to, q)
                if not invoices:
                    _jobs.fail_job(job_id, "لا توجد فواتير في الفترة.")
                    return
                _jobs.progress_job(job_id, 0, stage=f"تجهيز {len(invoices)} فاتورة...")
                html = render_template(
                    "invoices_batch_print.html",
                    invoices=invoices,
                    settings={k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS},
                    previous_invoices=previous_invoices,
                    date_from=date_from,
                    date_to=date_to,
                    q=q,
                    back_url=url_for("invoices"),
                )
                _jobs.progress_job(job_id, 0, stage="توليد ملف PDF...")
                pdf = _render_invoices_pdf(html)
                _jobs.finish_job(job_id, message=f"تم تجهيز {len(invoices)} فاتورة.", result_bytes=pdf, filename=filename)
        except Exception as exc:
            import jobs as _jobs2
            _jobs2.fail_job(job_id, f"تعذر التصدير: {exc}")
            try:
                flask_app.logger.exception("by-date pdf job %s failed", job_id)
            except Exception:
                pass

    @app.route("/export/invoices")
    @login_required
    def export_invoices():
        ids = request.args.get("ids", "").strip()
        q = request.args.get("q", "").strip()
        valid_per_page = (1, 2, 3, 4, 6, 8, 9, 12)
        try:
            default_pp = int(float(get_setting("invoice_rows_per_pdf", "2")))
        except (TypeError, ValueError):
            default_pp = 2
        per_page = request.args.get("per_page", type=int, default=default_pp)
        if per_page not in valid_per_page:
            per_page = default_pp
        if per_page not in valid_per_page:
            per_page = 2
        db = get_db()
        rows = _get_export_rows(db, ids, q)
        if not rows:
            flash("لا توجد فواتير للتصدير.", "warning")
            return redirect(url_for("invoices"))
        html = render_template(
            "export_invoices.html",
            invoices=rows,
            per_page=per_page,
            settings={k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS},
        )
        try:
            pdf = _render_invoices_pdf(html)
        except ValueError:
            return "ميزة تصدير الـ PDF غير متاحة بسبب عدم تثبيت Playwright", 500
        except Exception as exc:
            try:
                app.logger.exception("sync export failed")
            except Exception:
                pass
            return f"تعذر توليد ملف PDF (عدد الفواتير: {len(rows)}). جرّب التصدير من النافذة الخلفية أو قلل العدد. التفاصيل: {exc}", 500
        return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name="invoices.pdf")

    @app.route("/export/invoices/start", methods=["POST"])
    @login_required
    def export_invoices_start():
        import jobs as _jobs
        payload = request.get_json(silent=True) or request.form
        ids = (payload.get("ids") or "").strip()
        q = (payload.get("q") or "").strip()
        try:
            per_page = int(payload.get("per_page") or 2)
        except (TypeError, ValueError):
            per_page = 2
        job_id = _jobs.create_job("export-pdf", label="تصدير الفواتير PDF")
        thread = threading.Thread(target=_export_worker, args=(app, job_id, ids, q, per_page, "invoices.pdf"), daemon=True)
        thread.start()
        return jsonify({"job_id": job_id})

    # --- مسارات واتساب الفواتير ---
    @app.route("/invoices/<int:invoice_id>/send-whatsapp", methods=["POST"])
    @login_required
    def invoice_send_whatsapp(invoice_id):
        db = get_db()
        invoice = db.execute(
            """
            SELECT i.*, s.name subscriber_name, s.account_number, s.phone, s.village, s.address, s.meter_number,
                   s.active AS subscriber_active,
                   uc.username AS created_by_name,
                   up.username AS printed_by_name,
                   us.username AS sent_by_name
            FROM invoices i
            JOIN subscribers s ON s.id = i.subscriber_id
            LEFT JOIN users uc ON uc.id = i.created_by
            LEFT JOIN users up ON up.id = i.printed_by
            LEFT JOIN users us ON us.id = i.sent_by
            WHERE i.id = ?
            """,
            (invoice_id,),
        ).fetchone()
        if not invoice:
            abort(404)
        to_number = request.form.get("to_number") or invoice["phone"]
        from_number = request.form.get("from_number") or get_setting("whatsapp_from_number", "")
        api_url = request.form.get("api_url") or get_setting("whatsapp_api_url", DEFAULT_SETTINGS["whatsapp_api_url"])
        message_body = request.form.get("message_body") or build_invoice_message(invoice)
        use_pdf = request.form.get("use_pdf") == "1"
        file_tuple = None
        if use_pdf:
            try:
                pdf, invoice_row = generate_invoice_pdf_bytes(app, invoice_id)
                file_tuple = (f"invoice-{invoice_row['invoice_no']}.pdf", io.BytesIO(pdf), "application/pdf")
            except Exception:
                pass
        if not to_number:
            flash("رقم المستلم غير موجود لهذا المشترك.", "danger")
            return redirect(url_for("invoice_detail", invoice_id=invoice_id))
        try:
            resp = post_whatsapp(api_url, from_number, to_number, message_body, file_tuple=file_tuple)
            if file_tuple and hasattr(file_tuple[1], "close"):
                file_tuple[1].close()
            if resp.ok:
                db.execute(
                    "UPDATE invoices SET sent_by=?, sent_at=?, sent_count=COALESCE(sent_count,0)+1, last_sent_channel=?, updated_at=? WHERE id=?",
                    (session.get("user_id"), now_iso(), "whatsapp", now_iso(), invoice_id),
                )
                db.commit()
                flash("تم إرسال الفاتورة عبر واتساب.", "success")
            else:
                flash(f"فشل إرسال واتساب: {resp.status_code} {resp.text[:250]}", "danger")
        except Exception as e:
            flash(f"تعذر إرسال واتساب: {e}", "danger")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.route("/payments/<int:payment_id>/send-whatsapp", methods=["POST"])
    @login_required
    def payment_send_whatsapp(payment_id):
        db = get_db()
        payment = db.execute(
            """
            SELECT p.*, i.invoice_no, i.total_amount, i.remaining_amount,
                   s.name subscriber_name, s.account_number, s.phone,
                   u.username AS created_by_name
            FROM payments p
            LEFT JOIN invoices i ON i.id = p.invoice_id
            JOIN subscribers s ON s.id = p.subscriber_id
            LEFT JOIN users u ON u.id = p.created_by
            WHERE p.id = ?
            """,
            (payment_id,),
        ).fetchone()
        if not payment:
            abort(404)
        invoice = None
        if payment["invoice_id"]:
            invoice = db.execute(
                """
                SELECT i.*, s.name subscriber_name, s.account_number, s.phone
                FROM invoices i JOIN subscribers s ON s.id = i.subscriber_id
                WHERE i.id = ?
                """,
                (payment["invoice_id"],),
            ).fetchone()
        to_number = request.form.get("to_number") or payment["phone"]
        from_number = request.form.get("from_number") or get_setting("whatsapp_from_number", "")
        api_url = request.form.get("api_url") or get_setting("whatsapp_api_url", DEFAULT_SETTINGS["whatsapp_api_url"])
        message_body = request.form.get("message_body") or build_payment_message(payment, invoice)
        file_tuple = None
        attachment_path = payment["attachment_path"]
        if attachment_path:
            full_path = UPLOAD_DIR / attachment_path
            if full_path.exists():
                file_tuple = (payment["attachment_path"], open(full_path, "rb"), payment["method"] or "application/octet-stream")
        if not to_number:
            flash("رقم المستلم غير موجود لهذا السداد.", "danger")
            return redirect(url_for("invoice_detail", invoice_id=payment["invoice_id"]) if payment["invoice_id"] else url_for("payments"))
        try:
            resp = post_whatsapp(api_url, from_number, to_number, message_body, file_tuple=file_tuple)
            if file_tuple and hasattr(file_tuple[1], "close"):
                file_tuple[1].close()
            if resp.ok:
                flash("تم إرسال سند السداد عبر واتساب.", "success")
            else:
                flash(f"فشل إرسال السند: {resp.status_code} {resp.text[:250]}", "danger")
        except Exception as e:
            flash(f"تعذر إرسال السند: {e}", "danger")
        return redirect(url_for("invoice_detail", invoice_id=payment["invoice_id"]) if payment["invoice_id"] else url_for("payments"))

    @app.route("/invoices/send-all-whatsapp", methods=["POST"])
    @login_required
    def invoices_send_all_whatsapp():
        db = get_db()
        q = request.form.get("q", "").strip()
        only_remaining = request.form.get("only_remaining") == "1"
        from_number = request.form.get("from_number") or get_setting("whatsapp_from_number", "")
        api_url = request.form.get("api_url") or get_setting("whatsapp_api_url", DEFAULT_SETTINGS["whatsapp_api_url"])
        base_sql = """
            SELECT i.*, s.name subscriber_name, s.account_number, s.phone, s.village, s.address, s.meter_number
            FROM invoices i
            JOIN subscribers s ON s.id = i.subscriber_id
        """
        params = []
        where = []
        if q:
            where.append("(i.invoice_no LIKE ? OR s.name LIKE ? OR s.account_number LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        if only_remaining:
            where.append("i.remaining_amount > 0")
        sql = base_sql + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY i.id DESC"
        invoices_list = db.execute(sql, params).fetchall()
        sent = 0
        failed = 0
        for invoice in invoices_list:
            to_number = invoice["phone"]
            if not to_number:
                failed += 1
                continue
            message_body = build_invoice_message(invoice)
            try:
                pdf, invoice_row = generate_invoice_pdf_bytes(app, invoice["id"])
                file_tuple = (f"invoice-{invoice_row['invoice_no']}.pdf", io.BytesIO(pdf), "application/pdf")
                resp = post_whatsapp(api_url, from_number, to_number, message_body, file_tuple=file_tuple)
                if hasattr(file_tuple[1], "close"):
                    file_tuple[1].close()
                if resp.ok:
                    db.execute(
                        "UPDATE invoices SET sent_by=?, sent_at=?, sent_count=COALESCE(sent_count,0)+1, last_sent_channel=?, updated_at=? WHERE id=?",
                        (session.get("user_id"), now_iso(), "whatsapp", now_iso(), invoice["id"]),
                    )
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        db.commit()
        flash(f"تم إرسال {sent} فاتورة، وفشل {failed}.", "success" if sent else "warning")
        return redirect(url_for("invoices", q=q))

    @app.route("/reports/pdf")
    @login_required
    @role_required(["admin"])
    def reports_pdf():
        db = get_db()
        subscriber_id = request.args.get("subscriber_id", type=int)
        start = request.args.get("start", date(date.today().year, date.today().month, 1).isoformat())
        end = request.args.get("end", date.today().isoformat())
        q = request.args.get("q", "").strip()
        subscribers_list = db.execute("SELECT id, name, account_number FROM subscribers ORDER BY name").fetchall()
        payload = build_reports_payload(db, subscriber_id=subscriber_id, start=start, end=end, q=q)
        html = render_template("reports.html", subscribers=subscribers_list, request=request, **payload)
        if not PLAYWRIGHT_AVAILABLE:
            return "ميزة تصدير الـ PDF غير متاحة بسبب عدم تثبيت Playwright", 500
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content(html)
            pdf = page.pdf(format="A4", print_background=True)
            browser.close()
        return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name="report.pdf")

    @app.route("/api/invoice-preview")
    @login_required
    def invoice_preview():
        subscriber_id = request.args.get("subscriber_id", type=int)
        if not subscriber_id:
            return jsonify({"ok": False})
        db = get_db()
        subscriber = db.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)).fetchone()
        if not subscriber:
            return jsonify({"ok": False})
        last_invoice = db.execute(
            "SELECT id, invoice_no, invoice_date, month_label, current_reading, total_amount, paid_amount, remaining_amount, credit_amount FROM invoices WHERE subscriber_id=? ORDER BY id DESC LIMIT 1",
            (subscriber_id,),
        ).fetchone()
        opening_snapshot = get_subscriber_opening_snapshot(db, subscriber_id)
        opening_balance = get_opening_balance(db, subscriber_id)
        if last_invoice and last_invoice["current_reading"] is not None:
            previous_reading = float(last_invoice["current_reading"])
        elif opening_snapshot.get("previous_reading") is not None:
            previous_reading = float(opening_snapshot["previous_reading"])
        else:
            previous_reading = float(subscriber["last_reading"] or 0)
        previous_arrears = float(opening_snapshot.get("previous_arrears") or 0)
        return jsonify({
            "ok": True,
            "subscriber": dict(subscriber),
            "previous_reading": previous_reading,
            "previous_arrears": previous_arrears,
            "last_reading": previous_reading,
            "last_due_amount": float(subscriber["last_due_amount"] or 0) if "last_due_amount" in subscriber.keys() else 0.0,
            "last_paid_amount": float(subscriber["last_paid_amount"] or 0) if "last_paid_amount" in subscriber.keys() else 0.0,
            "opening_balance": float(opening_balance),
            "unit_price": float(subscriber["default_unit_price"] or get_setting("default_unit_price", "0")),
            "subscription_fee": float(subscriber["default_subscription_fee"] or get_setting("default_subscription_fee", "0")),
            "previous_invoice": {
                "invoice_no": last_invoice["invoice_no"] if last_invoice else None,
                "invoice_date": last_invoice["invoice_date"] if last_invoice else None,
                "month_label": last_invoice["month_label"] if last_invoice else None,
                "total_amount": float(last_invoice["total_amount"] or 0) if last_invoice else 0,
                "paid_amount": float(last_invoice["paid_amount"] or 0) if last_invoice else 0,
                "remaining_amount": float(last_invoice["remaining_amount"] or 0) if last_invoice else 0,
                "credit_amount": float(last_invoice["credit_amount"] or 0) if last_invoice else 0,
            },
        })

    @app.route("/send-whatsapp", methods=["POST"])
    @login_required
    def send_whatsapp():
        api_url = request.form.get("api_url") or get_setting("whatsapp_api_url", DEFAULT_SETTINGS["whatsapp_api_url"])
        from_number = request.form.get("from_number") or get_setting("whatsapp_from_number", "")
        to_number = request.form.get("to_number") or request.form.get("number") or request.form.get("to") or request.form.get("phone") or ""
        message_body = request.form.get("message_body") or request.form.get("message") or ""
        file = request.files.get("file")
        if not api_url or not message_body:
            flash("البيانات ناقصة لإرسال واتساب.", "danger")
            return redirect(request.referrer or url_for("index"))
        if not to_number:
            flash("رقم المستلم مطلوب.", "danger")
            return redirect(request.referrer or url_for("index"))
        file_tuple = None
        if file and file.filename:
            file_tuple = (secure_filename(file.filename), file.stream, file.mimetype or "application/octet-stream")
        try:
            resp = post_whatsapp(api_url, from_number, to_number, message_body, file_tuple=file_tuple)
            if resp.ok:
                flash("تم إرسال الرسالة بنجاح.", "success")
            else:
                flash(f"فشل الإرسال: {resp.status_code} {resp.text[:250]}", "danger")
        except Exception as e:
            flash(f"تعذر الاتصال بخدمة واتساب: {e}", "danger")
        return redirect(request.referrer or url_for("index"))

    @app.route("/api/subscriber/<int:subscriber_id>/history")
    @login_required
    def subscriber_history(subscriber_id):
        db = get_db()
        invoices_list = db.execute(
            "SELECT id, invoice_no, invoice_date, total_amount, remaining_amount, current_reading FROM invoices WHERE subscriber_id=? ORDER BY id DESC LIMIT 12",
            (subscriber_id,),
        ).fetchall()
        payments_list = db.execute(
            "SELECT id, payment_date, amount, method FROM payments WHERE subscriber_id=? ORDER BY id DESC LIMIT 12",
            (subscriber_id,),
        ).fetchall()
        return jsonify({"invoices": [dict(r) for r in invoices_list], "payments": [dict(r) for r in payments_list]})

    # --- مسارات الصناديق والخزن المالية ---
    @app.route("/wallets")
    @login_required
    def wallets_dashboard():
        db = get_db()
        wallets = db.execute("SELECT * FROM wallets ORDER BY id ASC").fetchall()
        transactions = db.execute("""
            SELECT t.*, w.name as wallet_name, u.username, s.name as subscriber_name
            FROM transactions t
            JOIN wallets w ON w.id = t.wallet_id
            JOIN users u ON u.id = t.user_id
            LEFT JOIN subscribers s ON s.id = t.subscriber_id
            ORDER BY t.id DESC LIMIT 15
        """).fetchall()
        account_options = flatten_account_options(promote_visible_account_tree(get_account_balance_rows(db)), only_postable=True)
        return render_template("wallets.html", wallets=wallets, transactions=transactions, account_options=account_options)

    # مسار مستعار للتوافق مع الروابط القديمة
    @app.route("/wallets/list")
    @login_required
    def wallets_list():
        return redirect(url_for("vouchers_page"))

    @app.route("/wallets/expense", methods=["POST"])
    @login_required
    @role_required(["admin", "accountant"])
    def add_expense():
        db = get_db()
        account_id = request.form.get("account_id", type=int)
        amount = parse_decimal(request.form.get("amount"))
        notes = request.form.get("notes", "").strip()
        user_id = session["user_id"]

        if not account_id or amount <= 0 or not notes:
            flash("يرجى اختيار الحساب المصروف عليه وإدخال مبلغ صحيح.", "danger")
            return redirect(url_for("vouchers_page"))

        account = db.execute("SELECT id, name FROM account_nodes WHERE id = ? AND active=1 AND is_postable=1", (account_id,)).fetchone()
        if not account:
            flash("الحساب المختار غير صالح.", "danger")
            return redirect(url_for("vouchers_page"))

        cash_account_id = get_default_cash_account_id(db)
        if not cash_account_id:
            flash("لم يتم العثور على حساب صندوق تفصيلي في شجرة الحسابات.", "danger")
            return redirect(url_for("vouchers_page"))

        try:
            voucher_id = create_accounting_voucher(
                db,
                voucher_type="payment",
                voucher_date=date.today().isoformat(),
                amount=amount,
                from_account_id=cash_account_id,
                to_account_id=account_id,
                reference=f"EXP-{user_id}-{int(amount)}",
                description=f"سند صرف: {notes}",
                created_by=user_id,
                source_type="manual",
                source_id=None,
            )
            log_audit(db, user_id, "EXPENSE", "accounting_vouchers", voucher_id, f"صرف مبلغ {amount} على الحساب {account['name']}")
            db.commit()
            flash("تم قيد سند الصرف وربطه بشجرة الحسابات.", "success")
        except Exception as e:
            db.rollback()
            flash(f"حدث خطأ مالي أثناء معالجة الصرف: {e}", "danger")

        return redirect(url_for("vouchers_page"))

    @app.route("/staff/edit/<int:user_id>", methods=["POST"])
    @login_required
    @role_required(["admin"])
    def edit_user(user_id):
        db = get_db()
        role = request.form.get("role", "").strip()
        wallet_id = request.form.get("wallet_id", type=int)
        active = 1 if request.form.get("active") == "1" else 0
        valid_roles = ["Admin", "Staff", "Collector", "Technician"]
        if role not in valid_roles:
            flash("دور غير صحيح.", "danger")
            return redirect(url_for("staff_monitor"))
        db.execute(
            "UPDATE users SET role=?, active=?, wallet_id=?, updated_at=? WHERE id=?",
            (role, active, wallet_id or None, now_iso(), user_id)
        )
        if wallet_id:
            db.execute("DELETE FROM user_wallets WHERE user_id = ?", (user_id,))
            db.execute("INSERT OR IGNORE INTO user_wallets (user_id, wallet_id) VALUES (?, ?)", (user_id, wallet_id))
        db.commit()
        flash(f"تم تحديث بيانات الموظف بنجاح.", "success")
        return redirect(url_for("staff_monitor"))

    # --- مسار مراقبة الموظفين والنشاط ---
    @app.route("/staff/monitor")
    @login_required
    @role_required(["admin", "technician"])
    def staff_monitor():
        db = get_db()
        users_with_balances = db.execute("""
            SELECT u.id, u.username, u.role, u.active, u.created_at,
                   w.name as wallet_name, COALESCE(w.balance, 0) as wallet_balance,
                   u.wallet_id
            FROM users u
            LEFT JOIN wallets w ON w.id = u.wallet_id
            ORDER BY u.id ASC
        """).fetchall()

        activity_logs = db.execute("""
            SELECT 'audit' as log_type, a.action as action_type, a.table_name, a.row_id, a.description, a.timestamp as created_at, u.username, u.role
            FROM audit_logs a
            JOIN users u ON u.id = a.user_id
            UNION ALL
            SELECT 'transaction' as log_type, t.type as action_type, 'wallets' as table_name, t.wallet_id as row_id, t.notes as description, t.created_at, u.username, u.role
            FROM transactions t
            JOIN users u ON u.id = t.user_id
            ORDER BY created_at DESC LIMIT 30
        """).fetchall()

        wallets = db.execute("SELECT * FROM wallets ORDER BY id ASC").fetchall()
        return render_template("staff_monitor.html", users=users_with_balances, logs=activity_logs, wallets=wallets)


    # --- مسارات المحاسبة وشجرة الحسابات ولوحة الموظف ---
    @app.route("/accounts/tree")
    @login_required
    @role_required(["admin", "technician"])
    def accounts_tree():
        db = get_db()
        tree = promote_visible_account_tree(get_account_balance_rows(db))
        flat_accounts = flatten_parent_accounts(tree)
        currency = get_setting("currency_name", "ريال")
        return render_template("accounts_tree.html", tree=tree, flat_accounts=flat_accounts, currency=currency)

    @app.route("/accounts/create", methods=["POST"])
    @login_required
    @role_required(["admin", "technician"])
    def accounts_create():
        db = get_db()
        try:
            payload = prepare_account_form_payload(db, request.form)
        except ValueError:
            flash("الأب المختار غير موجود.", "danger")
            return redirect(url_for("accounts_tree"))

        code = payload["code"]
        name = payload["name"]
        parent_id = payload["parent_id"]
        node_type = payload["node_type"]
        is_postable = payload["is_postable"]

        if not name:
            flash("اسم الحساب مطلوب.", "danger")
            return redirect(url_for("accounts_tree"))
        if not parent_id:
            flash("يرجى اختيار الأب من الحسابات المتاحة.", "danger")
            return redirect(url_for("accounts_tree"))
        if not code:
            flash("تعذر توليد رقم الحساب تلقائياً.", "danger")
            return redirect(url_for("accounts_tree"))
        if db.execute("SELECT 1 FROM account_nodes WHERE code=?", (code,)).fetchone():
            flash("رمز الحساب مستخدم مسبقاً.", "danger")
            return redirect(url_for("accounts_tree"))
        add_account_node(db, code, name, node_type=node_type, parent_id=parent_id, is_postable=is_postable)
        db.commit()
        flash("تمت إضافة الحساب بنجاح.", "success")
        return redirect(url_for("accounts_tree"))

    @app.route("/accounts/edit/<int:account_id>", methods=["GET", "POST"])
    @login_required
    @role_required(["admin"])
    def accounts_edit(account_id):
        db = get_db()
        account = db.execute("SELECT * FROM account_nodes WHERE id = ?", (account_id,)).fetchone()
        if not account:
            abort(404)
        tree = promote_visible_account_tree(get_account_balance_rows(db))
        flat_accounts = flatten_parent_accounts(tree, exclude_id=account_id)
        if request.method == "POST":
            try:
                payload = prepare_account_form_payload(db, request.form, existing_account=account)
            except ValueError:
                flash("الأب المختار غير موجود.", "danger")
                return render_template("account_form.html", account=account, flat_accounts=flat_accounts)

            code = payload["code"]
            name = payload["name"]
            parent_id = payload["parent_id"]
            node_type = payload["node_type"]
            is_postable = payload["is_postable"]

            if not code or not name:
                flash("رمز واسم الحساب مطلوبان.", "danger")
                return render_template("account_form.html", account=account, flat_accounts=flat_accounts)
            duplicate = db.execute("SELECT 1 FROM account_nodes WHERE code=? AND id<>?", (code, account_id)).fetchone()
            if duplicate:
                flash("رمز الحساب مستخدم مسبقاً.", "danger")
                return render_template("account_form.html", account=account, flat_accounts=flat_accounts)
            db.execute(
                "UPDATE account_nodes SET code=?, name=?, node_type=?, parent_id=?, is_postable=?, updated_at=? WHERE id=?",
                (code, name, node_type, parent_id, is_postable, now_iso(), account_id),
            )
            db.commit()
            flash("تم تحديث الحساب بنجاح.", "success")
            return redirect(url_for("accounts_tree"))
        return render_template("account_form.html", account=account, flat_accounts=flat_accounts)

    @app.route("/accounts/delete/<int:account_id>", methods=["POST"])
    @login_required
    @role_required(["admin"])
    def accounts_delete(account_id):
        db = get_db()
        account = db.execute("SELECT * FROM account_nodes WHERE id = ?", (account_id,)).fetchone()
        if not account:
            abort(404)
        child = db.execute("SELECT 1 FROM account_nodes WHERE parent_id = ? LIMIT 1", (account_id,)).fetchone()
        linked = db.execute("SELECT 1 FROM journal_lines WHERE account_id = ? LIMIT 1", (account_id,)).fetchone()
        if child or linked:
            db.execute("UPDATE account_nodes SET active = 0, updated_at = ? WHERE id = ?", (now_iso(), account_id))
            flash("تم تعطيل الحساب لأنه مرتبط بحركات أو حسابات فرعية.", "warning")
        else:
            db.execute("DELETE FROM account_nodes WHERE id = ?", (account_id,))
            flash("تم حذف الحساب نهائياً.", "success")
        db.commit()
        return redirect(url_for("accounts_tree"))

    @app.route("/employee/portal")
    @login_required
    def employee_portal():
        db = get_db()
        user = g.user
        user_role = (user["role"] or "").lower()

        with SessionLocal() as s:
            profile = s.query(EmployeeProfile).filter(EmployeeProfile.user_id == user["id"]).first()
        if not profile:
            ensure_employee_profile(db, user["id"], full_name=user["username"], job_title=user["role"])
            db.commit()
            with SessionLocal() as s:
                profile = s.query(EmployeeProfile).filter(EmployeeProfile.user_id == user["id"]).first()

        wallet_row = None
        if user["wallet_id"]:
            wallet_row = db.execute("SELECT id, name, balance FROM wallets WHERE id = ?", (user["wallet_id"],)).fetchone()

        stats = {
            "received_month": db.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE created_by=? AND substr(created_at,1,7)=substr(?,1,7)", (user["id"], date.today().isoformat())).fetchone()["s"],
            "on_cashbox": db.execute("SELECT COALESCE(SUM(remaining_amount),0) s FROM invoices WHERE created_by=?", (user["id"],)).fetchone()["s"],
            "my_invoices": db.execute("SELECT COUNT(*) c FROM invoices WHERE created_by=?", (user["id"],)).fetchone()["c"],
            "my_payments": db.execute("SELECT COUNT(*) c FROM payments WHERE created_by=?", (user["id"],)).fetchone()["c"],
            "wallet_balance": float(wallet_row["balance"] or 0) if wallet_row else 0,
        }

        recent = []
        for inv in db.execute("SELECT invoice_no, total_amount, remaining_amount, created_at FROM invoices WHERE created_by=? ORDER BY id DESC LIMIT 5", (user["id"],)).fetchall():
            recent.append({"kind": "فاتورة", "ref": inv["invoice_no"], "amount": inv["total_amount"], "date": inv["created_at"][:10] if inv["created_at"] else ""})
        for pay in db.execute("SELECT id, amount, payment_date FROM payments WHERE created_by=? ORDER BY id DESC LIMIT 5", (user["id"],)).fetchall():
            recent.append({"kind": "سداد", "ref": pay["id"], "amount": pay["amount"], "date": pay["payment_date"][:10] if pay["payment_date"] else ""})
        recent = sorted(recent, key=lambda x: x["date"], reverse=True)[:8]

        role_actions = {
            "collector": [
                {"label": "جولة التحصيل", "url": url_for("collection_trip"), "icon": "bi-map"},
                {"label": "إضافة فاتورة", "url": url_for("invoice_new"), "icon": "bi-receipt"},
                {"label": "تسجيل سداد", "url": url_for("payment_center"), "icon": "bi-cash-coin"},
                {"label": "تقرير يومي", "url": url_for("collector_daily_report"), "icon": "bi-bar-chart"},
                {"label": "مراجعة فواتيري", "url": url_for("invoices"), "icon": "bi-journal-text"},
            ],
            "staff": [
                {"label": "إضافة فاتورة", "url": url_for("invoice_new"), "icon": "bi-receipt"},
                {"label": "تسجيل سداد", "url": url_for("payment_center"), "icon": "bi-cash-coin"},
                {"label": "المشتركين", "url": url_for("subscribers"), "icon": "bi-people"},
            ],
            "accountant": [
                {"label": "الحسابات", "url": url_for("accounts_tree"), "icon": "bi-diagram-3"},
                {"label": "تسجيل سداد", "url": url_for("payment_center"), "icon": "bi-cash-coin"},
                {"label": "التقارير", "url": url_for("reports"), "icon": "bi-graph-up"},
            ],
            "manager": [
                {"label": "الحسابات", "url": url_for("accounts_tree"), "icon": "bi-diagram-3"},
                {"label": "الموظفون", "url": url_for("employees"), "icon": "bi-people-fill"},
                {"label": "تسجيل سداد", "url": url_for("payment_center"), "icon": "bi-cash-coin"},
            ],
            "admin": [
                {"label": "الحسابات", "url": url_for("accounts_tree"), "icon": "bi-diagram-3"},
                {"label": "الموظفون", "url": url_for("employees"), "icon": "bi-people-fill"},
                {"label": "تقرير المتأخرات", "url": url_for("arrears_report"), "icon": "bi-exclamation-triangle"},
                {"label": "تقرير المحصّل", "url": url_for("collector_daily_report"), "icon": "bi-bar-chart"},
                {"label": "تسجيل سداد", "url": url_for("payment_center"), "icon": "bi-cash-coin"},
            ],
            "technician": [
                {"label": "مراقبة الأنشطة", "url": url_for("staff_monitor"), "icon": "bi-shield-shaded"},
                {"label": "الحسابات", "url": url_for("accounts_tree"), "icon": "bi-diagram-3"},
                {"label": "الموظفون", "url": url_for("employees"), "icon": "bi-people-fill"},
            ],
        }

        return render_template("employee_portal.html", employee=profile, stats=stats, recent=recent, user_role=user_role, wallet_row=wallet_row, role_actions=role_actions.get(user_role, []))

    @app.route("/invoices/<int:invoice_id>/print-batch")
    @login_required
    def invoice_print_batch(invoice_id=None):
        return redirect(url_for("invoices"))

    # --- واجهات الموظفين والسداد المحسّنة ---
    @app.route("/employees")
    @login_required
    @role_required(["admin", "technician"])
    
    def employees():
        db = get_db()
        q = request.args.get("q", "").strip()
        sql = """
            SELECT u.id, u.username, u.role, u.active, u.created_at, u.updated_at,
                   u.wallet_id, ep.full_name, ep.job_title, ep.monthly_target, ep.account_node_id,
                   an.code AS account_code, an.name AS account_name, an.node_type AS account_type
            FROM users u
            LEFT JOIN employee_profiles ep ON ep.user_id = u.id
            LEFT JOIN account_nodes an ON an.id = ep.account_node_id
        """
        params = []
        if q:
            sql += " WHERE u.username LIKE ? OR ep.full_name LIKE ? OR ep.job_title LIKE ? OR u.role LIKE ?"
            like = f"%{q}%"
            params = [like, like, like, like]
        sql += " ORDER BY u.id DESC"
        rows = db.execute(sql, params).fetchall()

        employee_accounts = db.execute("""
            SELECT a.id, a.code, a.name, a.node_type, a.is_postable
            FROM account_nodes a
            WHERE a.active=1 AND a.is_postable=1 AND a.node_type='employee'
            ORDER BY a.code
        """).fetchall()

        return render_template("employees.html", employees=rows, employee_accounts=employee_accounts, q=q)

    @app.route("/employees/new", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "technician"])
    def employee_new():
        db = get_db()
        employee_accounts = db.execute("""
            SELECT a.id, a.code, a.name, a.node_type, a.is_postable
            FROM account_nodes a
            WHERE a.active=1 AND a.is_postable=1 AND a.node_type='employee'
            ORDER BY a.code
        """).fetchall()
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "Collector").strip() or "Collector"
            wallet_id = request.form.get("wallet_id", type=int)
            account_node_id = request.form.get("account_node_id", type=int)
            full_name = request.form.get("full_name", "").strip()
            job_title = request.form.get("job_title", "").strip()
            monthly_target = parse_decimal(request.form.get("monthly_target"))
            active = 1 if request.form.get("active") == "1" else 0

            if not username:
                flash("اسم المستخدم مطلوب.", "danger")
                return render_template("employee_form.html", employee=None, employee_accounts=employee_accounts)
            if not password:
                flash("كلمة المرور مطلوبة عند إنشاء موظف جديد.", "danger")
                return render_template("employee_form.html", employee=None, employee_accounts=employee_accounts)

            if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                flash("اسم المستخدم مستخدم مسبقاً.", "danger")
                return render_template("employee_form.html", employee=None, employee_accounts=employee_accounts)

            try:
                db.execute(
                    "INSERT INTO users (username, password_hash, role, wallet_id, active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (username, generate_password_hash(password), role, wallet_id, active, now_iso(), now_iso()),
                )
                user_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

                if account_node_id:
                    used = db.execute("SELECT ep.user_id FROM employee_profiles ep WHERE ep.account_node_id = ? LIMIT 1", (account_node_id,)).fetchone()
                    if used:
                        raise ValueError("الحساب المختار مرتبط بموظف آخر.")
                    valid = db.execute("SELECT id FROM account_nodes WHERE id=? AND active=1 AND is_postable=1", (account_node_id,)).fetchone()
                    if not valid:
                        raise ValueError("الحساب المختار غير صالح.")
                    ensure_employee_profile(db, user_id, full_name=full_name or username, job_title=job_title or role, account_node_id=account_node_id)
                else:
                    create_employee_account(db, user_id, full_name=full_name or username, job_title=job_title or role)

                db.execute(
                    "UPDATE employee_profiles SET monthly_target=?, active=?, updated_at=? WHERE user_id=?",
                    (monthly_target, active, now_iso(), user_id),
                )
                db.commit()
                flash("تمت إضافة الموظف بنجاح.", "success")
                return redirect(url_for("employees"))
            except Exception as e:
                db.rollback()
                flash(f"تعذر إضافة الموظف: {e}", "danger")
        return render_template("employee_form.html", employee=None, employee_accounts=employee_accounts)

    @app.route("/employees/<int:user_id>/edit", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "technician"])
    def employee_edit(user_id):
        db = get_db()
        employee = db.execute("""
            SELECT u.id, u.username, u.role, u.active, u.wallet_id, ep.full_name, ep.job_title, ep.monthly_target,
                   ep.account_node_id, ep.notes
            FROM users u
            LEFT JOIN employee_profiles ep ON ep.user_id = u.id
            WHERE u.id = ?
        """, (user_id,)).fetchone()
        if not employee:
            abort(404)

        employee_accounts = db.execute("""
            SELECT a.id, a.code, a.name, a.node_type, a.is_postable
            FROM account_nodes a
            WHERE a.active=1 AND a.is_postable=1 AND a.node_type='employee'
            ORDER BY a.code
        """).fetchall()

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "Collector").strip() or "Collector"
            wallet_id = request.form.get("wallet_id", type=int)
            account_node_id = request.form.get("account_node_id", type=int)
            full_name = request.form.get("full_name", "").strip()
            job_title = request.form.get("job_title", "").strip()
            monthly_target = parse_decimal(request.form.get("monthly_target"))
            active = 1 if request.form.get("active") == "1" else 0

            try:
                if username:
                    if db.execute("SELECT 1 FROM users WHERE username = ? AND id <> ?", (username, user_id)).fetchone():
                        flash("اسم المستخدم مستخدم مسبقاً.", "danger")
                        return render_template("employee_form.html", employee=employee, employee_accounts=employee_accounts)
                    db.execute(
                        "UPDATE users SET username=?, role=?, wallet_id=?, active=?, updated_at=? WHERE id=?",
                        (username, role, wallet_id, active, now_iso(), user_id),
                    )
                else:
                    db.execute(
                        "UPDATE users SET role=?, wallet_id=?, active=?, updated_at=? WHERE id=?",
                        (role, wallet_id, active, now_iso(), user_id),
                    )

                if password:
                    db.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?", (generate_password_hash(password), now_iso(), user_id))

                if account_node_id:
                    used = db.execute("SELECT ep.user_id FROM employee_profiles ep WHERE ep.account_node_id = ? AND ep.user_id <> ? LIMIT 1", (account_node_id, user_id)).fetchone()
                    if used:
                        raise ValueError("الحساب المختار مرتبط بموظف آخر.")
                    valid = db.execute("SELECT id FROM account_nodes WHERE id=? AND active=1 AND is_postable=1", (account_node_id,)).fetchone()
                    if not valid:
                        raise ValueError("الحساب المختار غير صالح.")
                    ensure_employee_profile(db, user_id, full_name=full_name or username or employee["full_name"] or employee["username"], job_title=job_title or role, account_node_id=account_node_id)
                else:
                    if employee["account_node_id"]:
                        ensure_employee_profile(db, user_id, full_name=full_name or username or employee["full_name"] or employee["username"], job_title=job_title or role, account_node_id=employee["account_node_id"])
                    else:
                        create_employee_account(db, user_id, full_name=full_name or username or employee["full_name"] or employee["username"], job_title=job_title or role)

                db.execute(
                    "UPDATE employee_profiles SET monthly_target=?, active=?, updated_at=? WHERE user_id=?",
                    (monthly_target, active, now_iso(), user_id),
                )
                db.commit()
                flash("تم حفظ بيانات الموظف.", "success")
                return redirect(url_for("employees"))
            except Exception as e:
                db.rollback()
                flash(f"تعذر حفظ التعديلات: {e}", "danger")
        return render_template("employee_form.html", employee=employee, employee_accounts=employee_accounts)

    @app.route("/payments/manage", methods=["GET"])
    @login_required
    @role_required(["admin", "staff", "collector", "technician"])
    def payment_center():
        db = get_db()
        q = request.args.get("q", "").strip()
        user_role = (g.user["role"] or "").lower()
        user_id = g.user["id"]

        invoice_sql = """
            SELECT i.*, s.name subscriber_name, s.account_number, s.phone,
                   u.username AS created_by_name
            FROM invoices i
            JOIN subscribers s ON s.id = i.subscriber_id
            LEFT JOIN users u ON u.id = i.created_by
        """
        invoice_params = []
        if user_role in ["collector"]:
            invoice_sql += " WHERE i.created_by = ?"
            invoice_params.append(user_id)
        elif q:
            invoice_sql += " WHERE i.invoice_no LIKE ? OR s.name LIKE ? OR s.account_number LIKE ?"
            like = f"%{q}%"
            invoice_params.extend([like, like, like])
        invoice_sql += " ORDER BY i.id DESC LIMIT 100"
        invoices = db.execute(invoice_sql, invoice_params).fetchall()

        payment_sql = """
            SELECT p.*, i.invoice_no, s.name subscriber_name, s.account_number,
                   u.username AS created_by_name
            FROM payments p
            LEFT JOIN invoices i ON i.id = p.invoice_id
            LEFT JOIN subscribers s ON s.id = p.subscriber_id
            LEFT JOIN users u ON u.id = p.created_by
        """
        payment_params = []
        if user_role in ["collector"]:
            payment_sql += " WHERE p.created_by = ?"
            payment_params.append(user_id)
        elif q:
            payment_sql += " WHERE s.name LIKE ? OR s.account_number LIKE ? OR i.invoice_no LIKE ?"
            like = f"%{q}%"
            payment_params.extend([like, like, like])
        payment_sql += " ORDER BY p.id DESC LIMIT 100"
        payments = db.execute(payment_sql, payment_params).fetchall()

        wallets = db.execute("SELECT id, name, balance FROM wallets ORDER BY name").fetchall()
        accounts = db.execute("SELECT id, code, name, node_type, is_postable FROM account_nodes WHERE active=1 ORDER BY code").fetchall()
        account_options = flatten_account_options(promote_visible_account_tree(get_account_balance_rows(db)), only_postable=True)
        default_cash_account_id = get_default_cash_account_id(db)
        stats = {
            "invoice_count":   len(invoices),
            "payment_count":   len(payments),
            "invoice_total":   sum(float(inv["total_amount"]  or 0) for inv in invoices),
            "payment_total":   sum(float(p["amount"]          or 0) for p in payments),
            "remaining_total": sum(float(inv["remaining_amount"] or 0) for inv in invoices),
        }
        return render_template(
            "payments/index.html",
            invoices=invoices,
            payments=payments,
            wallets=wallets,
            accounts=accounts,
            account_options=account_options,
            default_cash_account_id=default_cash_account_id,
            stats=stats,
            q=q,
            selected_account_ids=_selected_account_ids_from_request(),
            current_user=g.user,
            user_role=user_role,
        )

    @app.route("/payments/manage/add", methods=["POST"])
    @login_required
    @role_required(["admin", "staff", "collector", "technician"])

    def payment_center_add():
        db = get_db()
        invoice_id = request.form.get("invoice_id", type=int)
        amount = parse_decimal(request.form.get("amount"))
        method = request.form.get("method", "cash").strip() or "cash"
        notes = request.form.get("notes", "").strip()
        attachment_path = save_upload(request.files.get("attachment"))
        # لا يُطلب من المستخدم اختيار مصدر السداد يدوياً.
        # المدير يُرحَّل افتراضياً إلى حساب الصندوق التفصيلي، وبقية الموظفين إلى حسابهم المحاسبي.
        if (g.user["role"] or "").lower() == "admin":
            cash_account_id = get_default_cash_account_id(db)
        else:
            cash_account_id = create_employee_account(
                db,
                g.user["id"],
                full_name=(g.user["username"] or None),
                job_title=g.user["role"] or "Collector",
            )

        if not invoice_id or amount <= 0:
            flash("اختر فاتورة صحيحة وأدخل مبلغاً صحيحاً.", "danger")
            return redirect(url_for("payment_center"))

        try:
            process_invoice_payment(
                db,
                invoice_id=invoice_id,
                user_id=session["user_id"],
                amount=amount,
                method=method,
                notes=notes,
                attachment_path=attachment_path,
                wallet_id=None,
                cash_account_id=cash_account_id,
                receivable_account_id=get_default_receivable_account_id(db),
            )
            db.commit()
            flash("✅ تم تسجيل السداد وربطه بحساب الموظف بنجاح.", "success")
            after_save = request.form.get("after_save", "stay")
            if after_save == "thermal":
                payment = db.execute(
                    "SELECT id FROM payments WHERE invoice_id=? ORDER BY id DESC LIMIT 1",
                    (invoice_id,),
                ).fetchone()
                if payment:
                    return redirect(url_for("thermal_receipt", payment_id=payment["id"]))
        except Exception as e:
            db.rollback()
            flash(f"❌ تعذر تسجيل السداد: {e}", "danger")
        return redirect(url_for("payment_center"))

    @app.route("/invoices/<int:invoice_id>/thermal-pay", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "collector", "staff"])

    def thermal_pay(invoice_id):
        db = get_db()

        def _fetch_invoice():
            return db.execute(
                """
                SELECT i.*, s.name subscriber_name, s.account_number, s.phone,
                       s.village, s.address, s.meter_number,
                       s.default_unit_price, s.default_subscription_fee
                FROM invoices i
                JOIN subscribers s ON s.id = i.subscriber_id
                WHERE i.id = ?
                """,
                (invoice_id,),
            ).fetchone()

        invoice = _fetch_invoice()
        if not invoice:
            abort(404)

        if request.method == "POST":
            amount = parse_decimal(request.form.get("amount"))
            if amount <= 0:
                flash("أدخل مبلغاً صحيحاً.", "danger")
                return redirect(url_for("thermal_pay", invoice_id=invoice_id))
            method = request.form.get("method", "نقداً")
            notes  = request.form.get("notes", "")
            try:
                collector_account_id = create_employee_account(
                    db,
                    session["user_id"],
                    full_name=(g.user["username"] if g.user else None),
                    job_title=(g.user["role"] if g.user else "Collector"),
                ) if (g.user and (g.user["role"] or "").lower() != "admin") else None

                process_invoice_payment(
                    db,
                    invoice_id=invoice_id,
                    user_id=session["user_id"],
                    amount=amount,
                    method=method,
                    notes=notes,
                    attachment_path=None,
                    wallet_id=None,
                    cash_account_id=(collector_account_id or request.form.get("account_id", type=int) or get_default_cash_account_id(db)),
                    receivable_account_id=get_default_receivable_account_id(db),
                )
                db.commit()
            except Exception as e:
                db.rollback()
                flash(f"خطأ في تسجيل السداد: {e}", "danger")
                return redirect(url_for("thermal_pay", invoice_id=invoice_id))

            payment = db.execute(
                "SELECT * FROM payments WHERE invoice_id = ? ORDER BY id DESC LIMIT 1",
                (invoice_id,),
            ).fetchone()
            invoice = _fetch_invoice()
            qr_data = make_qr_data(invoice)
            collector_name = db.execute(
                "SELECT username FROM users WHERE id = ?", (session["user_id"],)
            ).fetchone()["username"]
            settings = {k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}
            return render_template(
                "thermal_receipt.html",
                invoice=invoice,
                payment=payment,
                qr_data=qr_data,
                collector_name=collector_name,
                settings=settings,
                now_date=date.today().isoformat(),
                back_url=url_for("invoice_detail", invoice_id=invoice_id),
            )

        settings = {k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}
        return render_template(
            "thermal_pay_form.html",
            invoice=invoice,
            settings=settings,
        )

    @app.route("/payments/<int:payment_id>/thermal-receipt")
    @login_required
    @role_required(["admin", "collector", "staff"])
    def thermal_receipt(payment_id):
        db = get_db()
        payment = db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        if not payment:
            abort(404)
        invoice = db.execute(
            """
            SELECT i.*, s.name subscriber_name, s.account_number, s.phone,
                   s.village, s.address, s.meter_number
            FROM invoices i JOIN subscribers s ON s.id = i.subscriber_id
            WHERE i.id = ?
            """,
            (payment["invoice_id"],),
        ).fetchone()
        if not invoice:
            abort(404)
        collector_row = db.execute(
            "SELECT username FROM users WHERE id = ?", (payment["created_by"],)
        ).fetchone()
        collector_name = collector_row["username"] if collector_row else "-"
        qr_data  = make_qr_data(invoice)
        settings = {k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}
        return render_template(
            "thermal_receipt.html",
            invoice=invoice,
            payment=payment,
            qr_data=qr_data,
            collector_name=collector_name,
            settings=settings,
            now_date=date.today().isoformat(),
            back_url=url_for("invoice_detail", invoice_id=invoice["id"]),
        )

    # ─────────────────────────────────────────────────────────────────────
    # ③ طباعة فواتير بالتاريخ (من تاريخ إلى تاريخ)
    # ─────────────────────────────────────────────────────────────────────
    @app.route("/invoices/print-by-date")
    @login_required
    @role_required(["admin", "staff", "collector", "technician"])
    def invoices_print_by_date():
        date_from = request.args.get("date_from", "").strip()
        date_to   = request.args.get("date_to",   "").strip()
        q         = request.args.get("q", "").strip()

        if not date_from or not date_to:
            # عرض صفحة اختيار التاريخ
            return render_template(
                "invoices_date_filter.html",
                date_from=date_from,
                date_to=date_to,
                q=q,
            selected_account_ids=_selected_account_ids_from_request(),
            )

        db   = get_db()
        sql  = """
            SELECT i.*, s.name subscriber_name, s.account_number, s.phone,
                   s.village, s.address, s.meter_number,
                   s.active AS subscriber_active,
                   uc.username AS created_by_name
            FROM invoices i
            JOIN subscribers s ON s.id = i.subscriber_id
            LEFT JOIN users uc ON uc.id = i.created_by
            WHERE date(i.invoice_date) BETWEEN date(?) AND date(?)
        """
        params = [date_from, date_to]
        if q:
            sql   += " AND (i.invoice_no LIKE ? OR s.name LIKE ? OR s.account_number LIKE ?)"
            like   = f"%{q}%"
            params += [like, like, like]
        sql += " ORDER BY i.invoice_date ASC, i.id ASC"
        invoices = db.execute(sql, params).fetchall()

        if not invoices:
            flash(f"لا توجد فواتير في الفترة من {date_from} إلى {date_to}.", "warning")
            return redirect(url_for("invoices_print_by_date", date_from=date_from, date_to=date_to, q=q))

        settings        = {k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}
        total_amount    = sum(float(i["total_amount"]    or 0) for i in invoices)
        total_paid      = sum(float(i["paid_amount"]     or 0) for i in invoices)
        total_remaining = sum(float(i["remaining_amount"] or 0) for i in invoices)
        total_consumption = sum(float(i["consumption"]   or 0) for i in invoices)
        paid_count      = sum(1 for i in invoices if (i["remaining_amount"] or 0) <= 0)

        previous_invoices = {}
        for inv in invoices:
            previous_invoices[inv["id"]] = fetch_previous_invoice_summary(db, inv["subscriber_id"], inv["id"])

        return render_template(
            "invoices_batch_print.html",
            invoices=invoices,
            settings=settings,
            previous_invoices=previous_invoices,
            date_from=date_from,
            date_to=date_to,
            q=q,
            selected_account_ids=_selected_account_ids_from_request(),
            total_amount=total_amount,
            total_paid=total_paid,
            total_remaining=total_remaining,
            total_consumption=int(total_consumption),
            paid_count=paid_count,
            now_date=date.today().isoformat(),
            back_url=url_for("invoices"),
        )

    @app.route("/invoices/print-by-date/pdf")
    @login_required
    def invoices_print_by_date_pdf():
        date_from = request.args.get("date_from", "").strip()
        date_to   = request.args.get("date_to",   "").strip()
        q         = request.args.get("q", "").strip()

        if not date_from or not date_to:
            flash("يرجى تحديد الفترة الزمنية.", "warning")
            return redirect(url_for("invoices_print_by_date"))

        db = get_db()
        invoices, previous_invoices = _get_by_date_rows(db, date_from, date_to, q)

        if not invoices:
            flash(f"لا توجد فواتير في الفترة من {date_from} إلى {date_to}.", "warning")
            return redirect(url_for("invoices_print_by_date", date_from=date_from, date_to=date_to, q=q))

        html = render_template(
            "invoices_batch_print.html",
            invoices=invoices,
            settings={k: get_setting(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS},
            previous_invoices=previous_invoices,
            date_from=date_from,
            date_to=date_to,
            q=q,
            back_url=url_for("invoices"),
        )
        try:
            pdf = _render_invoices_pdf(html)
        except ValueError:
            return "ميزة تصدير الـ PDF غير متاحة بسبب عدم تثبيت Playwright", 500
        except Exception as exc:
            try:
                app.logger.exception("sync by-date pdf failed")
            except Exception:
                pass
            return f"تعذر توليد ملف PDF (عدد الفواتير: {len(invoices)}). جرّب التصدير من النافذة الخلفية أو قلل الفترة. التفاصيل: {exc}", 500
        fname = f"invoices_{date_from}_to_{date_to}.pdf"
        return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name=fname)

    @app.route("/invoices/print-by-date/pdf/start", methods=["POST"])
    @login_required
    def invoices_print_by_date_pdf_start():
        import jobs as _jobs
        payload = request.get_json(silent=True) or request.form
        date_from = (payload.get("date_from") or "").strip()
        date_to = (payload.get("date_to") or "").strip()
        q = (payload.get("q") or "").strip()
        if not date_from or not date_to:
            return jsonify({"error": "حدد الفترة الزمنية أولاً."}), 400
        job_id = _jobs.create_job("export-pdf", label="تصدير الفواتير بالتاريخ PDF")
        thread = threading.Thread(
            target=_by_date_pdf_worker,
            args=(app, job_id, date_from, date_to, q, f"invoices_{date_from}_to_{date_to}.pdf"),
            daemon=True,
        )
        thread.start()
        return jsonify({"job_id": job_id})

    # ═══════════════════════════════════════════════════════════════════
    # ① جولة التحصيل اليومية
    # ═══════════════════════════════════════════════════════════════════

    @app.route("/collection/trip", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "collector", "staff"])
    def collection_trip():
        db = get_db()
        user_id   = session["user_id"]
        user_role = (g.user["role"] or "").lower()
        today     = date.today().isoformat()

        if request.method == "POST":
            action = request.form.get("action")

            # ── فتح جولة جديدة ──
            if action == "open":
                # أغلق أي جولة مفتوحة اليوم لنفس المحصل
                db.execute(
                    "UPDATE collection_trips SET status='closed', closed_at=? WHERE collector_id=? AND status='open'",
                    (now_iso(), user_id),
                )
                invoice_ids = request.form.getlist("invoice_ids")
                if not invoice_ids:
                    flash("اختر فاتورة واحدة على الأقل.", "danger")
                    return redirect(url_for("collection_trip"))
                target = db.execute(
                    f"SELECT COALESCE(SUM(remaining_amount),0) s FROM invoices WHERE id IN ({','.join(['?']*len(invoice_ids))})",
                    invoice_ids,
                ).fetchone()["s"]
                trip_id = db.execute(
                    "INSERT INTO collection_trips (collector_id,trip_date,status,target_amount,started_at,created_at) VALUES (?,?,?,?,?,?)",
                    (user_id, today, "open", target, now_iso(), now_iso()),
                ).lastrowid
                for iid in invoice_ids:
                    inv = db.execute("SELECT subscriber_id FROM invoices WHERE id=?", (iid,)).fetchone()
                    if inv:
                        db.execute(
                            "INSERT INTO trip_visits (trip_id,invoice_id,subscriber_id,status,created_at) VALUES (?,?,?,?,?)",
                            (trip_id, iid, inv["subscriber_id"], "pending", now_iso()),
                        )
                db.commit()
                flash("تم فتح الجولة بنجاح.", "success")
                return redirect(url_for("collection_trip"))

            # ── تحديث حالة زيارة ──
            if action == "update_visit":
                visit_id  = request.form.get("visit_id", type=int)
                status    = request.form.get("status", "pending")
                notes_v   = request.form.get("notes", "")
                visit = db.execute("SELECT * FROM trip_visits WHERE id=?", (visit_id,)).fetchone()
                if visit:
                    amount_collected = 0.0
                    if status == "collected":
                        amount_collected = parse_decimal(request.form.get("amount_collected", "0"))
                        if amount_collected > 0:
                            try:
                                process_invoice_payment(
                                    db, invoice_id=visit["invoice_id"],
                                    user_id=user_id, amount=amount_collected,
                                    method="نقداً - جولة تحصيل", notes=notes_v,
                                    attachment_path=None,
                                    wallet_id=resolve_payment_accounts(db, user_id)[0],
                                    cash_account_id=resolve_payment_accounts(db, user_id)[1],
                                    receivable_account_id=resolve_payment_accounts(db, user_id)[2],
                                )
                            except Exception as e:
                                db.rollback()
                                flash(f"خطأ في تسجيل السداد: {e}", "danger")
                                return redirect(url_for("collection_trip"))
                    db.execute(
                        "UPDATE trip_visits SET status=?,notes=?,amount_collected=?,visited_at=? WHERE id=?",
                        (status, notes_v, amount_collected, now_iso(), visit_id),
                    )
                    # تحديث إجمالي الجولة
                    trip_total = db.execute(
                        "SELECT COALESCE(SUM(amount_collected),0) s FROM trip_visits WHERE trip_id=?",
                        (visit["trip_id"],),
                    ).fetchone()["s"]
                    db.execute(
                        "UPDATE collection_trips SET collected_amount=? WHERE id=?",
                        (trip_total, visit["trip_id"]),
                    )
                    db.commit()
                    flash("تم تحديث الزيارة.", "success")
                return redirect(url_for("collection_trip"))

            # ── إغلاق الجولة ──
            if action == "close_trip":
                trip_id = request.form.get("trip_id", type=int)
                notes_t = request.form.get("notes", "")
                db.execute(
                    "UPDATE collection_trips SET status='closed',closed_at=?,notes=? WHERE id=? AND collector_id=?",
                    (now_iso(), notes_t, trip_id, user_id),
                )
                db.commit()
                flash("تم إغلاق الجولة بنجاح.", "success")
                return redirect(url_for("collection_trip"))

        # ── GET: عرض الجولة المفتوحة أو صفحة البدء ──
        open_trip = db.execute(
            "SELECT * FROM collection_trips WHERE collector_id=? AND status='open' ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()

        visits = []
        if open_trip:
            visits = db.execute(
                """
                SELECT tv.*, i.invoice_no, i.total_amount, i.paid_amount, i.remaining_amount,
                       i.month_label, i.invoice_date,
                       s.name subscriber_name, s.account_number, s.phone, s.address
                FROM trip_visits tv
                JOIN invoices i  ON i.id  = tv.invoice_id
                JOIN subscribers s ON s.id = tv.subscriber_id
                WHERE tv.trip_id = ?
                ORDER BY tv.id ASC
                """,
                (open_trip["id"],),
            ).fetchall()

        # فواتير غير محصّلة لاختيارها في الجولة الجديدة
        pending_invoices = db.execute(
            """
            SELECT i.id, i.invoice_no, i.remaining_amount, i.month_label, i.invoice_date,
                   s.name subscriber_name, s.account_number, s.phone, s.address
            FROM invoices i
            JOIN subscribers s ON s.id = i.subscriber_id
            WHERE i.remaining_amount > 0
              AND i.id NOT IN (
                  SELECT invoice_id FROM trip_visits tv
                  JOIN collection_trips ct ON ct.id = tv.trip_id
                  WHERE ct.status = 'open' AND ct.collector_id = ?
              )
            ORDER BY s.account_number ASC
            """,
            (user_id,),
        ).fetchall()

        # آخر 5 جولات مغلقة
        past_trips = db.execute(
            """
            SELECT ct.*, 
                   (SELECT COUNT(*) FROM trip_visits WHERE trip_id=ct.id) total_visits,
                   (SELECT COUNT(*) FROM trip_visits WHERE trip_id=ct.id AND status='collected') collected_visits
            FROM collection_trips ct
            WHERE ct.collector_id=? AND ct.status='closed'
            ORDER BY ct.id DESC LIMIT 5
            """,
            (user_id,),
        ).fetchall()

        currency = get_setting("currency_name", "ريال")
        return render_template(
            "collection_trip.html",
            open_trip=open_trip,
            visits=visits,
            pending_invoices=pending_invoices,
            past_trips=past_trips,
            today=today,
            currency=currency,
        )

    # ═══════════════════════════════════════════════════════════════════
    # ② تقرير المتأخرات المتقدم
    # ═══════════════════════════════════════════════════════════════════

    @app.route("/reports/arrears")
    @login_required
    @role_required(["admin", "staff", "collector", "accountant", "manager", "technician"])
    def arrears_report():
        db  = get_db()
        q   = request.args.get("q", "").strip()
        bracket = request.args.get("bracket", "all")   # all | 1 | 2-3 | 4-6 | 6+
        fmt_print = request.args.get("print") == "1"

        # جميع الفواتير غير المسددة بالكامل
        sql = """
            SELECT i.id, i.invoice_no, i.invoice_date, i.month_label,
                   i.total_amount, i.paid_amount, i.remaining_amount,
                   s.id subscriber_id, s.name subscriber_name,
                   s.account_number, s.phone, s.village, s.address, s.meter_number,
                   uc.username created_by_name
            FROM invoices i
            JOIN subscribers s ON s.id = i.subscriber_id
            LEFT JOIN users uc ON uc.id = i.created_by
            WHERE i.remaining_amount > 0
        """
        params = []
        if q:
            sql += " AND (s.name LIKE ? OR s.account_number LIKE ? OR i.invoice_no LIKE ?)"
            like = f"%{q}%"
            params += [like, like, like]
        sql += " ORDER BY s.account_number, i.invoice_date ASC"
        rows = db.execute(sql, params).fetchall()

        # تجميع حسب المشترك
        from collections import defaultdict
        subscribers_map = defaultdict(lambda: {
            "subscriber_name": "", "account_number": "", "phone": "",
            "address": "", "invoices": [], "total_remaining": 0.0,
            "months_count": 0,
        })
        today_dt = date.today()
        for r in rows:
            sid = r["subscriber_id"]
            entry = subscribers_map[sid]
            entry["subscriber_id"]   = sid
            entry["subscriber_name"] = r["subscriber_name"]
            entry["account_number"]  = r["account_number"]
            entry["phone"]           = r["phone"] or ""
            entry["address"]         = r["address"] or ""
            entry["total_remaining"] = round(entry["total_remaining"] + float(r["remaining_amount"] or 0), 2)
            entry["months_count"]    += 1
            entry["invoices"].append(dict(r))

        # تصنيف شرائح التأخر
        def bracket_of(n):
            if n == 1:       return "1"
            if n <= 3:       return "2-3"
            if n <= 6:       return "4-6"
            return "6+"

        result = []
        for entry in subscribers_map.values():
            n = entry["months_count"]
            entry["bracket"] = bracket_of(n)
            if bracket != "all" and entry["bracket"] != bracket:
                continue
            result.append(entry)

        result.sort(key=lambda x: x["months_count"], reverse=True)

        # إحصاءات الشرائح
        all_entries = list(subscribers_map.values())
        bracket_stats = {
            "1":   {"count": 0, "total": 0},
            "2-3": {"count": 0, "total": 0},
            "4-6": {"count": 0, "total": 0},
            "6+":  {"count": 0, "total": 0},
        }
        for e in all_entries:
            b = bracket_of(e["months_count"])
            bracket_stats[b]["count"] += 1
            bracket_stats[b]["total"] += e["total_remaining"]

        grand_total    = sum(e["total_remaining"] for e in all_entries)
        grand_count    = len(all_entries)
        currency       = get_setting("currency_name", "ريال")
        org            = get_setting("organization_name", "")
        project        = get_setting("project_name", "")

        return render_template(
            "arrears_report.html",
            result=result,
            bracket_stats=bracket_stats,
            grand_total=grand_total,
            grand_count=grand_count,
            q=q,
            selected_account_ids=_selected_account_ids_from_request(),
            bracket=bracket,
            currency=currency,
            org=org,
            project=project,
            today=today_dt.isoformat(),
            fmt_print=fmt_print,
        )

    # ═══════════════════════════════════════════════════════════════════
    # ③ تقرير المحصّل اليومي
    # ═══════════════════════════════════════════════════════════════════

    @app.route("/reports/collector-daily")
    @login_required
    @role_required(["admin", "collector", "staff", "accountant", "manager"])
    def collector_daily_report():
        db         = get_db()
        user_role  = (g.user["role"] or "").lower()
        user_id    = g.user["id"]
        report_date = request.args.get("report_date", date.today().isoformat()).strip()
        # Admin/Manager يرى كل المحصلين؛ Collector يرى نفسه فقط
        target_collector = request.args.get("collector_id", type=int)
        if user_role in ["collector", "staff"]:
            target_collector = user_id

        # قائمة المحصلين للفلتر (admin فقط)
        collectors = []
        if user_role in ["admin", "manager", "accountant"]:
            collectors = db.execute(
                "SELECT id, username, role FROM users WHERE active=1 ORDER BY username"
            ).fetchall()

        # بناء شرط المحصل
        collector_filter     = " AND p.created_by = ?" if target_collector else ""
        collector_filter_inv = " AND i.created_by = ?" if target_collector else ""
        params_p  = [report_date, report_date]
        params_i  = [report_date, report_date]
        if target_collector:
            params_p.append(target_collector)
            params_i.append(target_collector)

        # السدادات في التاريخ المحدد
        payments_sql = f"""
            SELECT p.id, p.amount, p.method, p.payment_date, p.notes,
                   i.invoice_no, s.name subscriber_name, s.account_number,
                   u.username collector_name
            FROM payments p
            LEFT JOIN invoices i    ON i.id = p.invoice_id
            LEFT JOIN subscribers s ON s.id = p.subscriber_id
            LEFT JOIN users u       ON u.id = p.created_by
            WHERE date(p.payment_date) BETWEEN date(?) AND date(?)
            {collector_filter}
            ORDER BY p.payment_date ASC
        """
        payments = db.execute(payments_sql, params_p).fetchall()

        # الفواتير المنشأة في التاريخ المحدد
        invoices_sql = f"""
            SELECT i.id, i.invoice_no, i.total_amount, i.paid_amount,
                   i.remaining_amount, i.invoice_date, i.month_label,
                   s.name subscriber_name, s.account_number,
                   u.username creator_name
            FROM invoices i
            JOIN subscribers s ON s.id = i.subscriber_id
            LEFT JOIN users u  ON u.id = i.created_by
            WHERE date(i.invoice_date) BETWEEN date(?) AND date(?)
            {collector_filter_inv}
            ORDER BY i.invoice_date ASC
        """
        invoices = db.execute(invoices_sql, params_i).fetchall()

        # الجولة المغلقة لهذا اليوم والمحصل
        trip = None
        if target_collector:
            trip = db.execute(
                "SELECT * FROM collection_trips WHERE collector_id=? AND trip_date=? ORDER BY id DESC LIMIT 1",
                (target_collector, report_date),
            ).fetchone()

        # إحصاءات
        total_collected = sum(float(p["amount"] or 0) for p in payments)
        by_method: dict = {}
        for p in payments:
            m = p["method"] or "أخرى"
            by_method[m] = by_method.get(m, 0.0) + float(p["amount"] or 0)
        total_invoiced  = sum(float(i["total_amount"] or 0) for i in invoices)

        # إحصاءات الجولة
        trip_stats = None
        if trip:
            visits = db.execute(
                "SELECT status, COUNT(*) c, COALESCE(SUM(amount_collected),0) s FROM trip_visits WHERE trip_id=? GROUP BY status",
                (trip["id"],),
            ).fetchall()
            trip_stats = {v["status"]: {"count": v["c"], "sum": v["s"]} for v in visits}

        currency  = get_setting("currency_name", "ريال")
        org       = get_setting("organization_name", "")
        project   = get_setting("project_name", "")
        eng       = get_setting("engineer_name", "")

        # اسم المحصل للتقرير
        collector_name = ""
        if target_collector:
            u = db.execute("SELECT username FROM users WHERE id=?", (target_collector,)).fetchone()
            collector_name = u["username"] if u else ""

        return render_template(
            "collector_daily_report.html",
            payments=payments,
            invoices=invoices,
            trip=trip,
            trip_stats=trip_stats,
            total_collected=total_collected,
            total_invoiced=total_invoiced,
            by_method=by_method,
            report_date=report_date,
            currency=currency,
            org=org,
            project=project,
            eng=eng,
            collectors=collectors,
            target_collector=target_collector,
            collector_name=collector_name,
            user_role=user_role,
        )


    def _serialize_account_tree(nodes):
        payload = []
        for node in nodes or []:
            children = _serialize_account_tree(node.get("children") or [])
            payload.append({
                "id": node.get("id"),
                "code": node.get("code"),
                "name": node.get("name"),
                "node_type": node.get("node_type"),
                "is_postable": bool(node.get("is_postable")),
                "active": bool(node.get("active", True)),
                "balance": float(node.get("balance") or 0),
                "debit_total": float(node.get("debit_total") or 0),
                "credit_total": float(node.get("credit_total") or 0),
                "children": children,
            })
        return payload

    def _voucher_json(v):
        if not v:
            return None
        return {
            "id": v["id"],
            "voucher_no": v["voucher_no"],
            "voucher_type": v["voucher_type"],
            "voucher_date": v["voucher_date"],
            "amount": float(v["amount"] or 0),
            "from_account_id": v["from_account_id"],
            "from_account_name": v["from_account_name"],
            "from_code": v["from_code"],
            "to_account_id": v["to_account_id"],
            "to_account_name": v["to_account_name"],
            "to_code": v["to_code"],
            "reference": v["reference"],
            "description": v["description"],
            "source_type": v["source_type"],
            "source_id": v["source_id"],
            "journal_entry_id": v["journal_entry_id"],
            "status": v["status"],
            "created_by_name": v["created_by_name"],
            "created_at": v["created_at"],
            "updated_at": v["updated_at"],
        }

    def _journal_json(item):
        if not item:
            return None
        entry = item.get("entry")
        lines = item.get("lines") or []
        return {
            "id": entry["id"],
            "reference": entry["entry_no"],
            "date": entry["entry_date"],
            "description": entry["description"],
            "source_type": entry["source_type"],
            "source_id": entry["source_id"],
            "created_by": entry["created_by"],
            "created_by_name": entry["created_by_name"],
            "created_at": entry["created_at"],
            "total_debit": float(item.get("debit_total") or 0),
            "total_credit": float(item.get("credit_total") or 0),
            "is_posted": True,
            "lines": [
                {
                    "id": line["id"] if "id" in line.keys() else None,
                    "account_id": line["account_id"],
                    "account_code": line["account_code"],
                    "account_name": line["account_name"],
                    "account_type": line["account_type"],
                    "debit": float(line["debit"] or 0),
                    "credit": float(line["credit"] or 0),
                    "description": line["description"],
                }
                for line in lines
            ],
        }

    @app.route("/api/v1/accounting/accounts/next-code/")
    @login_required
    @role_required(["admin", "technician"])
    def api_account_next_code():
        db = get_db()
        parent_id = request.args.get("parent_id", type=int)
        if not parent_id:
            return jsonify({"code": "", "node_type": ""})
        parent = db.execute(
            "SELECT id, code, name, node_type, parent_id, is_postable FROM account_nodes WHERE id=? AND active=1",
            (parent_id,),
        ).fetchone()
        if not parent:
            return jsonify({"error": "not_found"}), 404
        payload = {
            "code": next_child_account_code(db, parent_id),
            "node_type": infer_child_account_type(parent),
            "parent_name": parent["name"],
        }
        return jsonify(payload)

    @app.route("/api/v1/accounting/accounts/tree/")
    @login_required
    def api_account_tree():
        db = get_db()
        tree = promote_visible_account_tree(get_account_balance_rows(db))
        return jsonify({"results": _serialize_account_tree(tree)})

    @app.route("/api/v1/accounting/vouchers/", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "staff", "collector", "technician", "accountant", "manager"])
    def api_vouchers():
        db = get_db()
        if request.method == "GET":
            voucher_type = request.args.get("type", "").strip() or None
            rows = vouchers_list(db, voucher_type=voucher_type)
            return jsonify({"results": [_voucher_json(r) for r in rows]})

        payload = request.get_json(silent=True) or request.form
        voucher_type = (payload.get("voucher_type") or "receipt").strip()
        voucher_date = (payload.get("voucher_date") or date.today().isoformat()).strip()
        reference = (payload.get("reference") or "").strip()
        description = (payload.get("description") or "").strip()
        from_account_id = int(payload.get("from_account_id") or payload.get("from_account") or 0)
        to_account_id = int(payload.get("to_account_id") or payload.get("to_account") or 0)
        amount = parse_decimal(payload.get("amount"))
        if not from_account_id or not to_account_id or amount <= 0:
            return jsonify({"error": "invalid_payload"}), 400
        from_account = db.execute("SELECT id FROM account_nodes WHERE id=? AND active=1 AND is_postable=1", (from_account_id,)).fetchone()
        to_account = db.execute("SELECT id FROM account_nodes WHERE id=? AND active=1 AND is_postable=1", (to_account_id,)).fetchone()
        if not from_account or not to_account:
            return jsonify({"error": "account_not_found"}), 400
        if is_date_in_closed_year(db, voucher_date):
            return jsonify({"error": "السنة المالية مقفلة لهذا التاريخ."}), 400
        try:
            voucher_id = create_accounting_voucher(
                db,
                voucher_type=voucher_type,
                voucher_date=voucher_date,
                amount=amount,
                from_account_id=from_account_id,
                to_account_id=to_account_id,
                reference=reference,
                description=description,
                created_by=g.user["id"],
                source_type="manual",
                source_id=None,
            )
            db.commit()
            row = get_voucher_by_id(db, voucher_id)
            return jsonify(_voucher_json(row)), 201
        except Exception as exc:
            db.rollback()
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/v1/accounting/vouchers/<int:voucher_id>/", methods=["GET", "PUT", "DELETE"])
    @login_required
    @role_required(["admin", "staff", "collector", "technician", "accountant", "manager"])
    def api_voucher_detail(voucher_id):
        db = get_db()
        row = get_voucher_by_id(db, voucher_id)
        if not row:
            return jsonify({"error": "not_found"}), 404
        if request.method == "GET":
            return jsonify(_voucher_json(row))
        if request.method == "DELETE":
            try:
                if is_date_in_closed_year(db, row["voucher_date"]):
                    return jsonify({"error": "السند في سنة مالية مقفلة."}), 400
                void_accounting_voucher(db, voucher_id)
                db.commit()
                return jsonify({"ok": True})
            except Exception as exc:
                db.rollback()
                return jsonify({"error": str(exc)}), 400

        payload = request.get_json(silent=True) or request.form
        try:
            _new_vdate = (payload.get("voucher_date") or row["voucher_date"]).strip()
            if is_date_in_closed_year(db, row["voucher_date"]) or is_date_in_closed_year(db, _new_vdate):
                return jsonify({"error": "السند في سنة مالية مقفلة."}), 400
            update_accounting_voucher(
                db,
                voucher_id,
                voucher_type=(payload.get("voucher_type") or row["voucher_type"]).strip(),
                voucher_date=(payload.get("voucher_date") or row["voucher_date"]).strip(),
                amount=parse_decimal(payload.get("amount") or row["amount"]),
                from_account_id=int(payload.get("from_account_id") or payload.get("from_account") or row["from_account_id"]),
                to_account_id=int(payload.get("to_account_id") or payload.get("to_account") or row["to_account_id"]),
                reference=(payload.get("reference") or row["reference"] or "").strip(),
                description=(payload.get("description") or row["description"] or "").strip(),
                updated_by=g.user["id"],
            )
            db.commit()
            return jsonify(_voucher_json(get_voucher_by_id(db, voucher_id)))
        except Exception as exc:
            db.rollback()
            return jsonify({"error": str(exc)}), 400
    @app.route('/user-manual')
    def user_manual():
        return render_template('user_manual.html')

    @app.route("/developer-tools", methods=["GET", "POST"])
    @login_required
    @role_required(["admin"])
    def developer_tools():
        dev_secret = os.environ.get("DEV_SECRET_CODE", "")
        if not dev_secret:
            dev_secret = secrets.token_urlsafe(9)
        if request.method == "POST":
            secret = request.form.get("dev_secret", "").strip()
            if not secrets.compare_digest(secret, dev_secret):
                flash("رمز المبرمج غير صحيح.", "danger")
                return redirect(url_for("developer_tools"))
            chosen_mode = request.form.get("database_mode", "local").strip()
            external_path = request.form.get("database_path", "").strip()
            set_setting("database_mode", chosen_mode)
            set_setting("external_database_path", external_path)
            flash("تم حفظ إعدادات المبرمج.", "success")
            return redirect(url_for("developer_tools"))
        return render_template(
            "developer_tools.html",
            db_path=database_file_path,
            database_mode=get_setting("database_mode", "local"),
            external_db=get_setting("external_database_path", ""),
        )

    @app.route("/vouchers")
    @login_required
    @role_required(["admin", "staff", "collector", "technician", "accountant", "manager"])
    def vouchers_page():
        return render_template("vouchers.html", current_user=g.user, user_role=(g.user["role"] or "").lower())

    @app.route("/accounting/journal")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_journal():
        return render_template("journal.html", current_user=g.user, user_role=(g.user["role"] or "").lower())

    @app.route("/accounting/reports")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_reports():
        db = get_db()
        start = request.args.get("start", date(date.today().year, date.today().month, 1).isoformat())
        end = request.args.get("end", date.today().isoformat())
        q = request.args.get("q", "").strip()

        all_accounts = list_account_balances_flat(db, start=start, end=end)
        all_accounts = [r for r in all_accounts if str(r["node_type"] or "").lower() != "root"]

        account_ids = []
        for value in request.args.getlist("account_ids"):
            try:
                account_ids.append(int(value))
            except (TypeError, ValueError):
                continue

        account_rows = all_accounts
        if account_ids:
            selected_ids = set(account_ids)
            account_rows = [r for r in account_rows if int(r["id"]) in selected_ids]
        if q:
            ql = q.lower()
            account_rows = [r for r in account_rows if ql in str(r["code"]).lower() or ql in str(r["name"]).lower()]

        detail_rows = [r for r in account_rows if r["is_postable"]]
        summary = {
            "debit_total": sum(float(r["debit"] or 0) for r in detail_rows),
            "credit_total": sum(float(r["credit"] or 0) for r in detail_rows),
            "balance_total": sum(float(r["balance"] or 0) for r in detail_rows),
        }

        vouchers = vouchers_list(db)
        journal_entries = list_journal_entries(db, start=start, end=end, reference=q or None, limit=200)
        balance_issues = find_main_account_direct_balances(db)

        return render_template(
            "accounting_reports.html",
            start=start,
            end=end,
            q=q,
            selected_account_ids=_selected_account_ids_from_request(),
            all_accounts=all_accounts,
            account_rows=account_rows,
            vouchers=vouchers,
            journal_entries=journal_entries,
            summary=summary,
            balance_issues=balance_issues,
        )

    @app.route("/accounting/ledger")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_ledger():
        db = get_db()
        start = request.args.get("start", date(date.today().year, date.today().month, 1).isoformat())
        end = request.args.get("end", date.today().isoformat())
        account_id = request.args.get("account_id", type=int)
        all_accounts = list_account_balances_flat(db)
        all_accounts = [r for r in all_accounts if r["is_postable"]]
        ledger = get_ledger(db, account_id, start=start, end=end) if account_id else None
        return render_template(
            "ledger.html",
            start=start, end=end, account_id=account_id,
            all_accounts=all_accounts, ledger=ledger,
            currency=get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]),
        )

    @app.route("/accounting/trial-balance")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_trial_balance():
        db = get_db()
        start = request.args.get("start", date(date.today().year, 1, 1).isoformat())
        end = request.args.get("end", date.today().isoformat())
        tb = get_trial_balance(db, start=start, end=end)
        return render_template(
            "trial_balance.html",
            start=start, end=end, tb=tb,
            currency=get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]),
        )

    @app.route("/accounting/income-statement")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_income_statement():
        db = get_db()
        start = request.args.get("start", date(date.today().year, 1, 1).isoformat())
        end = request.args.get("end", date.today().isoformat())
        income = get_income_statement(db, start=start, end=end)
        return render_template(
            "income_statement.html",
            start=start, end=end, income=income,
            currency=get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]),
        )

    @app.route("/accounting/balance-sheet")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_balance_sheet():
        db = get_db()
        start = request.args.get("start", date(date.today().year, 1, 1).isoformat())
        end = request.args.get("end", date.today().isoformat())
        bs = get_balance_sheet(db, start=start, end=end)
        return render_template(
            "balance_sheet.html",
            start=start, end=end, bs=bs,
            currency=get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]),
        )

    @app.route("/accounting/fiscal-years", methods=["GET", "POST"])
    @login_required
    @role_required(["admin"])
    def accounting_fiscal_years():
        db = get_db()
        ensure_fiscal_tables(db)
        if request.method == "POST":
            action = request.form.get("action", "create")
            try:
                if action == "create":
                    create_fiscal_year(db, request.form.get("name", "").strip(),
                                       request.form.get("start_date", "").strip(),
                                       request.form.get("end_date", "").strip())
                    flash("تم إنشاء السنة المالية.", "success")
                elif action == "close":
                    entry_id = close_fiscal_year(db, int(request.form.get("year_id", 0)), session.get("user_id"))
                    flash(f"تم إقفال السنة وترحيل الأرصدة (قيد {entry_id}).", "success")
                elif action == "opening":
                    entry_id = create_opening_entry(db, int(request.form.get("year_id", 0)), session.get("user_id"))
                    flash(f"تم إنشاء القيد الافتتاحي (قيد {entry_id}).", "success")
                elif action == "reopen":
                    reopen_fiscal_year(db, int(request.form.get("year_id", 0)))
                    flash("تمت إعادة فتح السنة.", "info")
            except Exception as exc:
                db.rollback()
                flash(f"تعذر تنفيذ العملية: {exc}", "danger")
            return redirect(url_for("accounting_fiscal_years"))
        years = list_fiscal_years(db)
        return render_template("fiscal_years.html", years=years)

    @app.route("/accounting/counts", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "technician", "accountant", "manager", "collector"])
    def accounting_counts():
        db = get_db()
        ensure_fiscal_tables(db)
        if request.method == "POST":
            action = request.form.get("action", "save")
            try:
                if action == "review":
                    review_cash_count(db, int(request.form.get("count_id", 0)), session.get("user_id"))
                    flash("تمت مراجعة الجرد.", "success")
                elif action == "approve":
                    approve_cash_count(db, int(request.form.get("count_id", 0)), session.get("user_id"))
                    flash("تم اعتماد الجرد.", "success")
                else:
                    account_id = int(request.form.get("account_id", 0))
                    count_date = request.form.get("count_date", date.today().isoformat()).strip()
                    actual = float(request.form.get("actual") or 0)
                    expected = get_account_closing_balance(db, account_id, end=count_date)
                    save_cash_count(db, count_date, account_id, expected, actual,
                                    request.form.get("notes", "").strip(), session.get("user_id"))
                    flash(f"تم حفظ الجرد. الفرق: {actual - expected:.0f}", "success")
            except Exception as exc:
                db.rollback()
                flash(f"تعذر حفظ الجرد: {exc}", "danger")
            return redirect(url_for("accounting_counts"))
        cash_accounts = db.execute(
            "SELECT id, code, name FROM account_nodes WHERE active=1 AND is_postable=1 AND (node_type IN ('cash','bank','employee') OR code LIKE '11%' OR code LIKE '13%') ORDER BY code"
        ).fetchall()
        counts = list_cash_counts(db, limit=200)
        balances = {r["id"]: get_account_closing_balance(db, r["id"]) for r in cash_accounts}
        return render_template("counts.html", cash_accounts=cash_accounts, counts=counts,
                               balances=balances, today=date.today().isoformat(),
                               currency=get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]))

    @app.route("/accounting/counts/<int:count_id>/minutes")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager", "collector"])
    def accounting_count_minutes(count_id):
        db = get_db()
        row = get_count_minutes(db, count_id)
        if not row:
            abort(404)
        return render_template("count_minutes.html", c=row,
                               org=get_setting("organization_name", DEFAULT_SETTINGS["organization_name"]),
                               currency=get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]))

    @app.route("/accounting/reconciliation")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_reconciliation():
        db = get_db()
        day = request.args.get("day", date.today().isoformat())
        end = request.args.get("end", date.today().isoformat())
        daily = get_daily_collection_summary(db, day)
        rows = get_receivable_reconciliation(db, end=end)
        diff_total = sum(abs(r["difference"] or 0) for r in rows)
        return render_template("reconciliation.html", day=day, end=end, daily=daily, rows=rows,
                               diff_total=diff_total,
                               currency=get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]))

    @app.route("/accounting/adjustment", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "accountant", "manager", "technician"])
    def accounting_adjustment():
        db = get_db()
        accounts = db.execute(
            "SELECT id, code, name FROM account_nodes WHERE active=1 AND is_postable=1 ORDER BY code"
        ).fetchall()
        if request.method == "POST":
            try:
                entry_date = (request.form.get("entry_date") or date.today().isoformat()).strip()
                debit_id = int(request.form.get("debit_account_id") or 0)
                credit_id = int(request.form.get("credit_account_id") or 0)
                amount = parse_decimal(request.form.get("amount"))
                description = (request.form.get("description") or "قيد تسوية").strip()
                if not debit_id or not credit_id or debit_id == credit_id:
                    raise ValueError("اختر حسابين مختلفين.")
                if amount <= 0:
                    raise ValueError("المبلغ يجب أن يكون أكبر من صفر.")
                if is_date_in_closed_year(db, entry_date):
                    raise ValueError("السنة المالية مقفلة لهذا التاريخ.")
                entry_id = create_manual_journal_entry(
                    db, entry_date=entry_date, description=description,
                    lines=[{"account_id": debit_id, "debit": amount, "credit": 0.0, "description": description},
                           {"account_id": credit_id, "debit": 0.0, "credit": amount, "description": description}],
                    created_by=session.get("user_id"), source_type="adjustment", source_id=None,
                )
                db.commit()
                flash(f"تم إنشاء قيد التسوية (قيد {entry_id}).", "success")
                return redirect(url_for("accounting_journal"))
            except Exception as exc:
                db.rollback()
                flash(f"تعذر إنشاء قيد التسوية: {exc}", "danger")
        prefill = {
            "entry_date": request.args.get("entry_date", date.today().isoformat()),
            "debit_account_id": request.args.get("debit_account_id", type=int),
            "credit_account_id": request.args.get("credit_account_id", type=int),
            "amount": request.args.get("amount", ""),
            "description": request.args.get("description", "قيد تسوية"),
        }
        return render_template("adjustment.html", accounts=accounts, prefill=prefill)

    @app.route("/accounting/cash-flow")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_cash_flow():
        db = get_db()
        start = request.args.get("start", date(date.today().year, 1, 1).isoformat())
        end = request.args.get("end", date.today().isoformat())
        cf = get_cash_flow(db, start=start, end=end)
        return render_template("cash_flow.html", start=start, end=end, cf=cf,
                               currency=get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]))

    @app.route("/accounting/dashboard")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_dashboard():
        db = get_db()
        dash = get_accountant_dashboard(db)
        return render_template("accountant_dashboard.html", dash=dash, today=date.today().isoformat(),
                               currency=get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]))

    @app.route("/accounting/equity-statement")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_equity_statement():
        db = get_db()
        start = request.args.get("start", date(date.today().year, 1, 1).isoformat())
        end = request.args.get("end", date.today().isoformat())
        eq = get_equity_statement(db, start=start, end=end)
        return render_template("equity_statement.html", start=start, end=end, eq=eq,
                               currency=get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]))

    @app.route("/accounting/bank", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_bank():
        db = get_db()
        ensure_fiscal_tables(db)
        if request.method == "POST":
            try:
                save_bank_statement(db, request.form.get("statement_date", date.today().isoformat()).strip(),
                                    int(request.form.get("account_id", 0)),
                                    float(request.form.get("statement_balance") or 0),
                                    request.form.get("notes", "").strip(), session.get("user_id"))
                flash("تم حفظ كشف البنك.", "success")
            except Exception as exc:
                db.rollback()
                flash(f"تعذر الحفظ: {exc}", "danger")
            return redirect(url_for("accounting_bank"))
        end = request.args.get("end", date.today().isoformat())
        rows = get_bank_reconciliation(db, end=end)
        banks = db.execute(
            "SELECT id, code, name FROM account_nodes WHERE active=1 AND is_postable=1 AND (node_type='bank' OR code LIKE '112%') ORDER BY code"
        ).fetchall()
        balances = {b["id"]: get_account_closing_balance(db, b["id"], end=end) for b in banks}
        return render_template("bank.html", rows=rows, banks=banks, balances=balances, end=end,
                               today=date.today().isoformat(),
                               currency=get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]))

    @app.route("/accounting/auto-adjust", methods=["POST"])
    @login_required
    @role_required(["admin", "accountant", "manager"])
    def accounting_auto_adjust():
        db = get_db()
        kind = (request.form.get("kind") or "").strip()
        ref_id = request.form.get("ref_id", type=int)
        back = request.form.get("back") or url_for("accounting_reconciliation")
        try:
            entry_id, amount = auto_adjust_difference(db, kind, ref_id, session.get("user_id"))
            db.commit()
            flash(f"تم إنشاء قيد التسوية الآلية ({amount:.0f}) — قيد {entry_id}.", "success")
        except Exception as exc:
            db.rollback()
            flash(f"تعذر التسوية الآلية: {exc}", "danger")
        return redirect(back)

    @app.route("/accounting/templates", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "accountant", "manager", "technician"])
    def accounting_templates():
        db = get_db()
        ensure_template_tables(db)
        if request.method == "POST":
            action = request.form.get("action", "create")
            try:
                if action == "delete":
                    delete_template(db, int(request.form.get("template_id", 0)))
                    flash("تم حذف القالب.", "info")
                else:
                    accs = request.form.getlist("account_id")
                    debits = request.form.getlist("debit")
                    credits = request.form.getlist("credit")
                    descs = request.form.getlist("line_desc")
                    lines = []
                    for i in range(len(accs)):
                        try:
                            d = float(debits[i] or 0) if i < len(debits) else 0.0
                        except Exception:
                            d = 0.0
                        try:
                            c = float(credits[i] or 0) if i < len(credits) else 0.0
                        except Exception:
                            c = 0.0
                        lines.append({"account_id": accs[i], "debit": d, "credit": c,
                                      "description": descs[i] if i < len(descs) else ""})
                    create_template(db, request.form.get("name", ""), request.form.get("description", ""),
                                    lines, session.get("user_id"))
                    flash("تم إنشاء القالب.", "success")
            except Exception as exc:
                db.rollback()
                flash(f"تعذر حفظ القالب: {exc}", "danger")
            return redirect(url_for("accounting_templates"))
        accounts = db.execute("SELECT id, code, name FROM account_nodes WHERE active=1 AND is_postable=1 ORDER BY code").fetchall()
        templates = list_templates(db)
        return render_template("templates_list.html", accounts=accounts, templates=templates)

    @app.route("/accounting/templates/<int:template_id>", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "accountant", "manager", "technician"])
    def accounting_template_apply(template_id):
        db = get_db()
        ensure_template_tables(db)
        t = db.execute("SELECT * FROM journal_templates WHERE id=?", (template_id,)).fetchone()
        if not t:
            abort(404)
        lines = db.execute(
            """SELECT tl.*, a.code AS account_code, a.name AS account_name
               FROM journal_template_lines tl JOIN account_nodes a ON a.id=tl.account_id
               WHERE tl.template_id=? ORDER BY tl.id""", (template_id,)).fetchall()
        if request.method == "POST":
            try:
                amounts = {}
                for ln in lines:
                    amounts[str(ln["id"])] = {"debit": request.form.get(f"d_{ln['id']}", "0"),
                                              "credit": request.form.get(f"c_{ln['id']}", "0")}
                entry_id = apply_template(db, template_id,
                                          (request.form.get("entry_date") or date.today().isoformat()).strip(),
                                          amounts, session.get("user_id"),
                                          (request.form.get("description") or t["name"]).strip())
                db.commit()
                flash(f"تم ترحيل القالب (قيد {entry_id}).", "success")
                return redirect(url_for("accounting_journal"))
            except Exception as exc:
                db.rollback()
                flash(f"تعذر الترحيل: {exc}", "danger")
        return render_template("template_apply.html", t=t, lines=lines, today=date.today().isoformat())

    @app.route("/accounting/cost-centers")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_cost_centers():
        db = get_db()
        start = request.args.get("start", date(date.today().year, 1, 1).isoformat())
        end = request.args.get("end", date.today().isoformat())
        cc = get_village_profitability(db, start=start, end=end)
        return render_template("cost_centers.html", start=start, end=end, cc=cc,
                               currency=get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]))

    @app.route("/accounting/subscriber-ledger")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager", "staff", "collector"])
    def accounting_subscriber_ledger():
        db = get_db()
        subscriber_id = request.args.get("subscriber_id", type=int)
        start = request.args.get("start", date(date.today().year, 1, 1).isoformat())
        end = request.args.get("end", date.today().isoformat())
        subscribers_list = db.execute("SELECT id, name, account_number, account_node_id FROM subscribers ORDER BY name").fetchall()
        subscriber = None
        ledger = None
        if subscriber_id:
            subscriber = db.execute("SELECT * FROM subscribers WHERE id=?", (subscriber_id,)).fetchone()
            if subscriber and subscriber["account_node_id"]:
                ledger = get_ledger(db, subscriber["account_node_id"], start=start, end=end)
        return render_template("subscriber_ledger.html", subscribers=subscribers_list, subscriber=subscriber,
                               subscriber_id=subscriber_id, start=start, end=end, ledger=ledger,
                               currency=get_setting("currency_name", DEFAULT_SETTINGS["currency_name"]))

    def _excel_file_response(wb, filename):
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name=filename,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    def _excel_header(ws, title, start, end):
        ws["A1"] = get_setting("organization_name", DEFAULT_SETTINGS["organization_name"])
        ws["A2"] = title
        ws["A3"] = f"الفترة: {start} إلى {end}"

    @app.route("/accounting/ledger.xlsx")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_ledger_xlsx():
        db = get_db()
        start = request.args.get("start", "")
        end = request.args.get("end", "")
        account_id = request.args.get("account_id", type=int)
        ledger = get_ledger(db, account_id, start=start or None, end=end or None) if account_id else None
        if not ledger:
            abort(404)
        wb = Workbook()
        ws = wb.active
        ws.title = "دفتر الأستاذ"
        _excel_header(ws, f"دفتر الأستاذ: {ledger['account']['code']} - {ledger['account']['name']}", start, end)
        ws.append(["التاريخ", "رقم القيد", "البيان", "مدين", "دائن", "الرصيد"])
        ws.append(["", "", "رصيد افتتاحي", "", "", ledger["opening"]])
        for ln in ledger["lines"]:
            ws.append([ln["entry_date"], ln["entry_no"], ln["line_desc"] or ln["entry_desc"] or "",
                       ln["debit"], ln["credit"], ln["balance"]])
        ws.append(["", "", "الإجمالي", ledger["total_debit"], ledger["total_credit"], ledger["closing"]])
        return _excel_file_response(wb, f"ledger-{ledger['account']['code']}.xlsx")

    @app.route("/accounting/trial-balance.xlsx")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_trial_balance_xlsx():
        db = get_db()
        start = request.args.get("start", "")
        end = request.args.get("end", "")
        tb = get_trial_balance(db, start=start or None, end=end or None)
        wb = Workbook()
        ws = wb.active
        ws.title = "ميزان المراجعة"
        _excel_header(ws, "ميزان المراجعة", start, end)
        ws.append(["الرمز", "الحساب", "افتتاحي", "مدين", "دائن", "ختامي"])
        for r in tb["rows"]:
            ws.append([r["code"], r["name"], r["opening"], r["debit"], r["credit"], r["closing"]])
        t = tb["totals"]
        ws.append(["", "الإجمالي", t["opening"], t["debit"], t["credit"], t["closing"]])
        return _excel_file_response(wb, "trial-balance.xlsx")

    @app.route("/accounting/income-statement.xlsx")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_income_statement_xlsx():
        db = get_db()
        start = request.args.get("start", "")
        end = request.args.get("end", "")
        income = get_income_statement(db, start=start or None, end=end or None)
        wb = Workbook()
        ws = wb.active
        ws.title = "قائمة الدخل"
        _excel_header(ws, "قائمة الدخل", start, end)
        ws.append(["البند", "الرمز", "المبلغ"])
        for r in income["revenues"]:
            ws.append([r["name"], r["code"], r["amount"]])
        ws.append(["إجمالي الإيرادات", "", income["revenue_total"]])
        for r in income["expenses"]:
            ws.append([r["name"], r["code"], -r["amount"]])
        ws.append(["إجمالي المصروفات", "", -income["expense_total"]])
        ws.append(["صافي الربح", "", income["net_income"]])
        return _excel_file_response(wb, "income-statement.xlsx")

    @app.route("/accounting/balance-sheet.xlsx")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_balance_sheet_xlsx():
        db = get_db()
        start = request.args.get("start", "")
        end = request.args.get("end", "")
        bs = get_balance_sheet(db, start=start or None, end=end or None)
        wb = Workbook()
        ws = wb.active
        ws.title = "الميزانية"
        _excel_header(ws, "الميزانية العمومية", start, end)
        ws.append(["القسم", "الرمز", "الحساب", "المبلغ"])
        for r in bs["assets"]:
            ws.append(["الأصول", r["code"], r["name"], r["amount"]])
        ws.append(["إجمالي الأصول", "", "", bs["assets_total"]])
        for r in bs["liabilities"]:
            ws.append(["الخصوم", r["code"], r["name"], r["amount"]])
        for r in bs["equity"]:
            ws.append(["الحقوق", r["code"], r["name"], r["amount"]])
        ws.append(["صافي ربح الفترة", "", "", bs["net_income"]])
        ws.append(["إجمالي الخصوم والحقوق", "", "", bs["equity_liab_total"]])
        return _excel_file_response(wb, "balance-sheet.xlsx")

    @app.route("/accounting/cash-flow.xlsx")
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def accounting_cash_flow_xlsx():
        db = get_db()
        start = request.args.get("start", "")
        end = request.args.get("end", "")
        cf = get_cash_flow(db, start=start or None, end=end or None)
        wb = Workbook()
        ws = wb.active
        ws.title = "التدفقات"
        _excel_header(ws, "التدفقات النقدية", start, end)
        ws.append(["البند", "المبلغ"])
        ws.append(["تحصيل الفواتير", cf["collections"]])
        ws.append(["سندات القبض", cf["receipts"]])
        ws.append(["سندات الصرف", -cf["payments_out"]])
        ws.append(["رصيد أول الفترة", cf["cash_opening"]])
        ws.append(["رصيد آخر الفترة", cf["cash_closing"]])
        return _excel_file_response(wb, "cash-flow.xlsx")
    @app.route("/customer-statement")
    @login_required
    def customer_statement():
        db = get_db()
        subscriber_id = request.args.get("subscriber_id", type=int)
        start = request.args.get("start", date(date.today().year, date.today().month, 1).isoformat())
        end = request.args.get("end", date.today().isoformat())
        q = request.args.get("q", "").strip()
        subscribers_list = db.execute("SELECT id, name, account_number, phone FROM subscribers ORDER BY name").fetchall()
        payload = build_reports_payload(db, subscriber_id=subscriber_id, start=start, end=end, q=q) if subscriber_id else None
        return render_template(
            "customer_statement.html",
            subscribers=subscribers_list,
            request=request,
            start=start,
            end=end,
            q=q,
            payload=payload,
        )

    @app.route("/customer-statement/pdf")
    @login_required
    def customer_statement_pdf():
        db = get_db()
        subscriber_id = request.args.get("subscriber_id", type=int)
        start = request.args.get("start", date(date.today().year, date.today().month, 1).isoformat())
        end = request.args.get("end", date.today().isoformat())
        if not subscriber_id:
            return "يجب اختيار عميل", 400
        payload = build_reports_payload(db, subscriber_id=subscriber_id, start=start, end=end)
        sub = payload["subscriber"]
        invoices = list(payload["invoices"])
        payments = list(payload["payments"])
        rows = []
        for inv in invoices:
            rows.append({
                "date": inv["invoice_date"] or "",
                "desc": f"فاتورة رقم {inv['invoice_no']}" + (f" — استهلاك {inv['consumption']}" if inv["consumption"] else ""),
                "debit": float(inv["total_amount"] or 0),
                "credit": 0,
                "notes": "",
                "sort_key": inv["invoice_date"] or "9999",
                "sort_type": 0,
            })
        for pay in payments:
            sign = -1 if (pay["amount"] or 0) < 0 else 1
            rows.append({
                "date": pay["payment_date"] or "",
                "desc": "تسديد" + (f" فاتورة {pay['invoice_no']}" if pay.get("invoice_no") else "") + (" (عكس)" if pay.get("is_reversal") else ""),
                "debit": 0,
                "credit": abs(float(pay["amount"] or 0)),
                "notes": (pay["method"] or "") + (f" — {pay['notes']}" if pay.get("notes") else ""),
                "sort_key": pay["payment_date"] or "9999",
                "sort_type": 1,
            })
        rows.sort(key=lambda r: (r["sort_key"], r["sort_type"]))
        balance = 0
        for r in rows:
            balance += r["debit"] - r["credit"]
            r["balance"] = balance
        total_debit = sum(r["debit"] for r in rows)
        total_credit = sum(r["credit"] for r in rows)
        remaining = payload["summary"]["remaining_total"]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        printed_by = g.user.get("username", "—") if g.user else "—"
        currency = get_setting("currency_name", DEFAULT_SETTINGS["currency_name"])
        try:
            pdf = _render_pdf_bytes(
                "customer_statement_print.html",
                subscriber=sub,
                rows=rows,
                total_debit=total_debit,
                total_credit=total_credit,
                remaining=remaining,
                start=start,
                end=end,
                now=now_str,
                printed_by=printed_by,
                currency=currency,
            )
        except Exception as exc:
            return f"تعذر توليد كشف الحساب: {exc}", 500
        fname = f"كشف_حساب_{sub['name']}.pdf"
        return _send_pdf(pdf, fname)

    @app.route("/api/v1/accounting/journal-entries/", methods=["GET", "POST"])
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def api_journal_entries():
        db = get_db()
        if request.method == "GET":
            start = request.args.get("start")
            end = request.args.get("end")
            reference = request.args.get("reference") or request.args.get("q")
            limit = request.args.get("limit", type=int) or 200
            rows = list_journal_entries(db, start=start, end=end, reference=reference, limit=limit)
            return jsonify({"results": [_journal_json(r) for r in rows]})

        payload = request.get_json(silent=True) or request.form
        try:
            lines_raw = payload.get("lines") or payload.get("lines_write") or []
            if isinstance(lines_raw, str):
                import json as _json
                lines_raw = _json.loads(lines_raw or "[]")
            lines = []
            for line in lines_raw:
                account_id = int(line.get("account") or line.get("account_id") or 0)
                if not account_id:
                    return jsonify({"error": "account_required"}), 400
                account = db.execute(
                    "SELECT id, is_postable, active FROM account_nodes WHERE id=? AND active=1",
                    (account_id,),
                ).fetchone()
                if not account or not account["is_postable"]:
                    return jsonify({"error": "select_postable_account_only"}), 400
                lines.append({
                    "account_id": account_id,
                    "debit": float(line.get("debit") or 0),
                    "credit": float(line.get("credit") or 0),
                    "description": (line.get("description") or "").strip(),
                })
            _new_date = (payload.get("date") or payload.get("entry_date") or date.today().isoformat()).strip()
            if is_date_in_closed_year(db, _new_date):
                return jsonify({"error": "السنة المالية مقفلة لهذا التاريخ."}), 400
            entry_id = create_manual_journal_entry(
                db,
                entry_date=_new_date,
                description=(payload.get("description") or "").strip(),
                lines=lines,
                created_by=g.user["id"],
                source_type="manual",
                source_id=None,
                is_posted=str(payload.get("is_posted", "true")).lower() not in ("false", "0", "no"),
            )
            db.commit()
            return jsonify({"ok": True, "id": entry_id}), 201
        except Exception as exc:
            db.rollback()
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/v1/accounting/journal-entries/<int:entry_id>/", methods=["GET", "PATCH", "DELETE"])
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def api_journal_entry_detail(entry_id):
        db = get_db()
        item = get_journal_entry_by_id(db, entry_id)
        if not item:
            return jsonify({"error": "not_found"}), 404
        if request.method == "GET":
            return jsonify(_journal_json(item))

        if request.method == "DELETE":
            try:
                if is_date_in_closed_year(db, item["entry"]["entry_date"]):
                    return jsonify({"error": "السنة المالية مقفلة لهذا القيد."}), 400
                delete_manual_journal_entry(db, entry_id)
                db.commit()
                return jsonify({"ok": True})
            except Exception as exc:
                db.rollback()
                return jsonify({"error": str(exc)}), 400

        payload = request.get_json(silent=True) or request.form
        try:
            lines_raw = payload.get("lines") or payload.get("lines_write") or []
            if isinstance(lines_raw, str):
                import json as _json
                lines_raw = _json.loads(lines_raw or "[]")
            lines = []
            for line in lines_raw:
                account_id = int(line.get("account") or line.get("account_id") or 0)
                account = db.execute(
                    "SELECT id, is_postable, active FROM account_nodes WHERE id=? AND active=1",
                    (account_id,),
                ).fetchone()
                if not account or not account["is_postable"]:
                    return jsonify({"error": "select_postable_account_only"}), 400
                lines.append({
                    "account_id": account_id,
                    "debit": float(line.get("debit") or 0),
                    "credit": float(line.get("credit") or 0),
                    "description": (line.get("description") or "").strip(),
                })
            _upd_date = (payload.get("date") or payload.get("entry_date") or item["entry"]["entry_date"]).strip()
            if is_date_in_closed_year(db, item["entry"]["entry_date"]) or is_date_in_closed_year(db, _upd_date):
                return jsonify({"error": "السنة المالية مقفلة لهذا القيد."}), 400
            update_manual_journal_entry(
                db,
                entry_id,
                entry_date=_upd_date,
                description=(payload.get("description") or item["entry"]["description"] or "").strip(),
                lines=lines,
            )
            db.commit()
            return jsonify(_journal_json(get_journal_entry_by_id(db, entry_id)))
        except Exception as exc:
            db.rollback()
            return jsonify({"error": str(exc)}), 400

    @app.route("/accounting/normalize-parent-balances", methods=["POST"])
    @login_required
    @role_required(["admin", "technician", "accountant", "manager"])
    def normalize_parent_balances():
        db = get_db()
        issues = find_main_account_direct_balances(db)
        if not issues:
            flash("لا توجد أرصدة مباشرة على الحسابات الرئيسية.", "info")
            return redirect(url_for("accounting_reports"))

        opening_code = "999900"
        opening = db.execute("SELECT id FROM account_nodes WHERE code=? AND active=1", (opening_code,)).fetchone()
        if opening:
            opening_account_id = opening["id"]
        else:
            opening_account_id = add_account_node(
                db,
                opening_code,
                "أرصدة افتتاحية",
                node_type="equity",
                parent_id=None,
                is_postable=1,
            )

        lines = []
        total_net = 0.0
        for row in issues:
            net = float(row["debit"] or 0) - float(row["credit"] or 0)
            if abs(net) < 0.0001:
                continue
            total_net += net
            if net > 0:
                lines.append({
                    "account_id": row["id"],
                    "debit": 0.0,
                    "credit": abs(net),
                    "description": "ترحيل رصيد سابق من الحساب الرئيسي",
                })
            else:
                lines.append({
                    "account_id": row["id"],
                    "debit": abs(net),
                    "credit": 0.0,
                    "description": "ترحيل رصيد سابق إلى الحساب الافتتاحي",
                })

        if abs(total_net) > 0.0001:
            if total_net > 0:
                lines.append({
                    "account_id": opening_account_id,
                    "debit": 0.0,
                    "credit": abs(total_net),
                    "description": "مقابل ترحيل الأرصدة الرئيسية",
                })
            else:
                lines.append({
                    "account_id": opening_account_id,
                    "debit": abs(total_net),
                    "credit": 0.0,
                    "description": "مقابل ترحيل الأرصدة الرئيسية",
                })

        create_manual_journal_entry(
            db,
            entry_date=date.today().isoformat(),
            description="ترحيل الأرصدة السابقة للحسابات الرئيسية",
            lines=lines,
            created_by=g.user["id"],
            source_type="opening_balance",
            source_id=None,
            is_posted=True,
        )
        db.commit()
        flash("تم ترحيل الأرصدة السابقة إلى قيد افتتاحي وحساب أرصدة افتتاحية.", "success")
        return redirect(url_for("accounting_reports"))

    @app.errorhandler(404)

    def not_found(_):
        return render_template("base_error.html", title="الصفحة غير موجودة", message="تعذر العثور على الصفحة."), 404

    return app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
