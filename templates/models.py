from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker, scoped_session

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "instance" / "water_billing.sqlite3"
ENGINE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    ENGINE_URL,
    future=True,
    connect_args={"check_same_thread": False},
)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True))


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="Collector")
    wallet_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=lambda: datetime.now().isoformat())
    updated_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class Subscriber(Base):
    __tablename__ = "subscribers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    meter_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    village: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    address: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    default_unit_price: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    default_subscription_fee: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_reading: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_due_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_paid_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    updated_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    invoices: Mapped[list["Invoice"]] = relationship(back_populates="subscriber")


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint("COALESCE(previous_reading, 0) >= 0", name="ck_invoice_previous_reading_nonnegative"),
        CheckConstraint("COALESCE(current_reading, 0) >= 0", name="ck_invoice_current_reading_nonnegative"),
        CheckConstraint("COALESCE(consumption, 0) >= 0", name="ck_invoice_consumption_nonnegative"),
        CheckConstraint("COALESCE(unit_price, 0) >= 0", name="ck_invoice_unit_price_nonnegative"),
        CheckConstraint("COALESCE(consumption_amount, 0) >= 0", name="ck_invoice_consumption_amount_nonnegative"),
        CheckConstraint("COALESCE(subscription_fee, 0) >= 0", name="ck_invoice_subscription_fee_nonnegative"),
        CheckConstraint("COALESCE(other_charges, 0) >= 0", name="ck_invoice_other_charges_nonnegative"),
        CheckConstraint("COALESCE(opening_balance, 0) >= 0", name="ck_invoice_opening_balance_nonnegative"),
        CheckConstraint("COALESCE(total_amount, 0) >= 0", name="ck_invoice_total_amount_nonnegative"),
        CheckConstraint("COALESCE(paid_amount, 0) >= 0", name="ck_invoice_paid_amount_nonnegative"),
        CheckConstraint("COALESCE(remaining_amount, 0) >= 0", name="ck_invoice_remaining_amount_nonnegative"),
        CheckConstraint("COALESCE(credit_amount, 0) >= 0", name="ck_invoice_credit_amount_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_no: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    subscriber_id: Mapped[Optional[int]] = mapped_column(ForeignKey("subscribers.id"))
    invoice_date: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    month_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    previous_reading: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    current_reading: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    consumption: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    consumption_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    subscription_fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    other_charges: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    opening_balance: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    credit_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    paid_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    remaining_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    printed_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    printed_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sent_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sent_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sent_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)
    last_sent_channel: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    updated_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    subscriber: Mapped[Optional[Subscriber]] = relationship(back_populates="invoices")
    payments: Mapped[list["Payment"]] = relationship(back_populates="invoice")


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payment_amount_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[Optional[int]] = mapped_column(ForeignKey("invoices.id"))
    subscriber_id: Mapped[Optional[int]] = mapped_column(Integer)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    payment_date: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    method: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attachment_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    invoice: Mapped[Optional[Invoice]] = relationship(back_populates="payments")


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_wallet_balance_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    balance: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=lambda: datetime.now().isoformat())


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_transaction_amount_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    subscriber_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reference_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=lambda: datetime.now().isoformat())


class AccountNode(Base):
    __tablename__ = "account_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    node_type: Mapped[str] = mapped_column(String, nullable=False, default="account")
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("account_nodes.id"), nullable=True)
    is_postable: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=lambda: datetime.now().isoformat())
    updated_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    parent: Mapped[Optional["AccountNode"]] = relationship(remote_side=[id], backref="children")


class EmployeeProfile(Base):
    __tablename__ = "employee_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    account_node_id: Mapped[Optional[int]] = mapped_column(ForeignKey("account_nodes.id"), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    job_title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    monthly_target: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=lambda: datetime.now().isoformat())
    updated_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_no: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    entry_date: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=lambda: datetime.now().isoformat())

    lines: Mapped[list["JournalLine"]] = relationship(back_populates="entry", cascade="all, delete-orphan")


class JournalLine(Base):
    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0", name="ck_journal_line_debit_nonnegative"),
        CheckConstraint("credit >= 0", name="ck_journal_line_credit_nonnegative"),
        CheckConstraint("(debit = 0) OR (credit = 0)", name="ck_journal_line_one_sided"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("account_nodes.id"), nullable=False)
    debit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    credit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[AccountNode] = relationship()



class Settings(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[Optional[int]] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    filename: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    uploaded_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class UserWallet(Base):
    __tablename__ = "user_wallets"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id", ondelete="CASCADE"), primary_key=True)


class AccountingVoucher(Base):
    __tablename__ = "accounting_vouchers"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_voucher_amount_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    voucher_no: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    voucher_type: Mapped[str] = mapped_column(String, nullable=False)
    voucher_date: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    from_account_id: Mapped[int] = mapped_column(ForeignKey("account_nodes.id", ondelete="RESTRICT"), nullable=False)
    to_account_id: Mapped[int] = mapped_column(ForeignKey("account_nodes.id", ondelete="RESTRICT"), nullable=False)
    reference: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    source_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    journal_entry_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="posted")
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=lambda: datetime.now().isoformat())
    updated_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class CollectionTrip(Base):
    __tablename__ = "collection_trips"
    __table_args__ = (
        CheckConstraint("COALESCE(target_amount, 0) >= 0", name="ck_collection_trip_target_nonnegative"),
        CheckConstraint("COALESCE(collected_amount, 0) >= 0", name="ck_collection_trip_collected_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collector_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    trip_date: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")
    target_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    collected_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    closed_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=lambda: datetime.now().isoformat())


class TripVisit(Base):
    __tablename__ = "trip_visits"
    __table_args__ = (
        CheckConstraint("COALESCE(amount_collected, 0) >= 0", name="ck_trip_visit_amount_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("collection_trips.id", ondelete="CASCADE"), nullable=False)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    amount_collected: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    visited_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=lambda: datetime.now().isoformat())


class BulkReading(Base):
    __tablename__ = "bulk_readings"
    __table_args__ = (
        CheckConstraint("COALESCE(previous_reading, 0) >= 0", name="ck_bulk_reading_previous_nonnegative"),
        CheckConstraint("current_reading >= 0", name="ck_bulk_reading_current_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id"), nullable=False)
    previous_reading: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    current_reading: Mapped[float] = mapped_column(Float, nullable=False)
    reading_date: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    month_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    invoiced: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    updated_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    table_name: Mapped[str] = mapped_column(String, nullable=False)
    row_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[str] = mapped_column(String, nullable=False, default=lambda: datetime.now().isoformat())

@contextmanager
def session_scope() -> Iterable[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_accounting() -> None:
    Base.metadata.create_all(bind=engine)
    with session_scope() as session:
        if session.scalar(select(func.count(AccountNode.id))) == 0:
            seeds = [
                ("1000", "الأصول", "root", None, 0),
                ("1100", "الصناديق", "cash", None, 0),
                ("1200", "العملاء", "receivable", None, 0),
                ("1300", "الموظفون", "employee", None, 0),
                ("2000", "الخصوم", "root", None, 0),
                ("2100", "الموردون", "payable", None, 0),
                ("3000", "حقوق الملكية", "root", None, 0),
                ("4000", "الإيرادات", "root", None, 0),
                ("4100", "إيرادات المياه", "income", 4000, 1),
                ("5000", "المصروفات", "root", None, 0),
                ("5100", "مصروفات تشغيل", "expense", 5000, 1),
            ]
            for code, name, node_type, parent_code, postable in seeds:
                parent_id = None
                if parent_code:
                    parent = session.scalar(select(AccountNode).where(AccountNode.code == str(parent_code)))
                    parent_id = parent.id if parent else None
                session.add(AccountNode(code=code, name=name, node_type=node_type, parent_id=parent_id, is_postable=postable))


def account_tree(session: Session):
    nodes = session.scalars(select(AccountNode).where(AccountNode.active == 1).order_by(AccountNode.code)).all()
    by_parent = {}
    for node in nodes:
        by_parent.setdefault(node.parent_id, []).append(node)
    for items in by_parent.values():
        items.sort(key=lambda n: n.code)

    def build(parent_id=None):
        out = []
        for node in by_parent.get(parent_id, []):
            out.append({
                "node": node,
                "children": build(node.id),
            })
        return out

    return build(None)


def find_account(session: Session, code: str) -> Optional[AccountNode]:
    return session.scalar(select(AccountNode).where(AccountNode.code == code))


def record_journal_entry(session: Session, *, entry_date: str, source_type: str, source_id: Optional[int], description: str, created_by: Optional[int], lines: list[tuple[int, float, float, str]]) -> JournalEntry:
    entry = JournalEntry(
        entry_no=f"JE-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
        entry_date=entry_date,
        source_type=source_type,
        source_id=source_id,
        description=description,
        created_by=created_by,
    )
    session.add(entry)
    session.flush()
    for account_id, debit, credit, line_desc in lines:
        session.add(JournalLine(entry_id=entry.id, account_id=account_id, debit=debit, credit=credit, description=line_desc))
    return entry
