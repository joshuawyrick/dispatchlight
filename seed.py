"""Company defaults, first-company creation, and the Petrol Transport starter data set."""
import csv, os
from db import SessionLocal, Company, Setting, Location, Lane, FuelType, init_db

# key, default number, default text, label, unit, note, group
SETTINGS = [
    # operations
    ("avg_speed_mph", 41, None, "Average truck speed", "mph", "Used with road miles to estimate drive time", "Operations"),
    ("load_minutes", 60, None, "Time at a pickup", "minutes", "Default; can be set per location", "Operations"),
    ("unload_minutes", 60, None, "Time at a drop-off", "minutes", "Default; can be set per location", "Operations"),
    ("earliest_start_hour", 5, None, "Earliest shift start", "hour (0–23)", "Slots may leave the yard any time from this hour on; the plan recommends a start per slot", "Operations"),
    ("target_per_hour", 135, None, "Target gross per shift hour", "$/hour", "Yard-out to yard-in; shifts below this are flagged", "Operations"),
    ("shift_fixed_cost", 0, None, "Fixed cost per shift", "$", "Anything paid per shift regardless of loads (inspection pay, per-diem)", "Operations"),
    # quantities
    ("qty_unit", None, "bbl", "Quantity unit", "text", "bbl, gal, tons, loads — the unit your per-unit lanes are priced in", "Quantities"),
    ("min_qty", 150, None, "Minimum billable quantity", "units", "A smaller load is billed (and paid) as this much; blank = none", "Quantities"),
    ("default_qty", 150, None, "Default quantity per load", "units", "Used when a pickup has no average entered", "Quantities"),
    ("default_weight_tons", 25, None, "Default load weight", "tons", "For per-ton lanes without a weight", "Quantities"),
    # hours of service
    ("max_drive_hours", 10, None, "Max driving hours per shift", "hours", "California intrastate: 12; US federal: 11; Petrol policy: 10", "Hours of service"),
    ("max_duty_hours", 16, None, "Max on-duty hours per shift", "hours", "Yard-out to yard-in. California: 16; federal: 14", "Hours of service"),
    ("drive_buffer_hours", 0.5, None, "Driving safety buffer", "hours", "Plans stay this far under the driving limit", "Hours of service"),
    ("duty_buffer_hours", 0.5, None, "On-duty safety buffer", "hours", "Plans stay this far under the on-duty limit", "Hours of service"),
    # surcharge
    ("fsc_mode", None, "step", "Fuel surcharge rule", "text", "none · step (x% per price step above a base) · flat (fixed %) · mile ($ per loaded mile)", "Fuel surcharge"),
    ("fsc_base_price", 5.50, None, "Surcharge base fuel price", "$/gal", "No surcharge at or below this price (step rule)", "Fuel surcharge"),
    ("fsc_step_price", 0.10, None, "Surcharge price step", "$/gal", "Each step above the base adds one increment (step rule)", "Fuel surcharge"),
    ("fsc_step_pct", 0.6, None, "Surcharge per step", "% of freight", "Step rule increment", "Fuel surcharge"),
    ("fsc_flat_pct", 0, None, "Flat surcharge", "% of freight", "Flat rule", "Fuel surcharge"),
    ("fsc_per_mile", 0, None, "Surcharge per loaded mile", "$/mile", "Mile rule", "Fuel surcharge"),
    ("fsc_in_profit", 0, None, "Count surcharge as profit when optimizing", "1 = yes, 0 = no", "Off: surcharge is shown but does not steer the plan", "Fuel surcharge"),
    ("fsc_to_subs", 1, None, "Pass the whole surcharge to sub-haulers", "1 = yes, 0 = no", "", "Fuel surcharge"),
    # optimizer
    ("solver_seconds", 20, None, "Optimizer thinking time", "seconds", "10–60; longer = slightly better plans", "Optimizer"),
    ("loaded_mile_weight", 0, None, "Loaded-mile preference", "$ per empty mile", "Extra cost the optimizer charges an empty mile beyond fuel. 0 = pure profit; 1–3 pushes toward fuller shifts", "Optimizer"),
    ("priority_step", 30, None, "Company-first strength", "% of load profit", "How much worse a load on a sub-hauler slot counts vs your own truck (per tier)", "Optimizer"),
]
SETTING_GROUPS = ["Operations", "Quantities", "Hours of service", "Fuel surcharge", "Optimizer"]


def ensure_settings(s, company_id: int):
    have = {x.key: x for x in s.query(Setting).filter(Setting.company_id == company_id).all()}
    n = 0
    for key, val, txt, *_ in SETTINGS:
        if key not in have:
            s.add(Setting(company_id=company_id, key=key, value=val, text=txt)); n += 1
    if n: s.commit()


def settings_map(s, company_id: int) -> dict:
    """{key: number or text} with defaults filled in."""
    out = {key: (txt if val is None else val) for key, val, txt, *_ in SETTINGS}
    for x in s.query(Setting).filter(Setting.company_id == company_id).all():
        out[x.key] = x.text if x.text is not None and x.value is None else x.value
    return out


def ensure_company(s) -> Company:
    c = s.query(Company).order_by(Company.id).first()
    if not c:
        c = Company(name=os.environ.get("COMPANY_NAME", "My Trucking Company"))
        s.add(c); s.commit()
    ensure_settings(s, c.id)
    if not s.query(FuelType).filter(FuelType.company_id == c.id).count():
        s.add(FuelType(company_id=c.id, name="Diesel", kind="diesel", economy=5.5, price=5.90, is_default=True))
        s.commit()
    return c


def load_petrol(s, company_id: int) -> str:
    """Starter data: Petrol Transport's locations and per-barrel lanes (customer #1)."""
    if s.query(Location).filter(Location.company_id == company_id).count():
        return "This company already has locations — starter data not loaded."
    by_name = {}
    with open("data/locations.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            loc = Location(company_id=company_id, name=r["name"].strip(), kind=r["kind"], lat=float(r["lat"]), lon=float(r["lon"]),
                           avg_qty=float(r["avg_bbl_history"]) if r.get("avg_bbl_history") else None, notes=r.get("notes") or None)
            s.add(loc); by_name[loc.name] = loc
    s.flush()
    n = 0
    with open("data/lanes.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            rate = float(r["rate"]) if r["rate"] else None
            pay = float(r["driver_pay"]) if r["driver_pay"] else None
            s.add(Lane(company_id=company_id, customer=r["account"] or None, pickup_id=by_name[r["pickup"].strip()].id,
                       dropoff_id=by_name[r["dropoff"].strip()].id, rate_basis="unit", rate=rate,
                       pay_basis="unit", pay_rate=pay, product="crude oil", notes=r.get("notes") or None)); n += 1
    s.commit()
    return f"Loaded {len(by_name)} locations and {n} lanes (per-barrel, Petrol Transport starter set)."


def bootstrap():
    init_db()
    with SessionLocal() as s:
        c = ensure_company(s)
        if os.environ.get("SEED_PETROL") == "1" and not s.query(Location).filter(Location.company_id == c.id).count():
            print(load_petrol(s, c.id))
        return c.id
