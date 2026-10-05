"""Data model for the Shift Planner product.

Everything belongs to a Company (one account per trucking company). The first company is created automatically;
sign-ups, users and billing come later, but every table already carries company_id so nothing has to move.
"""
import os
from datetime import datetime
from sqlalchemy import (create_engine, Column, Integer, Float, String, Boolean, DateTime, ForeignKey,
                        UniqueConstraint, Text, LargeBinary)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///data/planner.db")
for prefix in ("postgres://", "postgresql://"):
    if DB_URL.startswith(prefix):
        DB_URL = "postgresql+psycopg://" + DB_URL[len(prefix):]
        break

engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {}, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()

RATE_BASES = [("unit", "per unit (bbl / gal / ton …)"), ("load", "flat per load"), ("mile", "per loaded mile"),
              ("hour", "per hour"), ("ton", "per ton (avg weight)")]
PAY_BASES = [("pct", "% of load revenue"), ("unit", "per unit"), ("mile", "per loaded mile"), ("hour", "per hour"), ("load", "per load")]
FUEL_KINDS = [("diesel", "Diesel (mpg, $/gal)"), ("cng", "CNG (mi per GGE, $/GGE)"), ("electric", "Electric (mi per kWh, $/kWh)"), ("other", "Other")]


class Company(Base):
    """A customer of the product. Company #1 is created on first start."""
    __tablename__ = "companies"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    address = Column(String(200))
    phone = Column(String(60))
    email = Column(String(120))
    website = Column(String(120))
    logo = Column(LargeBinary)
    logo_mime = Column(String(60))
    created_at = Column(DateTime, default=datetime.utcnow)


class Setting(Base):
    """Per-company settings: numbers (value) or text (text)."""
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("company_id", "key", name="uq_setting"),)
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    key = Column(String(60), nullable=False)
    value = Column(Float)
    text = Column(String(200))


class Location(Base):
    """Pickup, drop-off, both, or a yard (where trucks start and end their shift)."""
    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_location"),)
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(120), nullable=False)
    kind = Column(String(10), nullable=False)          # pickup | dropoff | both | yard
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    active = Column(Boolean, default=True)
    avg_qty = Column(Float)                            # average quantity per load picked up here (bbl, gal, tons ...)
    avg_weight_tons = Column(Float)                    # for per-ton lanes
    load_minutes = Column(Integer)                     # None = company default
    unload_minutes = Column(Integer)
    open_time = Column(String(5))
    close_time = Column(String(5))
    notes = Column(Text)


class Lane(Base):
    """Pickup -> drop-off with how it is billed and how the driver is paid."""
    __tablename__ = "lanes"
    __table_args__ = (UniqueConstraint("company_id", "customer", "pickup_id", "dropoff_id", name="uq_lane"),)
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    customer = Column(String(120))
    product = Column(String(60))
    pickup_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    dropoff_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    rate_basis = Column(String(10), default="unit")    # unit | load | mile | hour | ton
    rate = Column(Float)                               # $ per basis
    min_qty = Column(Float)                            # billing floor for unit lanes (None = company default)
    qty_override = Column(Float)                       # force a quantity for this lane (None = pickup average)
    weight_tons = Column(Float)                        # for ton lanes (None = pickup avg weight / company default)
    pay_basis = Column(String(10), default="pct")      # pct | unit | mile | hour | load
    pay_rate = Column(Float)                           # % or $ per basis
    active = Column(Boolean, default=True)
    notes = Column(Text)
    pickup = relationship("Location", foreign_keys=[pickup_id])
    dropoff = relationship("Location", foreign_keys=[dropoff_id])


class Distance(Base):
    """Road miles between two locations (one direction); an approved/override route always wins."""
    __tablename__ = "distances"
    __table_args__ = (UniqueConstraint("company_id", "origin_id", "dest_id", name="uq_dist"),)
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    origin_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    dest_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    google_miles = Column(Float)
    google_minutes = Column(Float)
    override_miles = Column(Float)
    override_minutes = Column(Float)
    override_note = Column(String(200))
    source = Column(String(30))                        # google | manual | straight-line | import
    fetched_at = Column(DateTime)
    approved = Column(Boolean, default=False)
    approved_at = Column(DateTime)
    route_kind = Column(String(20))
    via_json = Column(Text)
    polyline = Column(Text)
    origin = relationship("Location", foreign_keys=[origin_id])
    dest = relationship("Location", foreign_keys=[dest_id])

    @property
    def miles(self):
        return self.override_miles if self.override_miles else self.google_miles


class FuelType(Base):
    """Diesel / CNG / electric with the fleet's average economy and the current energy price."""
    __tablename__ = "fuel_types"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(40), nullable=False)
    kind = Column(String(12), default="diesel")
    economy = Column(Float)                            # miles per gallon / GGE / kWh
    price = Column(Float)                              # $ per gallon / GGE / kWh
    is_default = Column(Boolean, default=False)
    active = Column(Boolean, default=True)

    @property
    def unit(self):
        return {"diesel": "gal", "cng": "GGE", "electric": "kWh"}.get(self.kind, "unit")

    def cost_per_mile(self):
        return (self.price or 0) / self.economy if self.economy else 0.0


class Plan(Base):
    """One planning run: the inputs (loads + slot groups) and the result, both as JSON so a plan can be reopened."""
    __tablename__ = "plans"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(120))
    plan_date = Column(String(10))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    inputs_json = Column(Text)                         # {"loads": [...], "groups": [...]}
    result_json = Column(Text)                         # engine output, possibly edited by the dispatcher
    status = Column(String(12), default="draft")       # draft | built | final
    summary = Column(String(300))


def init_db():
    """Create tables and add any columns introduced by newer versions (simple forward migration)."""
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(engine)
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}'))
