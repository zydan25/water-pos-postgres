from __future__ import annotations

from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, inspect, text
from sqlalchemy.orm import Mapped, mapped_column

from models import Base, engine


class WaterUnit(Base):
    __tablename__ = "water_units"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WaterLocation(Base):
    __tablename__ = "water_locations"
    __table_args__ = (UniqueConstraint("unit_id", "parent_id", "name", name="uq_water_location_sibling_name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("water_units.id", ondelete="SET NULL"), nullable=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("water_locations.id", ondelete="SET NULL"), nullable=True)
    code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    location_type: Mapped[str] = mapped_column(String(50), nullable=False, default="منطقة")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class NetworkMeter(Base):
    __tablename__ = "network_meters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    meter_number: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    meter_type: Mapped[str] = mapped_column(String(30), nullable=False, default="main")
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("water_units.id", ondelete="SET NULL"), nullable=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("water_locations.id", ondelete="SET NULL"), nullable=True)
    parent_meter_id: Mapped[int | None] = mapped_column(ForeignKey("network_meters.id", ondelete="SET NULL"), nullable=True)
    initial_reading: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    installed_at: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class NetworkMeterReading(Base):
    __tablename__ = "network_meter_readings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("network_meters.id", ondelete="CASCADE"), nullable=False)
    reading_date: Mapped[str] = mapped_column(String(30), nullable=False)
    reading_value: Mapped[float] = mapped_column(Float, nullable=False)
    previous_reading: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    consumption: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


def ensure_schema():
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        inspector = inspect(conn)
        tables = set(inspector.get_table_names())
        if "subscribers" in tables:
            columns = {c["name"] for c in inspector.get_columns("subscribers")}
            if "location_id" not in columns:
                conn.execute(text("ALTER TABLE subscribers ADD COLUMN location_id INTEGER"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_subscribers_location_id ON subscribers(location_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_water_locations_parent_id ON water_locations(parent_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_water_locations_unit_id ON water_locations(unit_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_network_meters_location_id ON network_meters(location_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_network_meters_parent_meter_id ON network_meters(parent_meter_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_network_meter_readings_meter_date ON network_meter_readings(meter_id, reading_date)"))

        # ترحيل آمن للمواقع النصية القديمة: ننشئ موقعًا بسيطًا لكل قيمة village غير فارغة،
        # ثم نربط المشتركين به دون حذف أو تعديل القيمة القديمة.
        if "water_locations" in tables and "subscribers" in tables:
            rows = conn.execute(text("SELECT DISTINCT TRIM(village) AS village FROM subscribers WHERE village IS NOT NULL AND TRIM(village) <> ''")).fetchall()
            for row in rows:
                name = str(row[0]).strip()
                existing = conn.execute(text("SELECT id FROM water_locations WHERE parent_id IS NULL AND name = :name LIMIT 1"), {"name": name}).first()
                if not existing:
                    conn.execute(text("INSERT INTO water_locations (unit_id,parent_id,code,name,location_type,active,sort_order,created_at) VALUES (NULL,NULL,NULL,:name,'قرية',1,0,CURRENT_TIMESTAMP)"), {"name": name})
            conn.execute(text("UPDATE subscribers SET location_id=(SELECT wl.id FROM water_locations wl WHERE wl.parent_id IS NULL AND wl.name=TRIM(subscribers.village) LIMIT 1) WHERE (location_id IS NULL OR location_id=0) AND village IS NOT NULL AND TRIM(village) <> ''"))
