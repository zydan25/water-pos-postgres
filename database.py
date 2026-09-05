import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from flask import g
from sqlalchemy import delete, func, inspect, select, text as sa_text
from models import (
    Base,
    engine,
    SessionLocal,
    Settings,
    Subscriber,
    Invoice,
    Payment,
    Wallet,
    Transaction,
    AccountNode,
    EmployeeProfile,
    JournalEntry,
    JournalLine,
    Attachment,
    User,
    UserWallet,
    AccountingVoucher,
    CollectionTrip,
    TripVisit,
    BulkReading,
    AuditLog,
)

# إعداد المسارات الأساسية لقاعدة البيانات
BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
DB_PATH = INSTANCE_DIR / "water_billing.sqlite3"
# مسار قاعدة البيانات
database_file_path = str(DB_PATH)

# الإعدادات الافتراضية للنظام
DEFAULT_SETTINGS = {
    "project_name": "نظام فواتير المياه",
    "organization_name": "جمعية مياه",
    "engineer_name": "يمن كود للتقنيات الذكية",
    "default_unit_price": "3500",
    "default_subscription_fee": "500",
    "currency_name": "ريال",
    "whatsapp_api_url": "https://alattab.site:3000/water/api/send",
    "whatsapp_from_number": "",
    "invoice_start_no": "1",
    "invoice_rows_per_pdf": "4",
    "enforce_one_invoice_per_month": "1",
    "manual_previous_arrears_entry": "1",
    "ui_theme": "light",
}


class _CompatRow:
    """صف متوافق مع sqlite3.Row يدعم الوصول بالنص وبالفهرس."""
    __slots__ = ("_keys", "_values", "_mapping")

    def __init__(self, row):
        mapping = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
        self._keys = list(mapping.keys())
        self._values = [mapping[k] for k in self._keys]
        self._mapping = mapping

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def get(self, key, default=None):
        return self._mapping.get(key, default)

    def keys(self):
        return list(self._keys)

    def items(self):
        return self._mapping.items()

    def values(self):
        return list(self._values)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __contains__(self, key):
        return key in self._mapping

    def __getattr__(self, name):
        try:
            return self._mapping[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __repr__(self):
        return f"_CompatRow({self._mapping!r})"


class _CompatResult:
    """غلاف نتيجة SQLAlchemy يحاكي fetchone/fetchall/lastrowid."""
    __slots__ = ("_rows", "rowcount", "lastrowid", "_index")

    def __init__(self, result):
        self._rows = []
        self._index = 0
        self.rowcount = getattr(result, "rowcount", -1)
        self.lastrowid = getattr(result, "lastrowid", None)
        try:
            rows = result.mappings().all()
        except Exception:
            try:
                rows = result.fetchall()
            except Exception:
                rows = []
        for row in rows:
            self._rows.append(_CompatRow(row))
        # بعض أوامر INSERT/UPDATE/DDL لا تعيد صفوفاً؛ نتعامل معها بهدوء.

    def fetchone(self):
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self):
        if self._index >= len(self._rows):
            return []
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows

    def first(self):
        return self.fetchone()

    def scalar(self):
        row = self.fetchone()
        if row is None:
            return None
        return row[0]

    def __iter__(self):
        return iter(self.fetchall())


class SQLAlchemyDBProxy:
    """واجهة اتصال تشبه sqlite3 لكنها تعتمد SQLAlchemy Session تحت الغطاء."""

    def __init__(self, session):
        self.session = session

    def _connection(self):
        # نطلب اتصالاً طازجاً من الجلسة كل مرة لتجنب ResourceClosedError
        # بعد commit/rollback أو انتهاء transaction السابقة.
        return self.session.connection()

    def execute(self, sql, params=None):
        conn = self._connection()
        try:
            if params is None:
                result = conn.exec_driver_sql(sql)
            else:
                if isinstance(params, (list, tuple)):
                    params = tuple(params)
                result = conn.exec_driver_sql(sql, params)
        except Exception:
            # بعض الاستعلامات النصية استخدم named params فلا تُنفذ عبر exec_driver_sql؛
            # نعيد تنفيذها عبر text() لتمريرها بشكل صحيح. أما غيرها فنبقي الخطأ الأصلي ظاهراً
            # بدل كسره برسالة بارامترات مضللة (مثل IntegrityError من triggers قاعدة البيانات).
            if isinstance(params, dict):
                try:
                    result = self.session.execute(sa_text(sql), params)
                except Exception:
                    raise
            else:
                raise
        return _CompatResult(result)

    def executemany(self, sql, seq_of_params):
        last = None
        for params in seq_of_params:
            last = self.execute(sql, params)
        return last or _CompatResult(self.session.execute(sa_text("SELECT 1 WHERE 0")))

    def executescript(self, script):
        """توافق sqlite3.executescript باستخدام اتصال SQLite الموجود تحت SQLAlchemy."""
        raw = self.session.connection().connection
        raw.executescript(script)
        self.session.expire_all()
        return None

    def commit(self):
        self.session.commit()

    def rollback(self):
        self.session.rollback()

    def close(self):
        try:
            self.session.close()
        finally:
            try:
                SessionLocal.remove()
            except Exception:
                pass

    def __getattr__(self, name):
        # للتوافق مع أي استدعاءات عرضية أخرى.
        return getattr(self.session, name)


def get_db():
    """إدارة وفتح اتصال قاعدة البيانات لكل طلب ممرر عبر Flask g."""
    if "db" not in g:
        INSTANCE_DIR.mkdir(exist_ok=True)
        g.db = SQLAlchemyDBProxy(SessionLocal())
        try:
            g.db.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass
    return g.db

    if "db" not in g:
        INSTANCE_DIR.mkdir(exist_ok=True)
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
        # تفعيل دعم المفاتيح الأجنبية لضمان سلامة العلاقات والعزل المالي
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@contextmanager
def orm_session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _schema_sql_type(column):
    try:
        return column.type.compile(dialect=engine.dialect)
    except Exception:
        python_type = getattr(column.type, "python_type", None)
        if python_type is int:
            return "INTEGER"
        if python_type is float:
            return "REAL"
        return "TEXT"


def sync_sqlalchemy_schema(db=None):
    """
    مزامنة مخطط SQLAlchemy مع قاعدة SQLite الحالية بإضافة الأعمدة الناقصة فقط.
    لا يحذف أي بيانات ولا يغير أسماء الجداول القديمة.
    """
    conn = db or get_db()
    existing_tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            continue
        current_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
        for column in table.columns:
            if column.name in current_cols:
                continue
            sql_type = _schema_sql_type(column)
            parts = [f"{column.name} {sql_type}"]
            if not column.nullable and not column.primary_key:
                parts.append("NOT NULL")
            try:
                default = column.default.arg if column.default is not None else None
                if callable(default):
                    default = default()
            except Exception:
                default = None
            if default is not None and not column.primary_key:
                if isinstance(default, str):
                    parts.append(f"DEFAULT '{default}'")
                else:
                    parts.append(f"DEFAULT {default}")
            try:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {' '.join(parts)}")
            except Exception:
                pass
    try:
        conn.commit()
    except Exception:
        pass


def _with_orm_session(fn):
    def wrapper(*args, **kwargs):
        with orm_session_scope() as session:
            return fn(session, *args, **kwargs)
    return wrapper


def _ensure_table_column(db, table_name, column_name, column_def):
    """إضافة عمود مفقود بطريقة آمنة لملف قاعدة بيانات قديم."""
    cols = db.execute(f"PRAGMA table_info({table_name})").fetchall()
    if any((c[1] if not isinstance(c, dict) else c['name']) == column_name for c in cols):
        return False
    db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
    return True

def init_db(app):
    """تهيئة قاعدة البيانات وبناء الجداول الأساسية عند إقلاع التطبيق"""
    with app.app_context():
        db = get_db()
        # جعل SQLAlchemy هو المصدر البنيوي الأساسي للجداول الحديثة
        Base.metadata.create_all(bind=engine)
        sync_sqlalchemy_schema(db)

        # 1. جدول المستخدمين المحدث لدعم الموظفين والصلاحيات والربط بالخزن
        db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'Collector', -- (Admin, Staff, Collector, Technician)
            wallet_id INTEGER, -- ربط الموظف/المحصل بصندوق مالي محدد
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            FOREIGN KEY(wallet_id) REFERENCES wallets(id) ON DELETE SET NULL
        )""")
        
        # 2. جدول المشتركين
        db.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_number TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            meter_number TEXT,
            phone TEXT,
            village TEXT,
            address TEXT,
            default_unit_price TEXT,
            default_subscription_fee TEXT,
            last_reading REAL,
            last_due_amount REAL,
            last_paid_amount REAL,
            active INTEGER DEFAULT 1,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        )""")

        # 3. جدول الفواتير
        db.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_no INTEGER UNIQUE NOT NULL,
            subscriber_id INTEGER,
            invoice_date TEXT,
            month_label TEXT,
            previous_reading REAL,
            current_reading REAL,
            consumption REAL,
            unit_price REAL,
            consumption_amount REAL,
            subscription_fee REAL,
            other_charges REAL,
            opening_balance REAL,
            total_amount REAL,
            paid_amount REAL,
            remaining_amount REAL,
            notes TEXT,
            created_by INTEGER,
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY(subscriber_id) REFERENCES subscribers(id)
        )""")

        # 3.1 جدول القراءات الجماعية المؤقتة
        _ensure_table_column(db, "invoices", "credit_amount", "REAL DEFAULT 0")

        db.execute("""
        CREATE TABLE IF NOT EXISTS bulk_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscriber_id INTEGER NOT NULL,
            previous_reading REAL DEFAULT 0,
            current_reading REAL NOT NULL,
            reading_date TEXT,
            month_label TEXT,
            invoiced INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY(subscriber_id) REFERENCES subscribers(id)
        )""")

        # 4. جدول السندات والمدفوعات
        db.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER,
            subscriber_id INTEGER,
            amount REAL,
            payment_date TEXT,
            method TEXT,
            notes TEXT,
            attachment_path TEXT,
            created_by INTEGER,
            created_at TEXT,
            FOREIGN KEY(invoice_id) REFERENCES invoices(id),
            FOREIGN KEY(subscriber_id) REFERENCES subscribers(id)
        )""")

        # 5. جدول الإعدادات العامة (مفتاح وقيمة)
        db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")

        # 6. جدول المرفقات للفواتير
        db.execute("""
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER,
            filename TEXT,
            path TEXT,
            mime_type TEXT,
            uploaded_at TEXT,
            FOREIGN KEY(invoice_id) REFERENCES invoices(id)
        )""")

        # 7. جدول الصناديق/الحسابات المالية (Wallets)
        db.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            balance REAL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")

        # 8. جدول ربط الموظفين بالصناديق (User Wallets)
        db.execute("""
        CREATE TABLE IF NOT EXISTS user_wallets (
            user_id INTEGER,
            wallet_id INTEGER,
            PRIMARY KEY(user_id, wallet_id),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(wallet_id) REFERENCES wallets(id) ON DELETE CASCADE
        )""")

        # 9. جدول العمليات المالية لحركة الصناديق (Transactions)
        db.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL, -- 'IN' للتحصيل والإيداع، 'OUT' للمصروفات والصرف
            amount REAL NOT NULL,
            wallet_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            subscriber_id INTEGER,
            reference_id INTEGER, -- يربط برقم السند (payment_id) إن وجد
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(wallet_id) REFERENCES wallets(id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(subscriber_id) REFERENCES subscribers(id)
        )""")

        # 10. جدول سجل التدقيق والمراقبة لأمان النظام (Audit Log)
        db.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL, -- INSERT, UPDATE, DELETE, EXPENSE
            table_name TEXT NOT NULL,
            row_id INTEGER,
            description TEXT,
            timestamp TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )""")


        # 10. شجرة الحسابات والقيود اليومية والملفات المحاسبية
        db.execute("""
        CREATE TABLE IF NOT EXISTS account_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            node_type TEXT NOT NULL DEFAULT 'account',
            parent_id INTEGER,
            is_postable INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            FOREIGN KEY(parent_id) REFERENCES account_nodes(id) ON DELETE SET NULL
        )""")
        db.execute("""
        CREATE TABLE IF NOT EXISTS employee_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            account_node_id INTEGER,
            full_name TEXT,
            job_title TEXT,
            monthly_target REAL DEFAULT 0,
            notes TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(account_node_id) REFERENCES account_nodes(id) ON DELETE SET NULL
        )""")
        db.execute("""
        CREATE TABLE IF NOT EXISTS journal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_no TEXT UNIQUE NOT NULL,
            entry_date TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id INTEGER,
            description TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
        )""")
        db.execute("""
        CREATE TABLE IF NOT EXISTS journal_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            debit REAL NOT NULL DEFAULT 0,
            credit REAL NOT NULL DEFAULT 0,
            description TEXT,
            FOREIGN KEY(entry_id) REFERENCES journal_entries(id) ON DELETE CASCADE,
            FOREIGN KEY(account_id) REFERENCES account_nodes(id) ON DELETE CASCADE
        )""")


        db.execute("""
        CREATE TABLE IF NOT EXISTS accounting_vouchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_no TEXT UNIQUE NOT NULL,
            voucher_type TEXT NOT NULL,
            voucher_date TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            from_account_id INTEGER NOT NULL,
            to_account_id INTEGER NOT NULL,
            reference TEXT,
            description TEXT,
            source_type TEXT NOT NULL DEFAULT 'manual',
            source_id INTEGER,
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'posted',
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            FOREIGN KEY(from_account_id) REFERENCES account_nodes(id) ON DELETE RESTRICT,
            FOREIGN KEY(to_account_id) REFERENCES account_nodes(id) ON DELETE RESTRICT
        )""")

        # ── جولات التحصيل اليومية ──
        db.execute("""
        CREATE TABLE IF NOT EXISTS collection_trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collector_id INTEGER NOT NULL,
            trip_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',  -- open | closed
            target_amount REAL DEFAULT 0,
            collected_amount REAL DEFAULT 0,
            notes TEXT,
            started_at TEXT,
            closed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(collector_id) REFERENCES users(id)
        )""")
        db.execute("""
        CREATE TABLE IF NOT EXISTS trip_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            invoice_id INTEGER NOT NULL,
            subscriber_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending | collected | absent | refused | deferred
            amount_collected REAL DEFAULT 0,
            notes TEXT,
            visited_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(trip_id) REFERENCES collection_trips(id) ON DELETE CASCADE,
            FOREIGN KEY(invoice_id) REFERENCES invoices(id),
            FOREIGN KEY(subscriber_id) REFERENCES subscribers(id)
        )""")

        # زرع صندوق مالي رئيسي افتراضي إذا كانت الجداول جديدة تماماً
        default_wallet = db.execute("SELECT id FROM wallets WHERE id = 1").fetchone()
        if not default_wallet:
            db.execute("INSERT INTO wallets (id, name, balance, created_at) VALUES (1, 'صندوق ترحيل قديم', 0.0, ?)", (datetime.now().isoformat(),))

        # زرع المستخدم المسؤول الافتراضي لتجنب فشل تسجيل الدخول
        from werkzeug.security import generate_password_hash
        if not db.execute("SELECT 1 FROM users WHERE username=?", ("zydan",)).fetchone():
            db.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                ("zydan", generate_password_hash("774952665"), "Admin", datetime.now().isoformat()),
            )

        # تشغيل التحديثات التلقائية للبنية القديمة لتجنب كراش الحقول المفقودة
        migrate_db_schema(db)
        ensure_accounting_seed(db)
        apply_financial_constraints(db)
        db.commit()

def ensure_column(db, table, column, ddl):
    """التحقق من وجود العمود وإضافته ديناميكياً لتفادي أخطاء التحديثات"""
    existing = db.execute(f"PRAGMA table_info({table})").fetchall()
    if column not in {row[1] for row in existing}:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

def migrate_db_schema(db):
    """تحديث بنية الجداول القديمة لتتوافق مع الميزات الحالية والمستقبلية للطباعة والإرسال والصناديق"""
    sync_sqlalchemy_schema(db)
    # ترقيات جدول الفواتير والسندات السابقة
    ensure_column(db, "invoices", "printed_by", "printed_by INTEGER")
    ensure_column(db, "invoices", "printed_at", "printed_at TEXT")
    ensure_column(db, "invoices", "sent_by", "sent_by INTEGER")
    ensure_column(db, "invoices", "sent_at", "sent_at TEXT")
    ensure_column(db, "invoices", "sent_count", "sent_count INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "invoices", "last_sent_channel", "last_sent_channel TEXT")
    ensure_column(db, "invoices", "journal_entry_id", "journal_entry_id INTEGER")
    ensure_column(db, "payments", "created_by", "created_by INTEGER")
    ensure_column(db, "payments", "created_at", "created_at TEXT")
    ensure_column(db, "subscribers", "account_node_id", "account_node_id INTEGER")
    ensure_column(db, "subscribers", "village", "village TEXT")
    ensure_column(db, "subscribers", "last_reading", "last_reading REAL")
    ensure_column(db, "subscribers", "last_due_amount", "last_due_amount REAL")
    ensure_column(db, "subscribers", "last_paid_amount", "last_paid_amount REAL")
    

    # ترقية جدول السندات المحاسبية
    if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='accounting_vouchers'").fetchone():
        ensure_column(db, "accounting_vouchers", "source_type", "source_type TEXT NOT NULL DEFAULT 'manual'")
        ensure_column(db, "accounting_vouchers", "source_id", "source_id INTEGER")
        ensure_column(db, "accounting_vouchers", "journal_entry_id", "journal_entry_id INTEGER")
        ensure_column(db, "accounting_vouchers", "status", "status TEXT NOT NULL DEFAULT 'posted'")
        ensure_column(db, "accounting_vouchers", "updated_at", "updated_at TEXT")

    # ترقية جدول المستخدمين لضمان عدم وجود أخطاء إذا كانت قاعدة البيانات منشأة مسبقاً بدون الحقول الجديدة
    ensure_column(db, "users", "role", "role TEXT NOT NULL DEFAULT 'Collector'")
    ensure_column(db, "users", "wallet_id", "wallet_id INTEGER")
    ensure_column(db, "users", "active", "active INTEGER DEFAULT 1")
    ensure_column(db, "users", "updated_at", "updated_at TEXT")

    # إعادة تسمية الصندوق الافتراضي القديم لتقليل الالتباس، مع إبقائه لأغراض التوافق فقط
    if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wallets'").fetchone():
        db.execute("UPDATE wallets SET name='صندوق ترحيل قديم' WHERE id=1 AND name LIKE '%%الصندوق الرئيسي%%'")


def ensure_accounting_seed(db):
    """زرع شجرة الحسابات الأساسية والفروع المحاسبية عند أول تشغيل.

    تم حذف حساب "الصندوق الرئيسي" كحساب ظاهر، والاكتفاء بحسابات تفصيلية
    داخل فرع الصناديق لتكون كل الحركات مرتبطة بشجرة الحسابات.
    """
    if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='account_nodes'").fetchone():
        return
    now = datetime.now().isoformat()
    seeds = [
        ("1000", "الأصول", "root", None, 0),
        ("1100", "الصناديق", "cash", "1000", 0),
        ("1111", "صندوق الإدارة", "cash", "1100", 1),
        ("1112", "صندوق التحصيل", "cash", "1100", 1),
        ("1200", "العملاء", "receivable", "1000", 0),
        ("1210", "حسابات العملاء", "receivable", "1200", 1),
        ("1300", "الموظفون", "employee", "1000", 0),
        ("1310", "حسابات الموظفين", "employee", "1300", 1),
        ("2000", "الخصوم", "root", None, 0),
        ("2100", "الموردون", "payable", "2000", 0),
        ("2110", "حسابات الموردين", "payable", "2100", 1),
        ("3000", "حقوق الملكية", "root", None, 0),
        ("4000", "الإيرادات", "root", None, 0),
        ("4100", "إيرادات المياه", "income", "4000", 1),
        ("4200", "إيرادات أخرى", "income", "4000", 1),
        ("5000", "المصروفات", "root", None, 0),
        ("5100", "مصروفات تشغيل", "expense", "5000", 1),
        ("5200", "مصروفات إدارية", "expense", "5000", 1),
    ]
    for code, name, node_type, parent_ref, postable in seeds:
        if db.execute("SELECT 1 FROM account_nodes WHERE code=?", (code,)).fetchone():
            continue
        parent_id = None
        if parent_ref:
            parent = db.execute("SELECT id FROM account_nodes WHERE code=?", (parent_ref,)).fetchone()
            parent_id = parent["id"] if parent else None
        db.execute(
            "INSERT INTO account_nodes (code, name, node_type, parent_id, is_postable, active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (code, name, node_type, parent_id, postable, now),
        )



def ensure_employee_profile(db, user_id, full_name=None, job_title=None, account_node_id=None):
    now = datetime.now().isoformat()
    row = db.execute("SELECT id FROM employee_profiles WHERE user_id=?", (user_id,)).fetchone()
    if row:
        db.execute(
            "UPDATE employee_profiles SET full_name=COALESCE(?, full_name), job_title=COALESCE(?, job_title), account_node_id=COALESCE(?, account_node_id), updated_at=? WHERE user_id=?",
            (full_name, job_title, account_node_id, now, user_id),
        )
        return row["id"]
    db.execute(
        "INSERT INTO employee_profiles (user_id, account_node_id, full_name, job_title, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, account_node_id, full_name, job_title, now, now),
    )
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def create_employee_account(db, user_id, full_name=None, job_title="Collector"):
    profile = db.execute("SELECT * FROM employee_profiles WHERE user_id=?", (user_id,)).fetchone()
    if profile and profile["account_node_id"]:
        return profile["account_node_id"]
    branch = db.execute("SELECT id FROM account_nodes WHERE code='1310' AND active=1 LIMIT 1").fetchone()
    if not branch:
        branch = db.execute("SELECT id FROM account_nodes WHERE code='1300' AND active=1 LIMIT 1").fetchone()
    parent_id = branch["id"] if branch else None
    code = f"EMP-{user_id:05d}"
    name = full_name or f"موظف {user_id}"
    existing = db.execute("SELECT id FROM account_nodes WHERE code=?", (code,)).fetchone()
    if existing:
        account_id = existing["id"]
    else:
        account_id = add_account_node(db, code, name, node_type="employee", parent_id=parent_id, is_postable=1)
    ensure_employee_profile(db, user_id, full_name=name, job_title=job_title, account_node_id=account_id)
    return account_id


def add_account_node(db, code, name, node_type="account", parent_id=None, is_postable=1):
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO account_nodes (code, name, node_type, parent_id, is_postable, active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
        (code, name, node_type, parent_id, is_postable, now),
    )
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def get_account_tree(db):
    rows = db.execute("SELECT * FROM account_nodes WHERE active=1 ORDER BY code").fetchall()
    by_parent = {}
    for row in rows:
        by_parent.setdefault(row["parent_id"], []).append(row)
    for items in by_parent.values():
        items.sort(key=lambda r: r["code"])
    def build(parent_id=None):
        return [
            {"node": row, "children": build(row["id"])}
            for row in by_parent.get(parent_id, [])
        ]
    return build(None)


def post_journal_entry(db, entry_date, source_type, source_id, description, created_by, lines):
    with orm_session_scope() as session:
        entry = JournalEntry(entry_no="JE-" + datetime.now().strftime('%Y%m%d%H%M%S%f'), entry_date=entry_date, source_type=source_type, source_id=source_id, description=description, created_by=created_by, created_at=datetime.now().isoformat())
        session.add(entry)
        session.flush()
        for account_id, debit, credit, line_desc in lines:
            session.add(JournalLine(entry_id=entry.id, account_id=account_id, debit=float(debit or 0), credit=float(credit or 0), description=line_desc))
        return entry.id

def get_account_balance_rows(db, start=None, end=None):
    """إرجاع شجرة الحسابات مع إجمالي المدين/الدائن/الرصيد لكل حساب.

    مبدأ العرض المحاسبي هنا:
    - الحسابات الرئيسية/غير القابلة للتفصيل لا نُظهر لها رصيدًا مباشرًا من قيودها الخاصة.
    - الرصيد الظاهر لكل حساب رئيسي يأتي من تجميع فروعه فقط.
    - الحسابات التفصيلية القابلة للاختيار تحتفظ برصيدها المباشر.
    """
    accounts = db.execute("SELECT * FROM account_nodes WHERE active=1 ORDER BY code").fetchall()
    lines_sql = """
        SELECT jl.account_id,
               COALESCE(SUM(jl.debit), 0) AS debit,
               COALESCE(SUM(jl.credit), 0) AS credit
        FROM journal_lines jl
        JOIN journal_entries je ON je.id = jl.entry_id
        WHERE 1=1
    """
    params = []
    if start:
        lines_sql += " AND je.entry_date >= ?"
        params.append(start)
    if end:
        lines_sql += " AND je.entry_date <= ?"
        params.append(end)
    lines_sql += " GROUP BY jl.account_id"
    line_rows = db.execute(lines_sql, params).fetchall()
    direct = {
        r["account_id"]: {"debit": float(r["debit"] or 0), "credit": float(r["credit"] or 0)}
        for r in line_rows
    }

    by_parent = {}
    for acc in accounts:
        by_parent.setdefault(acc["parent_id"], []).append(acc)
    for items in by_parent.values():
        items.sort(key=lambda r: r["code"])

    def walk(node, level=0):
        children = []
        debit = 0.0
        credit = 0.0

        # الرصيد المباشر يُحتسب فقط للحسابات التفصيلية (Postable) أو للحسابات التي لا أبناء لها.
        own = direct.get(node["id"], {"debit": 0.0, "credit": 0.0})
        include_own = bool(node["is_postable"])
        if include_own:
            debit += own["debit"]
            credit += own["credit"]

        for child in by_parent.get(node["id"], []):
            child_payload = walk(child, level + 1)
            children.append(child_payload)
            debit += child_payload["debit_total"]
            credit += child_payload["credit_total"]

        return {
            "id": node["id"],
            "code": node["code"],
            "name": node["name"],
            "node_type": node["node_type"],
            "parent_id": node["parent_id"],
            "is_postable": bool(node["is_postable"]),
            "active": bool(node["active"]),
            "level": level,
            "debit_total": debit,
            "credit_total": credit,
            "balance": debit - credit,
            "children": children,
            "has_direct_parent_balance": (not bool(node["is_postable"])) and (own["debit"] or own["credit"]),
            "direct_debit": own["debit"],
            "direct_credit": own["credit"],
        }

    return [walk(root, 0) for root in by_parent.get(None, [])]


def list_account_balances_flat(db, start=None, end=None):
    rows = []
    def visit(nodes, level=0):
        for node in nodes:
            rows.append({
                "id": node["id"],
                "code": node["code"],
                "name": node["name"],
                "node_type": node["node_type"],
                "parent_id": node["parent_id"],
                "is_postable": node["is_postable"],
                "active": node["active"],
                "level": level,
                "debit": node["debit_total"],
                "credit": node["credit_total"],
                "balance": node["balance"],
            })
            visit(node["children"], level + 1)
    visit(get_account_balance_rows(db, start=start, end=end))
    return rows


def next_voucher_no(db, prefix="VCH"):
    row = db.execute("SELECT voucher_no FROM accounting_vouchers ORDER BY id DESC LIMIT 1").fetchone()
    if not row or not row["voucher_no"]:
        return f"{prefix}-000001"
    digits = "".join(ch for ch in str(row["voucher_no"]) if ch.isdigit())
    try:
        n = int(digits) + 1
    except Exception:
        n = 1
    return f"{prefix}-{n:06d}"


def create_accounting_voucher(db, *, voucher_type, voucher_date, amount, from_account_id, to_account_id, reference="", description="", created_by=None, source_type="manual", source_id=None):
    voucher_no = next_voucher_no(db)
    with orm_session_scope() as session:
        voucher = AccountingVoucher(voucher_no=voucher_no, voucher_type=voucher_type, voucher_date=voucher_date, amount=float(amount or 0), from_account_id=from_account_id, to_account_id=to_account_id, reference=reference, description=description, source_type=source_type, source_id=source_id, status="posted", created_by=created_by, created_at=datetime.now().isoformat(), updated_at=datetime.now().isoformat())
        session.add(voucher)
        session.flush()
        entry = JournalEntry(entry_no="JE-" + datetime.now().strftime('%Y%m%d%H%M%S%f'), entry_date=voucher_date, source_type=f"voucher:{voucher_type}", source_id=voucher.id, description=description or reference or voucher_no, created_by=created_by, created_at=datetime.now().isoformat())
        session.add(entry)
        session.flush()
        session.add_all([
            JournalLine(entry_id=entry.id, account_id=to_account_id, debit=float(amount or 0), credit=0.0, description=description or reference or voucher_no),
            JournalLine(entry_id=entry.id, account_id=from_account_id, debit=0.0, credit=float(amount or 0), description=description or reference or voucher_no),
        ])
        voucher.journal_entry_id = entry.id
        return voucher.id

def update_accounting_voucher(db, voucher_id, *, voucher_type, voucher_date, amount, from_account_id, to_account_id, reference="", description="", updated_by=None):
    with orm_session_scope() as session:
        row = session.get(AccountingVoucher, voucher_id)
        if not row:
            raise ValueError("السند غير موجود.")
        if row.source_type != "manual":
            raise ValueError("السند الآلي لا يمكن تعديله من هذه الشاشة.")
        if row.journal_entry_id:
            session.execute(delete(JournalLine).where(JournalLine.entry_id == row.journal_entry_id))
            session.execute(delete(JournalEntry).where(JournalEntry.id == row.journal_entry_id))
        row.voucher_type = voucher_type
        row.voucher_date = voucher_date
        row.amount = float(amount or 0)
        row.from_account_id = from_account_id
        row.to_account_id = to_account_id
        row.reference = reference
        row.description = description
        row.updated_at = datetime.now().isoformat()
        entry = JournalEntry(entry_no="JE-" + datetime.now().strftime('%Y%m%d%H%M%S%f'), entry_date=voucher_date, source_type=f"voucher:{voucher_type}", source_id=voucher_id, description=description or reference or row.voucher_no, created_by=updated_by, created_at=datetime.now().isoformat())
        session.add(entry)
        session.flush()
        session.add_all([
            JournalLine(entry_id=entry.id, account_id=to_account_id, debit=float(amount or 0), credit=0.0, description=description or reference or row.voucher_no),
            JournalLine(entry_id=entry.id, account_id=from_account_id, debit=0.0, credit=float(amount or 0), description=description or reference or row.voucher_no),
        ])
        row.journal_entry_id = entry.id
        return voucher_id

def void_accounting_voucher(db, voucher_id):
    with orm_session_scope() as session:
        row = session.get(AccountingVoucher, voucher_id)
        if not row:
            raise ValueError("السند غير موجود.")
        if row.source_type != "manual":
            raise ValueError("السند الآلي لا يمكن حذفه من هذه الشاشة.")
        if row.journal_entry_id:
            session.execute(delete(JournalLine).where(JournalLine.entry_id == row.journal_entry_id))
            session.execute(delete(JournalEntry).where(JournalEntry.id == row.journal_entry_id))
        session.delete(row)
        return True

def vouchers_list(db, voucher_type=None):
    with orm_session_scope() as session:
        sql = """
            SELECT v.*, fa.code AS from_code, fa.name AS from_account_name,
                   ta.code AS to_code, ta.name AS to_account_name,
                   u.username AS created_by_name
            FROM accounting_vouchers v
            LEFT JOIN account_nodes fa ON fa.id = v.from_account_id
            LEFT JOIN account_nodes ta ON ta.id = v.to_account_id
            LEFT JOIN users u ON u.id = v.created_by
            WHERE 1=1
        """
        params = {}
        if voucher_type:
            sql += " AND v.voucher_type = :voucher_type"
            params["voucher_type"] = voucher_type
        sql += " ORDER BY v.id DESC"
        return session.execute(sa_text(sql), params).mappings().all()

def get_voucher_by_id(db, voucher_id):
    with orm_session_scope() as session:
        row = session.execute(sa_text("""
            SELECT v.*, fa.code AS from_code, fa.name AS from_account_name,
                   ta.code AS to_code, ta.name AS to_account_name,
                   u.username AS created_by_name
            FROM accounting_vouchers v
            LEFT JOIN account_nodes fa ON fa.id = v.from_account_id
            LEFT JOIN account_nodes ta ON ta.id = v.to_account_id
            LEFT JOIN users u ON u.id = v.created_by
            WHERE v.id = :voucher_id
        """), {"voucher_id": voucher_id}).mappings().first()
        return row

def user_name(db, user_id):
    if not user_id:
        return None
    row = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["username"] if row else None

def get_setting(key, default=""):
    with orm_session_scope() as session:
        row = session.get(Settings, key)
        return row.value if row and row.value is not None else default

def set_setting(key, value):
    with orm_session_scope() as session:
        row = session.get(Settings, key)
        if row is None:
            session.add(Settings(key=str(key), value=str(value)))
        else:
            row.value = str(value)

def _is_truthy(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "نعم", "فعال"}


def _normalize_month_key(invoice_date=None, month_label=None):
    label = (month_label or "").strip()
    if label:
        return label[:7]
    value = (invoice_date or "").strip()
    return value[:7] if value else ""


def _invoice_month_uniqueness_enabled(db):
    row = db.execute("SELECT value FROM settings WHERE key = 'enforce_one_invoice_per_month'").fetchone()
    if not row:
        return True
    return _is_truthy(row["value"], True)



def refresh_invoice_totals(db, invoice_id):
    row = db.execute("SELECT id, total_amount FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not row:
        return None

    paid_row = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS paid FROM payments WHERE invoice_id = ?",
        (invoice_id,),
    ).fetchone()
    paid_f = float((paid_row["paid"] if paid_row else 0) or 0)
    total = float(row["total_amount"] or 0)
    remaining = max(0.0, total - paid_f)
    credit = max(0.0, paid_f - total)

    db.execute(
        """
        UPDATE invoices
        SET paid_amount = ?,
            remaining_amount = ?,
            credit_amount = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (paid_f, remaining, credit, datetime.now().isoformat(), invoice_id),
    )
    return {"paid_amount": paid_f, "remaining_amount": remaining, "credit_amount": credit}

def apply_financial_constraints(db):
    """تطبيق قيود مستوى قاعدة البيانات لمنع القيم غير المنطقية وتثبيت مصادر الحقيقة."""
    db.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS trg_invoices_month_insert
        BEFORE INSERT ON invoices
        WHEN COALESCE((SELECT value FROM settings WHERE key='enforce_one_invoice_per_month'), '1') IN ('1','true','TRUE','yes','on')
             AND NEW.subscriber_id IS NOT NULL
             AND COALESCE(substr(COALESCE(NEW.month_label, NEW.invoice_date), 1, 7), '') <> ''
             AND EXISTS (
                 SELECT 1
                 FROM invoices i
                 WHERE i.subscriber_id = NEW.subscriber_id
                   AND COALESCE(substr(COALESCE(i.month_label, i.invoice_date), 1, 7), '') = COALESCE(substr(COALESCE(NEW.month_label, NEW.invoice_date), 1, 7), '')
             )
        BEGIN
            SELECT RAISE(ABORT, 'يوجد بالفعل فاتورة لهذا المشترك في هذا الشهر.');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_invoices_month_update
        BEFORE UPDATE OF subscriber_id, invoice_date, month_label ON invoices
        WHEN COALESCE((SELECT value FROM settings WHERE key='enforce_one_invoice_per_month'), '1') IN ('1','true','TRUE','yes','on')
             AND NEW.subscriber_id IS NOT NULL
             AND COALESCE(substr(COALESCE(NEW.month_label, NEW.invoice_date), 1, 7), '') <> ''
             AND EXISTS (
                 SELECT 1
                 FROM invoices i
                 WHERE i.subscriber_id = NEW.subscriber_id
                   AND i.id <> NEW.id
                   AND COALESCE(substr(COALESCE(i.month_label, i.invoice_date), 1, 7), '') = COALESCE(substr(COALESCE(NEW.month_label, NEW.invoice_date), 1, 7), '')
             )
        BEGIN
            SELECT RAISE(ABORT, 'يوجد بالفعل فاتورة لهذا المشترك في هذا الشهر.');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_invoices_non_negative_insert
        BEFORE INSERT ON invoices
        WHEN COALESCE(NEW.previous_reading, 0) < 0
          OR COALESCE(NEW.current_reading, 0) < 0
          OR COALESCE(NEW.consumption, 0) < 0
          OR COALESCE(NEW.unit_price, 0) < 0
          OR COALESCE(NEW.consumption_amount, 0) < 0
          OR COALESCE(NEW.subscription_fee, 0) < 0
          OR COALESCE(NEW.other_charges, 0) < 0
          OR COALESCE(NEW.opening_balance, 0) < 0
          OR COALESCE(NEW.total_amount, 0) < 0
          OR COALESCE(NEW.paid_amount, 0) < 0
          OR COALESCE(NEW.remaining_amount, 0) < 0
          OR COALESCE(NEW.credit_amount, 0) < 0
        BEGIN
            SELECT RAISE(ABORT, 'قيم الفاتورة المالية يجب أن تكون غير سالبة.');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_invoices_non_negative_update
        BEFORE UPDATE OF previous_reading, current_reading, consumption, unit_price, consumption_amount, subscription_fee, other_charges, opening_balance, total_amount, paid_amount, remaining_amount, credit_amount ON invoices
        WHEN COALESCE(NEW.previous_reading, 0) < 0
          OR COALESCE(NEW.current_reading, 0) < 0
          OR COALESCE(NEW.consumption, 0) < 0
          OR COALESCE(NEW.unit_price, 0) < 0
          OR COALESCE(NEW.consumption_amount, 0) < 0
          OR COALESCE(NEW.subscription_fee, 0) < 0
          OR COALESCE(NEW.other_charges, 0) < 0
          OR COALESCE(NEW.opening_balance, 0) < 0
          OR COALESCE(NEW.total_amount, 0) < 0
          OR COALESCE(NEW.paid_amount, 0) < 0
          OR COALESCE(NEW.remaining_amount, 0) < 0
          OR COALESCE(NEW.credit_amount, 0) < 0
        BEGIN
            SELECT RAISE(ABORT, 'قيم الفاتورة المالية يجب أن تكون غير سالبة.');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_payments_non_negative_insert
        BEFORE INSERT ON payments
        WHEN COALESCE(NEW.amount, 0) <= 0
        BEGIN
            SELECT RAISE(ABORT, 'مبلغ السداد يجب أن يكون أكبر من صفر.');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_payments_non_negative_update
        BEFORE UPDATE OF amount ON payments
        WHEN COALESCE(NEW.amount, 0) <= 0
        BEGIN
            SELECT RAISE(ABORT, 'مبلغ السداد يجب أن يكون أكبر من صفر.');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_payment_refresh_invoice_ai
        AFTER INSERT ON payments
        WHEN NEW.invoice_id IS NOT NULL
        BEGIN
            UPDATE invoices
            SET paid_amount = COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = NEW.invoice_id), 0),
                remaining_amount = CASE
                    WHEN COALESCE(total_amount, 0) - COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = NEW.invoice_id), 0) > 0
                        THEN COALESCE(total_amount, 0) - COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = NEW.invoice_id), 0)
                    ELSE 0
                END,
                credit_amount = CASE
                    WHEN COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = NEW.invoice_id), 0) - COALESCE(total_amount, 0) > 0
                        THEN COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = NEW.invoice_id), 0) - COALESCE(total_amount, 0)
                    ELSE 0
                END,
                updated_at = datetime('now')
            WHERE id = NEW.invoice_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_payment_refresh_invoice_au
        AFTER UPDATE OF amount, invoice_id ON payments
        BEGIN
            UPDATE invoices
            SET paid_amount = COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = OLD.invoice_id), 0),
                remaining_amount = CASE
                    WHEN COALESCE(total_amount, 0) - COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = OLD.invoice_id), 0) > 0
                        THEN COALESCE(total_amount, 0) - COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = OLD.invoice_id), 0)
                    ELSE 0
                END,
                credit_amount = CASE
                    WHEN COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = OLD.invoice_id), 0) - COALESCE(total_amount, 0) > 0
                        THEN COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = OLD.invoice_id), 0) - COALESCE(total_amount, 0)
                    ELSE 0
                END,
                updated_at = datetime('now')
            WHERE id = OLD.invoice_id;

            UPDATE invoices
            SET paid_amount = COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = NEW.invoice_id), 0),
                remaining_amount = CASE
                    WHEN COALESCE(total_amount, 0) - COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = NEW.invoice_id), 0) > 0
                        THEN COALESCE(total_amount, 0) - COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = NEW.invoice_id), 0)
                    ELSE 0
                END,
                credit_amount = CASE
                    WHEN COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = NEW.invoice_id), 0) - COALESCE(total_amount, 0) > 0
                        THEN COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = NEW.invoice_id), 0) - COALESCE(total_amount, 0)
                    ELSE 0
                END,
                updated_at = datetime('now')
            WHERE id = NEW.invoice_id AND NEW.invoice_id IS NOT OLD.invoice_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_payment_refresh_invoice_ad
        AFTER DELETE ON payments
        WHEN OLD.invoice_id IS NOT NULL
        BEGIN
            UPDATE invoices
            SET paid_amount = COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = OLD.invoice_id), 0),
                remaining_amount = CASE
                    WHEN COALESCE(total_amount, 0) - COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = OLD.invoice_id), 0) > 0
                        THEN COALESCE(total_amount, 0) - COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = OLD.invoice_id), 0)
                    ELSE 0
                END,
                credit_amount = CASE
                    WHEN COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = OLD.invoice_id), 0) - COALESCE(total_amount, 0) > 0
                        THEN COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = OLD.invoice_id), 0) - COALESCE(total_amount, 0)
                    ELSE 0
                END,
                updated_at = datetime('now')
            WHERE id = OLD.invoice_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_journal_lines_non_negative_insert
        BEFORE INSERT ON journal_lines
        WHEN COALESCE(NEW.debit, 0) < 0
          OR COALESCE(NEW.credit, 0) < 0
          OR (COALESCE(NEW.debit, 0) > 0 AND COALESCE(NEW.credit, 0) > 0)
        BEGIN
            SELECT RAISE(ABORT, 'سطر القيد يجب أن يكون مديناً أو دائناً فقط وبقيم غير سالبة.');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_journal_lines_non_negative_update
        BEFORE UPDATE OF debit, credit ON journal_lines
        WHEN COALESCE(NEW.debit, 0) < 0
          OR COALESCE(NEW.credit, 0) < 0
          OR (COALESCE(NEW.debit, 0) > 0 AND COALESCE(NEW.credit, 0) > 0)
        BEGIN
            SELECT RAISE(ABORT, 'سطر القيد يجب أن يكون مديناً أو دائناً فقط وبقيم غير سالبة.');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_accounting_vouchers_non_negative_insert
        BEFORE INSERT ON accounting_vouchers
        WHEN COALESCE(NEW.amount, 0) <= 0
        BEGIN
            SELECT RAISE(ABORT, 'قيمة السند يجب أن تكون أكبر من صفر.');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_accounting_vouchers_non_negative_update
        BEFORE UPDATE OF amount ON accounting_vouchers
        WHEN COALESCE(NEW.amount, 0) <= 0
        BEGIN
            SELECT RAISE(ABORT, 'قيمة السند يجب أن تكون أكبر من صفر.');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_collection_trips_non_negative_insert
        BEFORE INSERT ON collection_trips
        WHEN COALESCE(NEW.target_amount, 0) < 0 OR COALESCE(NEW.collected_amount, 0) < 0
        BEGIN
            SELECT RAISE(ABORT, 'أرقام الجولات يجب أن تكون غير سالبة.');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_trip_visits_non_negative_insert
        BEFORE INSERT ON trip_visits
        WHEN COALESCE(NEW.amount_collected, 0) < 0
        BEGIN
            SELECT RAISE(ABORT, 'المبلغ المحصل لا يمكن أن يكون سالباً.');
        END;
        """
    )


def financial_reset(db, *, clear_opening_snapshots=True):
    """تصفير مالي آمن مع الإبقاء على العملاء وشجرة الحسابات والمستخدمين."""
    backup_dir = BASE_DIR / "instance" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"financial_backup_{ts}.sqlite3"

    source = sqlite3.connect(str(DB_PATH))
    dest = sqlite3.connect(str(backup_path))
    try:
        source.backup(dest)
    finally:
        source.close()
        dest.close()

    tables = [
        "payments",
        "attachments",
        "trip_visits",
        "collection_trips",
        "invoices",
        "journal_lines",
        "journal_entries",
        "accounting_vouchers",
        "transactions",
        "bulk_readings",
    ]
    for table in tables:
        db.execute(f"DELETE FROM {table}")

    try:
        db.execute("UPDATE wallets SET balance = 0")
    except Exception:
        pass

    if clear_opening_snapshots:
        db.execute(
            """
            UPDATE subscribers
            SET last_reading = NULL,
                last_due_amount = 0,
                last_paid_amount = 0,
                updated_at = ?
            """,
            (datetime.now().isoformat(),),
        )

    placeholders = ",".join("?" for _ in tables)
    db.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", tables)
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("invoice_start_no", "1"),
    )
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("financial_reset_at", datetime.now().isoformat()),
    )
    return str(backup_path)


def get_opening_balance(db, subscriber_id, exclude_invoice_id=None):
    sql = """
        SELECT COALESCE(SUM(COALESCE(remaining_amount, 0) - COALESCE(credit_amount, 0)), 0) AS bal
        FROM invoices
        WHERE subscriber_id = ?
    """
    params = [subscriber_id]
    if exclude_invoice_id:
        sql += " AND id != ?"
        params.append(exclude_invoice_id)
    row = db.execute(sql, params).fetchone()
    return row["bal"] if row else 0

def get_subscriber_opening_snapshot(db, subscriber_id):
    """إرجاع آخر بيانات افتتاحية محفوظة للمشترك عند عدم وجود فاتورة سابقة."""
    row = db.execute(
        """
        SELECT last_reading, last_due_amount, last_paid_amount
        FROM subscribers
        WHERE id = ?
        """,
        (subscriber_id,),
    ).fetchone()
    if not row:
        return {"previous_reading": None, "previous_arrears": 0.0}

    last_due = float(row["last_due_amount"] or 0)
    last_paid = float(row["last_paid_amount"] or 0)
    previous_arrears = max(0.0, last_due - last_paid)
    previous_reading = row["last_reading"]
    try:
        previous_reading = float(previous_reading) if previous_reading is not None else None
    except Exception:
        previous_reading = None
    return {
        "previous_reading": previous_reading,
        "previous_arrears": previous_arrears,
    }

def get_invoice_starting_context(db, subscriber_id, exclude_invoice_id=None):
    """إرجاع القراءة/المتأخرات/الرصيد الافتتاحي بنفس منطق نموذج إنشاء الفاتورة الفردي."""
    snapshot = get_subscriber_opening_snapshot(db, subscriber_id)
    if exclude_invoice_id:
        last_invoice = db.execute(
            """
            SELECT current_reading
            FROM invoices
            WHERE subscriber_id = ? AND id != ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (subscriber_id, exclude_invoice_id),
        ).fetchone()
    else:
        last_invoice = db.execute(
            """
            SELECT current_reading
            FROM invoices
            WHERE subscriber_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (subscriber_id,),
        ).fetchone()

    if last_invoice and last_invoice["current_reading"] is not None:
        previous_reading = float(last_invoice["current_reading"] or 0)
        previous_arrears = 0.0
    else:
        previous_reading = snapshot.get("previous_reading")
        if previous_reading is None:
            subscriber = db.execute("SELECT last_reading FROM subscribers WHERE id=?", (subscriber_id,)).fetchone()
            previous_reading = float(subscriber["last_reading"] or 0) if subscriber else 0.0
        previous_arrears = float(snapshot.get("previous_arrears") or 0)

    opening_balance = float(get_opening_balance(db, subscriber_id, exclude_invoice_id=exclude_invoice_id) or 0) + float(previous_arrears or 0)
    return {
        "previous_reading": float(previous_reading or 0),
        "previous_arrears": float(previous_arrears or 0),
        "opening_balance": float(opening_balance or 0),
    }

def next_invoice_no(db):
    try:
        start_no = int(get_setting("invoice_start_no", DEFAULT_SETTINGS["invoice_start_no"]))
    except ValueError:
        start_no = 1
    with orm_session_scope() as session:
        max_no = session.scalar(select(func.coalesce(func.max(Invoice.invoice_no), 0))) or 0
        return max(start_no, int(max_no) + 1)

def next_subscriber_account_number(db, prefix="WS"):
    """توليد رقم حساب تلقائي للمشترك الجديد بدون كسر الأرقام القديمة."""
    import re as _re
    with orm_session_scope() as session:
        rows = session.scalars(select(Subscriber.account_number).where(Subscriber.account_number.is_not(None))).all()
    max_num = 0
    for val in rows:
        val = str(val or "").strip()
        if not val:
            continue
        digits = _re.findall(r"(\d+)", val)
        if digits:
            try:
                max_num = max(max_num, int(digits[-1]))
            except ValueError:
                pass
    next_num = max_num + 1
    return f"{prefix}-{next_num:05d}"

def _subscriber_account_code(subscriber_id, account_number):
    safe = "".join(ch for ch in str(account_number or subscriber_id) if ch.isalnum())
    safe = safe[-10:] if safe else f"{int(subscriber_id):05d}"
    return f"CUST-{safe}"


def ensure_subscriber_account(db, subscriber_id, account_number=None, name=None):
    """إنشاء أو ربط حساب تفصيلي للمشترك تحت حسابات العملاء."""
    row = db.execute("SELECT * FROM subscribers WHERE id=?", (subscriber_id,)).fetchone()
    if not row:
        return None
    account_node_id = row["account_node_id"] if "account_node_id" in row.keys() else None
    if account_node_id:
        existing = db.execute("SELECT id FROM account_nodes WHERE id=? AND active=1", (account_node_id,)).fetchone()
        if existing:
            if name and row["name"] != name:
                db.execute("UPDATE account_nodes SET name=?, updated_at=? WHERE id=?", (name, datetime.now().isoformat(), account_node_id))
            return account_node_id
    branch = db.execute("SELECT id FROM account_nodes WHERE code='1210' AND active=1 LIMIT 1").fetchone()
    if not branch:
        branch = db.execute("SELECT id FROM account_nodes WHERE code='1200' AND active=1 LIMIT 1").fetchone()
    parent_id = branch["id"] if branch else None
    code = _subscriber_account_code(subscriber_id, account_number or row["account_number"])
    existing = db.execute("SELECT id FROM account_nodes WHERE code=?", (code,)).fetchone()
    if existing:
        account_id = existing["id"]
    else:
        account_id = add_account_node(db, code, name or row["name"], node_type="receivable", parent_id=parent_id, is_postable=1)
    db.execute("UPDATE subscribers SET account_node_id=? WHERE id=?", (account_id, subscriber_id))
    return account_id


def ensure_all_party_accounts(db):
    """مزامنة حسابات العملاء والموظفين عند الإقلاع أو بعد الاستيراد."""
    ensure_accounting_seed(db)
    subs = db.execute("SELECT id, account_number, name FROM subscribers WHERE active=1 ORDER BY id").fetchall()
    for sub in subs:
        ensure_subscriber_account(db, sub["id"], sub["account_number"], sub["name"])
    users = db.execute("SELECT u.id, u.username, ep.full_name, ep.job_title FROM users u LEFT JOIN employee_profiles ep ON ep.user_id = u.id WHERE u.active=1 ORDER BY u.id").fetchall()
    for user in users:
        create_employee_account(db, user["id"], full_name=user["full_name"] or user["username"], job_title=user["job_title"] or "Collector")


def archive_subscriber_account(db, subscriber_id):
    row = db.execute("SELECT account_node_id, account_number, name FROM subscribers WHERE id=?", (subscriber_id,)).fetchone()
    if not row:
        return None
    account_id = row["account_node_id"]
    if account_id:
        db.execute(
            "UPDATE account_nodes SET active=0, updated_at=?, name=CASE WHEN name LIKE 'مؤرشف:%' THEN name ELSE 'مؤرشف: ' || name END WHERE id=?",
            (datetime.now().isoformat(), account_id),
        )
    db.execute("UPDATE subscribers SET active=0, updated_at=? WHERE id=?", (datetime.now().isoformat(), subscriber_id))
    return True


def get_employee_account_node_id(db, user_id):
    row = db.execute("SELECT account_node_id FROM employee_profiles WHERE user_id = ?", (user_id,)).fetchone()
    return row["account_node_id"] if row and row["account_node_id"] else None



def get_default_cash_account_id(db):
    # نفضّل حساباً تفصيلياً ضمن فرع الصناديق، وليس الحساب الرئيسي نفسه.
    row = db.execute(
        """
        SELECT id FROM account_nodes
        WHERE active = 1 AND is_postable = 1 AND node_type = 'cash'
          AND parent_id IN (SELECT id FROM account_nodes WHERE code = '1100' AND active = 1)
        ORDER BY code
        LIMIT 1
        """
    ).fetchone()
    if row:
        return row["id"]
    row = db.execute("""
        SELECT id FROM account_nodes
        WHERE node_type = 'cash' AND is_postable = 1 AND active = 1
        ORDER BY code LIMIT 1
    """).fetchone()
    return row["id"] if row else None



def get_default_receivable_account_id(db):
    row = db.execute("SELECT id FROM account_nodes WHERE code = '1210' AND active = 1 LIMIT 1").fetchone()
    if row:
        return row["id"]
    row = db.execute("""
        SELECT id FROM account_nodes
        WHERE node_type = 'receivable' AND is_postable = 1 AND active = 1
        ORDER BY code LIMIT 1
    """).fetchone()
    if row:
        return row["id"]
    row = db.execute("""
        SELECT id FROM account_nodes
        WHERE active = 1 AND is_postable = 1
        ORDER BY code LIMIT 1
    """).fetchone()
    return row["id"] if row else None


def get_default_expense_account_id(db):
    row = db.execute("SELECT id FROM account_nodes WHERE code = '5100' AND active = 1 LIMIT 1").fetchone()
    if row:
        return row["id"]
    row = db.execute("""
        SELECT id FROM account_nodes
        WHERE node_type = 'expense' AND is_postable = 1 AND active = 1
        ORDER BY code LIMIT 1
    """).fetchone()
    return row["id"] if row else None


def get_default_income_account_id(db):
    row = db.execute("SELECT id FROM account_nodes WHERE code = '4100' AND active = 1 LIMIT 1").fetchone()
    if row:
        return row["id"]
    row = db.execute("""
        SELECT id FROM account_nodes
        WHERE node_type = 'income' AND is_postable = 1 AND active = 1
        ORDER BY code LIMIT 1
    """).fetchone()
    return row["id"] if row else None


def log_transaction(db, trans_type, amount, wallet_id, user_id, subscriber_id=None, reference_id=None, notes="", session=None):
    """تسجيل حركة مالية في سجل الخزن القديم عند وجودها فقط."""
    if not wallet_id:
        return None
    if session is not None:
        wallet = session.get(Wallet, wallet_id)
        if not wallet:
            return None
        amt = float(amount or 0)
        session.add(Transaction(type=trans_type, amount=amt, wallet_id=wallet_id, user_id=user_id, subscriber_id=subscriber_id, reference_id=reference_id, notes=notes, created_at=datetime.now().isoformat()))
        wallet.balance = float(wallet.balance or 0) + (amt if trans_type == 'IN' else -amt)
        return True
    with orm_session_scope() as _session:
        wallet = _session.get(Wallet, wallet_id)
        if not wallet:
            return None
        amt = float(amount or 0)
        _session.add(Transaction(type=trans_type, amount=amt, wallet_id=wallet_id, user_id=user_id, subscriber_id=subscriber_id, reference_id=reference_id, notes=notes, created_at=datetime.now().isoformat()))
        wallet.balance = float(wallet.balance or 0) + (amt if trans_type == 'IN' else -amt)
        return True

def log_audit(db, user_id, action, table_name, row_id, description, session=None):
    """توثيق حركات الإضافة والتعديل والمسح لضمان حماية النظام والرقابة الماليّة"""
    if session is not None:
        session.add(AuditLog(user_id=user_id, action=action, table_name=table_name, row_id=row_id, description=description, timestamp=datetime.now().isoformat()))
        return
    with orm_session_scope() as _session:
        _session.add(AuditLog(user_id=user_id, action=action, table_name=table_name, row_id=row_id, description=description, timestamp=datetime.now().isoformat()))


def process_invoice_payment(db, invoice_id, user_id, amount, method, notes, attachment_path=None, wallet_id=None, cash_account_id=None, receivable_account_id=None):
    """معالجة السداد وتقييده على شجرة الحسابات وسندات القبض."""
    try:
        amount = float(amount or 0)
    except Exception:
        amount = 0.0
    if amount <= 0:
        raise ValueError("المبلغ يجب أن يكون أكبر من صفر.")

    today_str = date.today().isoformat()
    now = datetime.now().isoformat()

    invoice_row = db.execute(
        """
        SELECT i.id, i.invoice_no, i.subscriber_id, i.total_amount,
               s.account_number, s.name, s.account_node_id
        FROM invoices i
        LEFT JOIN subscribers s ON s.id = i.subscriber_id
        WHERE i.id = ?
        """,
        (invoice_id,),
    ).fetchone()
    if not invoice_row:
        raise ValueError("الفاتورة غير موجودة في النظام.")

    subscriber_id = invoice_row["subscriber_id"]
    if not subscriber_id:
        raise ValueError("الفاتورة لا تحتوي على مشترك مرتبط.")

    # ضمان أن السداد مربوط مباشرة بحساب العميل التفصيلي وليس بحساب ذمم عام.
    from_account_id = ensure_subscriber_account(
        db,
        int(subscriber_id),
        invoice_row["account_number"],
        invoice_row["name"],
    )
    if not from_account_id:
        raise ValueError("تعذر ربط المشترك بحسابه المحاسبي التفصيلي.")

    # السداد يُرحَّل دائماً إلى حساب الموظف المسجّل دخوله، وليس إلى الصندوق العام.
    to_account_id = get_employee_account_node_id(db, user_id)
    if not to_account_id:
        user_row = db.execute("SELECT username, role FROM users WHERE id = ?", (user_id,)).fetchone()
        to_account_id = create_employee_account(
            db,
            user_id,
            full_name=(user_row["username"] if user_row else None),
            job_title=(user_row["role"] if user_row and user_row["role"] else "Collector"),
        )

    if not to_account_id:
        raise ValueError("تعذر تحديد حساب الموظف المسجّل للسداد.")

    with orm_session_scope() as session:
        invoice = session.get(Invoice, invoice_id)
        if not invoice:
            raise ValueError("الفاتورة غير موجودة في النظام.")

        payment = Payment(
            invoice_id=invoice_id,
            subscriber_id=subscriber_id,
            amount=amount,
            payment_date=today_str,
            method=method,
            notes=notes,
            attachment_path=attachment_path,
            created_by=user_id,
            created_at=now,
        )
        session.add(payment)
        session.flush()

        paid_amount = session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.invoice_id == invoice_id)
        ) or 0
        total_amount = float(invoice.total_amount or 0)
        paid_amount_f = float(paid_amount or 0)
        remaining_amount = max(0.0, total_amount - paid_amount_f)
        credit_amount = max(0.0, paid_amount_f - total_amount)

        invoice.paid_amount = paid_amount_f
        invoice.remaining_amount = remaining_amount
        invoice.credit_amount = credit_amount
        invoice.updated_at = now

        log_audit(
            db,
            user_id,
            "INSERT",
            "payments",
            payment.id,
            f"تحصيل مبلغ {amount} للفاتورة رقم {invoice.invoice_no}",
            session=session,
        )

        voucher_no = next_voucher_no(db)
        voucher = AccountingVoucher(
            voucher_no=voucher_no,
            voucher_type="receipt",
            voucher_date=today_str,
            amount=amount,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            reference=f"INV-{invoice.invoice_no}",
            description=f"سداد فاتورة رقم {invoice.invoice_no}",
            source_type="payment",
            source_id=payment.id,
            status="posted",
            created_by=user_id,
            created_at=now,
            updated_at=now,
        )
        session.add(voucher)
        session.flush()

        entry = JournalEntry(
            entry_no="JE-" + datetime.now().strftime("%Y%m%d%H%M%S%f"),
            entry_date=today_str,
            source_type="voucher:receipt",
            source_id=voucher.id,
            description=f"سداد فاتورة رقم {invoice.invoice_no}",
            created_by=user_id,
            created_at=now,
        )
        session.add(entry)
        session.flush()
        session.add_all([
            JournalLine(entry_id=entry.id, account_id=to_account_id, debit=amount, credit=0.0, description=voucher.description),
            JournalLine(entry_id=entry.id, account_id=from_account_id, debit=0.0, credit=amount, description=voucher.description),
        ])
        voucher.journal_entry_id = entry.id

        log_audit(
            db,
            user_id,
            "INSERT",
            "accounting_vouchers",
            voucher.id,
            f"إنشاء سند قبض للفاتورة رقم {invoice.invoice_no}",
            session=session,
        )

        if wallet_id:
            log_transaction(
                db,
                trans_type="IN",
                amount=amount,
                wallet_id=wallet_id,
                user_id=user_id,
                subscriber_id=subscriber_id,
                reference_id=payment.id,
                notes=f"سداد جزء/كل من فاتورة رقم {invoice.invoice_no}",
                session=session,
            )
        return payment.id


def post_invoice_accounting(db, invoice_id, user_id=None, amount=None, method="نقداً", notes="", attachment_path=None, wallet_id=None, cash_account_id=None, receivable_account_id=None, reverse_existing=None, **kwargs):
    """
    ترحيل الفاتورة محاسبياً على أساس سند إثبات ثم قيد يومية مرتبطين معاً.
    - الفاتورة الدائنة للعميل تُسجل كسند إثبات من حساب العميل التفصيلي إلى حساب إيراد المياه.
    - القيد لا يُنشأ منفصلاً عن السند.
    - عند التعديل مع reverse_existing=True يتم عكس السند والقيد السابقين ثم إعادة الترحيل.
    """
    try:
        amt = float(amount or 0)
    except Exception:
        amt = 0.0

    # إن كان المقصود سداداً مالياً، فالمرجع هو مسار التحصيل.
    if amt > 0:
        if user_id is None:
            user_id = 0
        return process_invoice_payment(
            db,
            invoice_id=invoice_id,
            user_id=user_id,
            amount=amt,
            method=method,
            notes=notes,
            attachment_path=attachment_path,
            wallet_id=wallet_id,
            cash_account_id=cash_account_id,
            receivable_account_id=receivable_account_id,
        )

    invoice = db.execute(
        """
        SELECT i.*, s.account_number, s.name, s.account_node_id
        FROM invoices i
        LEFT JOIN subscribers s ON s.id = i.subscriber_id
        WHERE i.id = ?
        """,
        (invoice_id,),
    ).fetchone()
    if not invoice:
        raise ValueError("الفاتورة غير موجودة.")

    total_amount = float(invoice["total_amount"] or 0)
    if total_amount <= 0:
        raise ValueError("لا يمكن ترحيل فاتورة بقيمة صفر أو سالبة. راجع الرصيد الافتتاحي/المدفوعات السابقة.")

    subscriber_id = invoice["subscriber_id"]
    from_account_id = None
    if subscriber_id:
        from_account_id = ensure_subscriber_account(
            db,
            int(subscriber_id),
            invoice["account_number"],
            invoice["name"],
        )
    if not from_account_id:
        raise ValueError("تعذر ربط الفاتورة بحساب العميل التفصيلي.")

    to_account_id = get_default_income_account_id(db)
    if not to_account_id:
        raise ValueError("تعذر العثور على حساب إيراد المياه.")

    now = datetime.now().isoformat()
    invoice_date = (invoice["invoice_date"] or date.today().isoformat())

    # عكس أي ترحيل سابق عند التعديل حتى لا تتراكم قيود مزدوجة.
    existing_voucher = db.execute(
        """
        SELECT *
        FROM accounting_vouchers
        WHERE source_type = 'invoice' AND source_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (invoice_id,),
    ).fetchone()
    if reverse_existing and existing_voucher:
        if existing_voucher["journal_entry_id"]:
            old_entry = db.execute(
                "SELECT * FROM journal_entries WHERE id = ?",
                (existing_voucher["journal_entry_id"],),
            ).fetchone()
            if old_entry:
                old_lines = db.execute(
                    """
                    SELECT account_id, debit, credit, description
                    FROM journal_lines
                    WHERE entry_id = ?
                    ORDER BY id ASC
                    """,
                    (old_entry["id"],),
                ).fetchall()
                if old_lines:
                    reversal_no = "JE-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
                    reversal_desc = f"عكس قيد الفاتورة رقم {invoice['invoice_no']}"
                    db.execute(
                        """
                        INSERT INTO journal_entries (entry_no, entry_date, source_type, source_id, description, created_by, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (reversal_no, invoice_date, "reversal:invoice", invoice_id, reversal_desc, user_id, now),
                    )
                    reversal_entry_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                    for line in old_lines:
                        db.execute(
                            """
                            INSERT INTO journal_lines (entry_id, account_id, debit, credit, description)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                reversal_entry_id,
                                line["account_id"],
                                float(line["credit"] or 0),
                                float(line["debit"] or 0),
                                f"عكس: {line['description'] or reversal_desc}",
                            ),
                        )

        db.execute(
            """
            UPDATE accounting_vouchers
            SET status = 'void',
                updated_at = ?
            WHERE id = ?
            """,
            (now, existing_voucher["id"]),
        )

    # إذا كان الترحيل موجوداً مسبقاً ولم يُطلب عكسه، لا ننشئ قيوداً مكررة.
    if existing_voucher and not reverse_existing:
        if not invoice["journal_entry_id"]:
            db.execute(
                "UPDATE invoices SET journal_entry_id = ?, updated_at = ? WHERE id = ?",
                (existing_voucher["journal_entry_id"], now, invoice_id),
            )
        return existing_voucher["id"]

    voucher_no = next_voucher_no(db)
    voucher_desc = f"إثبات فاتورة رقم {invoice['invoice_no']}"
    reference = f"INV-{invoice['invoice_no']}"

    db.execute(
        """
        INSERT INTO accounting_vouchers (
            voucher_no, voucher_type, voucher_date, amount,
            from_account_id, to_account_id, reference, description,
            source_type, source_id, status, created_by, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'posted', ?, ?, ?)
        """,
        (
            voucher_no,
            "invoice",
            invoice_date,
            total_amount,
            from_account_id,
            to_account_id,
            reference,
            voucher_desc,
            "invoice",
            invoice_id,
            user_id,
            now,
            now,
        ),
    )
    voucher_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    entry_no = "JE-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    db.execute(
        """
        INSERT INTO journal_entries (entry_no, entry_date, source_type, source_id, description, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (entry_no, invoice_date, "invoice", invoice_id, voucher_desc, user_id, now),
    )
    entry_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    db.execute(
        """
        INSERT INTO journal_lines (entry_id, account_id, debit, credit, description)
        VALUES (?, ?, ?, 0, ?)
        """,
        (entry_id, from_account_id, total_amount, "ذمم العميل"),
    )
    db.execute(
        """
        INSERT INTO journal_lines (entry_id, account_id, debit, credit, description)
        VALUES (?, ?, 0, ?, ?)
        """,
        (entry_id, to_account_id, total_amount, "إيراد المياه"),
    )

    db.execute(
        "UPDATE accounting_vouchers SET journal_entry_id = ? WHERE id = ?",
        (entry_id, voucher_id),
    )
    db.execute(
        "UPDATE invoices SET journal_entry_id = ?, updated_at = ? WHERE id = ?",
        (entry_id, now, invoice_id),
    )
    return voucher_id

def add_user(db, username, password_hash, role, wallet_id=None):
    with orm_session_scope() as session:
        user = User(username=username, password_hash=password_hash, role=role, wallet_id=wallet_id, created_at=datetime.now().isoformat())
        session.add(user)
        session.flush()
        return user.id

def link_user_wallet(db, user_id, wallet_id):
    with orm_session_scope() as session:
        user = session.get(User, user_id)
        if user:
            user.wallet_id = wallet_id
        session.execute(delete(UserWallet).where(UserWallet.user_id == user_id))
        if wallet_id:
            session.add(UserWallet(user_id=user_id, wallet_id=wallet_id))

def replace_database_from_file(file_bytes, db_path=None):
    """
    استبدال قاعدة البيانات الحالية بملف مرفوع (Restore) مع التحقق من صحة SQLite.
    """
    db_path = db_path or str(DB_PATH)
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.write(file_bytes)
        tmp.close()

        conn = sqlite3.connect(tmp.name)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1;")
        conn.close()

        target = Path(db_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()

        os.replace(tmp.name, str(target))
        return True

    except Exception as e:
        print("Restore Error:", e)
        return False


def backup_database_bytes(db_path=None):
    """
    نسخة احتياطية فعلية (ملف SQLite كامل) من قاعدة البيانات النشطة.
    """
    db_path = db_path or str(DB_PATH)
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False)
        backup_path = tmp.name

        source = sqlite3.connect(db_path)
        dest = sqlite3.connect(backup_path)

        source.backup(dest)

        source.close()
        dest.close()

        with open(backup_path, 'rb') as f:
            return f.read()

    except Exception as e:
        print("Backup Error:", e)
        return None


def list_journal_entries(db, start=None, end=None, reference=None, limit=500):
    with orm_session_scope() as session:
        sql = """
            SELECT je.id, je.entry_no, je.entry_date, je.source_type, je.source_id, je.description,
                   je.created_by, je.created_at,
                   u.username AS created_by_name
            FROM journal_entries je
            LEFT JOIN users u ON u.id = je.created_by
            WHERE 1=1
        """
        params = {}
        if start:
            sql += " AND date(je.entry_date) >= date(:start)"
            params["start"] = start
        if end:
            sql += " AND date(je.entry_date) <= date(:end)"
            params["end"] = end
        if reference:
            sql += " AND (je.entry_no LIKE :reference OR je.description LIKE :reference2)"
            params["reference"] = f"%{reference}%"
            params["reference2"] = f"%{reference}%"
        sql += " ORDER BY je.id DESC"
        if limit:
            sql += " LIMIT :limit"
            params["limit"] = int(limit)
        rows = session.execute(sa_text(sql), params).mappings().all()

        entry_ids = [row["id"] for row in rows]
        lines_by_entry = {}
        if entry_ids:
            placeholders = ",".join(f":id{i}" for i in range(len(entry_ids)))
            line_sql = f"""
                SELECT jl.entry_id, jl.account_id, jl.debit, jl.credit, jl.description,
                       a.code AS account_code, a.name AS account_name, a.node_type AS account_type,
                       jl.id AS id
                FROM journal_lines jl
                JOIN account_nodes a ON a.id = jl.account_id
                WHERE jl.entry_id IN ({placeholders})
                ORDER BY jl.entry_id, jl.id
            """
            line_params = {f"id{i}": entry_ids[i] for i in range(len(entry_ids))}
            line_rows = session.execute(sa_text(line_sql), line_params).mappings().all()
            for line in line_rows:
                lines_by_entry.setdefault(line["entry_id"], []).append(line)

        result = []
        for row in rows:
            debit_total = sum(float(l["debit"] or 0) for l in lines_by_entry.get(row["id"], []))
            credit_total = sum(float(l["credit"] or 0) for l in lines_by_entry.get(row["id"], []))
            result.append({
                "entry": row,
                "lines": lines_by_entry.get(row["id"], []),
                "debit_total": debit_total,
                "credit_total": credit_total,
            })
        return result

def get_journal_entry_by_id(db, entry_id):
    rows = list_journal_entries(db, limit=None)
    for item in rows:
        if int(item["entry"]["id"]) == int(entry_id):
            return item
    return None

def create_manual_journal_entry(db, *, entry_date, description, lines, created_by=None, source_type="manual", source_id=None, is_posted=True):
    if not lines:
        raise ValueError("لا توجد سطور للقيد.")
    total_debit = sum(float(line.get("debit") or 0) for line in lines)
    total_credit = sum(float(line.get("credit") or 0) for line in lines)
    if round(total_debit, 2) != round(total_credit, 2):
        raise ValueError("القيد غير متوازن.")
    with orm_session_scope() as session:
        entry = JournalEntry(entry_no='JE-' + datetime.now().strftime('%Y%m%d%H%M%S%f'), entry_date=entry_date, source_type=source_type, source_id=source_id, description=description, created_by=created_by, created_at=datetime.now().isoformat())
        session.add(entry)
        session.flush()
        for line in lines:
            account_id = int(line.get('account_id') or 0)
            debit = float(line.get('debit') or 0)
            credit = float(line.get('credit') or 0)
            line_desc = (line.get('description') or description or '').strip()
            if not account_id:
                raise ValueError('account_required')
            if debit > 0 and credit > 0:
                raise ValueError('لا يمكن إدخال مدين ودائن في السطر نفسه.')
            if debit <= 0 and credit <= 0:
                raise ValueError('يجب إدخال مدين أو دائن في كل سطر.')
            session.add(JournalLine(entry_id=entry.id, account_id=account_id, debit=debit, credit=credit, description=line_desc))
        return entry.id

def update_manual_journal_entry(db, entry_id, *, entry_date, description, lines, is_posted=True):
    with orm_session_scope() as session:
        row = session.get(JournalEntry, entry_id)
        if not row:
            raise ValueError("القيد غير موجود.")
        if row.source_type != "manual":
            raise ValueError("القيد الآلي لا يمكن تعديله من هذه الشاشة.")
        total_debit = sum(float(line.get("debit") or 0) for line in lines)
        total_credit = sum(float(line.get("credit") or 0) for line in lines)
        if round(total_debit, 2) != round(total_credit, 2):
            raise ValueError("القيد غير متوازن.")
        session.execute(delete(JournalLine).where(JournalLine.entry_id == entry_id))
        row.entry_date = entry_date
        row.description = description
        for line in lines:
            account_id = int(line.get('account_id') or 0)
            debit = float(line.get('debit') or 0)
            credit = float(line.get('credit') or 0)
            line_desc = (line.get('description') or description or '').strip()
            if not account_id:
                raise ValueError('account_required')
            if debit > 0 and credit > 0:
                raise ValueError('لا يمكن إدخال مدين ودائن في السطر نفسه.')
            if debit <= 0 and credit <= 0:
                raise ValueError('يجب إدخال مدين أو دائن في كل سطر.')
            session.add(JournalLine(entry_id=entry_id, account_id=account_id, debit=debit, credit=credit, description=line_desc))
        return entry_id

def delete_manual_journal_entry(db, entry_id):
    with orm_session_scope() as session:
        row = session.get(JournalEntry, entry_id)
        if not row:
            raise ValueError("القيد غير موجود.")
        if row.source_type != "manual":
            raise ValueError("القيد الآلي لا يمكن حذفه من هذه الشاشة.")
        session.execute(delete(JournalLine).where(JournalLine.entry_id == entry_id))
        session.delete(row)
        return True

def find_main_account_direct_balances(db):
    """إظهار الحسابات الرئيسية التي ما زالت تحتوي قيودًا مباشرة.
    نستخدم هذا فقط كتنبيه محاسبي حتى لا يختفي الأثر القديم بصمت.
    """
    rows = db.execute(
        """
        SELECT a.id, a.code, a.name, a.node_type, a.is_postable,
               COALESCE(SUM(jl.debit), 0) AS debit,
               COALESCE(SUM(jl.credit), 0) AS credit
        FROM account_nodes a
        LEFT JOIN journal_lines jl ON jl.account_id = a.id
        WHERE a.active = 1 AND a.is_postable = 0
        GROUP BY a.id
        HAVING ABS(COALESCE(SUM(jl.debit), 0) - COALESCE(SUM(jl.credit), 0)) > 0.0001
        ORDER BY a.code
        """
    ).fetchall()
    return rows

