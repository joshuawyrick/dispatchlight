"""Revenue, driver pay and fuel surcharge for one load, for any rate basis a company uses."""
import math
from db import Lane, Location


def load_qty(lane: Lane, pickup: Location, st: dict, override=None) -> float:
    """Quantity that gets billed: the load's own quantity, the lane's, or the pickup's average — floored at the minimum."""
    q = override or lane.qty_override or pickup.avg_qty or st.get("default_qty") or 0
    floor = lane.min_qty if lane.min_qty is not None else (st.get("min_qty") or 0)
    return max(float(q), float(floor or 0))


def load_weight(lane: Lane, pickup: Location, st: dict) -> float:
    return float(lane.weight_tons or pickup.avg_weight_tons or st.get("default_weight_tons") or 0)


def load_hours(loaded_miles: float, st: dict, pickup: Location, dropoff: Location) -> float:
    """Hours a per-hour lane bills: drive time on the loaded leg plus time at both ends."""
    speed = st.get("avg_speed_mph") or 41
    lm = (pickup.load_minutes or st.get("load_minutes") or 60) + (dropoff.unload_minutes or st.get("unload_minutes") or 60)
    return loaded_miles / speed + lm / 60


def revenue(lane: Lane, pickup: Location, dropoff: Location, loaded_miles: float, st: dict, qty_override=None) -> float:
    rate = lane.rate or 0
    b = lane.rate_basis or "unit"
    if b == "unit": return rate * load_qty(lane, pickup, st, qty_override)
    if b == "load": return rate
    if b == "mile": return rate * loaded_miles
    if b == "hour": return rate * load_hours(loaded_miles, st, pickup, dropoff)
    if b == "ton":  return rate * load_weight(lane, pickup, st)
    return 0.0


def driver_pay(lane: Lane, pickup: Location, dropoff: Location, loaded_miles: float, rev: float, st: dict, qty_override=None) -> float:
    r = lane.pay_rate or 0
    b = lane.pay_basis or "pct"
    if b == "pct":  return rev * r / 100.0
    if b == "unit": return r * load_qty(lane, pickup, st, qty_override)
    if b == "mile": return r * loaded_miles
    if b == "hour": return r * load_hours(loaded_miles, st, pickup, dropoff)
    if b == "load": return r
    return 0.0


def fsc_fraction(st: dict, fuel_price: float | None) -> float:
    """Surcharge as a fraction of freight for the step / flat rules (0.036 = 3.6%). Mile rule is handled separately."""
    mode = (st.get("fsc_mode") or "none")
    if mode == "flat": return (st.get("fsc_flat_pct") or 0) / 100.0
    if mode == "step":
        base, step, pct = st.get("fsc_base_price") or 0, st.get("fsc_step_price") or 0.10, (st.get("fsc_step_pct") or 0) / 100.0
        if not fuel_price or fuel_price <= base or step <= 0: return 0.0
        steps = math.floor((fuel_price - base) / step + 1e-9) + 1
        return round(steps * pct, 6)
    return 0.0


def surcharge(rev: float, loaded_miles: float, st: dict, fuel_price: float | None) -> float:
    if (st.get("fsc_mode") or "none") == "mile": return (st.get("fsc_per_mile") or 0) * loaded_miles
    return rev * fsc_fraction(st, fuel_price)
