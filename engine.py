"""The planning engine: given today's loads and a pool of driver slots per yard, build the most profitable set of shifts.

Model (Google OR-Tools routing solver)
  * a "slot" is an anonymous driver/truck starting and ending at a yard, with drive / on-duty caps (a slot group is
    N identical slots; sub-hauler groups carry the share of load revenue the sub keeps)
  * each load = pickup node + drop-off node served by the same slot in order; one load on the truck at a time
  * clocks per slot: on-duty minutes (drive + load + unload + waiting) and driving minutes, each capped; a slot may
    start any time in the day and must be back at its yard inside both caps
  * site open/close windows are honored (waiting allowed)
  * objective: maximize profit = revenue - driver pay - fuel - fixed cost; leaving a load unhauled costs its profit;
    loaded-mile preference adds a cost per empty mile; company slots fill before sub slots via a per-tier cost
  * surcharge is reported alongside (and optionally counted as profit)
"""
import json
from datetime import datetime
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from sqlalchemy.orm import Session, joinedload
from db import Location, Lane, Distance, FuelType, Plan
import money
from mileage import haversine_miles, STRAIGHT_LINE_FACTOR

HORIZON_MIN = 48 * 60
URGENCY_BONUS = 5_000        # cents: small nudge so a load is hauled rather than dropped at equal profit


def hm_to_min(hm):
    if not hm: return None
    h, m = hm.split(":")[:2]
    return int(h) * 60 + int(m)


def min_to_hm(m):
    m = int(round(m)); d, r = divmod(m, 1440)
    return f"{r // 60:02d}:{r % 60:02d}" + ("" if d == 0 else f" +{d}d")


class Inputs:
    """Everything the solver needs for one company, one plan: pulled once from the database + the plan's inputs."""

    def __init__(self, s: Session, cid: int, st: dict, inputs: dict):
        self.st = st
        self.speed = st.get("avg_speed_mph") or 41
        self.load_min = int(st.get("load_minutes") or 60)
        self.unload_min = int(st.get("unload_minutes") or 60)
        self.drive_cap = int(((st.get("max_drive_hours") or 10) - (st.get("drive_buffer_hours") or 0)) * 60)
        self.duty_cap = int(((st.get("max_duty_hours") or 16) - (st.get("duty_buffer_hours") or 0)) * 60)
        self.fixed = float(st.get("shift_fixed_cost") or 0)
        self.target = st.get("target_per_hour") or 135
        self.empty_mile_cost = float(st.get("loaded_mile_weight") or 0)
        self.priority_step = (st.get("priority_step") if st.get("priority_step") is not None else 30) / 100.0
        self.fsc_in_profit = bool(st.get("fsc_in_profit"))
        self.fsc_to_subs = bool(st.get("fsc_to_subs") if st.get("fsc_to_subs") is not None else 1)
        self.earliest = int((st.get("earliest_start_hour") if st.get("earliest_start_hour") is not None else 5) * 60)

        self.locs = {l.id: l for l in s.query(Location).filter(Location.company_id == cid).all()}
        self.fuels = {f.id: f for f in s.query(FuelType).filter(FuelType.company_id == cid).all()}
        self.default_fuel = next((f for f in self.fuels.values() if f.is_default), next(iter(self.fuels.values()), None))
        self.fuel_price = self.default_fuel.price if self.default_fuel else None
        self.dist = {}
        for d in s.query(Distance).filter(Distance.company_id == cid).all():
            m = d.override_miles if d.override_miles else d.google_miles
            if m is not None: self.dist[(d.origin_id, d.dest_id)] = m

        # slots
        self.slots = []
        sub_shares = sorted({float(g.get("share") or 0) for g in inputs.get("groups", []) if g.get("kind") == "sub"})
        for gi, g in enumerate(inputs.get("groups", [])):
            yard = self.locs.get(int(g.get("yard_id") or 0))
            n = int(g.get("count") or 0)
            if not yard or n <= 0: continue
            duty = int(min(self.duty_cap, float(g.get("max_duty") or 99) * 60))
            drive = int(min(self.drive_cap, float(g.get("max_drive") or 99) * 60))
            fuel = self.fuels.get(int(g.get("fuel_id") or 0)) or self.default_fuel
            is_sub = g.get("kind") == "sub"
            share = float(g.get("share") or 0) if is_sub else 0.0
            tier = 1 if not is_sub else 2 + sub_shares.index(share)
            label = g.get("label") or (f"{yard.name} sub {int(share)}%" if is_sub else yard.name)
            for k in range(n):
                self.slots.append(dict(group=gi, label=f"{label} #{k + 1}", yard=yard, duty_cap=duty, drive_cap=drive, fuel=fuel,
                                       is_sub=is_sub, share=share, tier=tier, cpm=(fuel.cost_per_mile() if fuel else 0.0)))

        # loads
        lanes = {l.id: l for l in s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff)).filter(Lane.company_id == cid).all()}
        self.loads = []
        for row in inputs.get("loads", []):
            lane = lanes.get(int(row.get("lane_id") or 0))
            if not lane or not lane.rate: continue
            qty_override = float(row["qty"]) if row.get("qty") else None
            lm = self.miles(lane.pickup_id, lane.dropoff_id)
            rev = money.revenue(lane, lane.pickup, lane.dropoff, lm, st, qty_override)
            pay = money.driver_pay(lane, lane.pickup, lane.dropoff, lm, rev, st, qty_override)
            fsc = money.surcharge(rev, lm, st, self.fuel_price)
            qty = money.load_qty(lane, lane.pickup, st, qty_override) if lane.rate_basis == "unit" else (qty_override or lane.pickup.avg_qty or 0)
            for k in range(int(row.get("count") or 1)):
                self.loads.append(dict(lane=lane, unit=k + 1, rev=rev, pay=pay, fsc=fsc, qty=qty, loaded_miles=lm,
                                       earliest=hm_to_min(row.get("earliest")), latest=hm_to_min(row.get("latest"))))

    def miles(self, a, b):
        if a == b: return 0.0
        m = self.dist.get((a, b))
        if m is None:
            m = round(haversine_miles(self.locs[a], self.locs[b]) * STRAIGHT_LINE_FACTOR, 1)
            self.dist[(a, b)] = m
        return m

    def drive_min(self, a, b):
        return int(round(self.miles(a, b) / self.speed * 60))

    def service_min(self, loc, kind):
        return int((loc.load_minutes if kind == "pickup" else loc.unload_minutes) or (self.load_min if kind == "pickup" else self.unload_min))

    def sub_pay(self, slot, L):
        return L["rev"] * slot["share"] / 100.0 if slot["is_sub"] else 0.0


def solve(s: Session, cid: int, st: dict, inputs: dict, time_limit_s: int | None = None) -> dict:
    inp = Inputs(s, cid, st, inputs)
    time_limit_s = int(time_limit_s or st.get("solver_seconds") or 20)
    if not inp.slots: return dict(ok=False, error="Add at least one driver slot (a yard and a number of drivers).")
    if not inp.loads: return dict(ok=False, error="Add at least one load on a lane that has a rate.")

    yards = []
    for v in inp.slots:
        if v["yard"].id not in [y.id for y in yards]: yards.append(v["yard"])
    yard_node = {y.id: i for i, y in enumerate(yards)}
    nodes = [dict(loc=y, kind="yard", load=None) for y in yards]
    for L in inp.loads:
        L["p_node"] = len(nodes); nodes.append(dict(loc=L["lane"].pickup, kind="pickup", load=L))
        L["d_node"] = len(nodes); nodes.append(dict(loc=L["lane"].dropoff, kind="dropoff", load=L))
    n_nodes, n_veh = len(nodes), len(inp.slots)
    starts = [yard_node[v["yard"].id] for v in inp.slots]
    mgr = pywrapcp.RoutingIndexManager(n_nodes, n_veh, starts, starts)
    routing = pywrapcp.RoutingModel(mgr)
    solver = routing.solver()

    loc_of = [nd["loc"] for nd in nodes]
    def node_service(i):
        nd = nodes[i]
        return 0 if nd["kind"] == "yard" else inp.service_min(nd["loc"], nd["kind"])
    drive_mat = [[inp.drive_min(loc_of[i].id, loc_of[j].id) for j in range(n_nodes)] for i in range(n_nodes)]
    miles_mat = [[inp.miles(loc_of[i].id, loc_of[j].id) for j in range(n_nodes)] for i in range(n_nodes)]
    loaded_arc = [[nodes[j]["kind"] == "dropoff" for j in range(n_nodes)] for i in range(n_nodes)]   # arriving at a drop-off = loaded leg

    def time_cb(fi, ti):
        i, j = mgr.IndexToNode(fi), mgr.IndexToNode(ti); return drive_mat[i][j] + node_service(i)
    def drive_cb(fi, ti):
        i, j = mgr.IndexToNode(fi), mgr.IndexToNode(ti); return drive_mat[i][j]
    def demand_cb(fi):
        k = nodes[mgr.IndexToNode(fi)]["kind"]; return 1 if k == "pickup" else (-1 if k == "dropoff" else 0)
    time_idx = routing.RegisterTransitCallback(time_cb)
    drive_idx = routing.RegisterTransitCallback(drive_cb)
    demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)

    # per-slot arc cost: fuel (own trucks) + empty-mile preference + margin given up on a sub slot + tier step
    def make_cb(v):
        slot = inp.slots[v]
        extra = [0] * n_nodes
        tier_frac = min(0.9, inp.priority_step * (slot["tier"] - 1))
        for L in inp.loads:
            profit_c = max(0, int(round((L["rev"] - L["pay"]) * 100)))
            margin_c = max(0, int(round((inp.sub_pay(slot, L) - L["pay"]) * 100))) if slot["is_sub"] else 0
            extra[L["d_node"]] = min(profit_c, max(margin_c, int(tier_frac * profit_c)))
        cpm = 0.0 if slot["is_sub"] else slot["cpm"]
        def cb(fi, ti):
            i, j = mgr.IndexToNode(fi), mgr.IndexToNode(ti)
            c = miles_mat[i][j] * cpm
            if not loaded_arc[i][j]: c += miles_mat[i][j] * inp.empty_mile_cost
            return int(round(c * 100)) + extra[j]
        return cb
    for v in range(n_veh):
        routing.SetArcCostEvaluatorOfVehicle(routing.RegisterTransitCallback(make_cb(v)), v)

    routing.AddDimension(time_idx, 8 * 60, HORIZON_MIN, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    routing.AddDimension(drive_idx, 0, 24 * 60, True, "Drive")
    drive_dim = routing.GetDimensionOrDie("Drive")
    routing.AddDimensionWithVehicleCapacity(demand_idx, 0, [1] * n_veh, True, "Load")

    for v, slot in enumerate(inp.slots):
        st_i, en_i = routing.Start(v), routing.End(v)
        time_dim.CumulVar(st_i).SetRange(inp.earliest, inp.earliest + 18 * 60)   # a slot may leave any time from the earliest start
        time_dim.SetSpanUpperBoundForVehicle(slot["duty_cap"], v)
        drive_dim.SetSpanUpperBoundForVehicle(slot["drive_cap"], v)
        routing.SetFixedCostOfVehicle(0 if slot["is_sub"] else int(inp.fixed * 100), v)
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(en_i))
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(st_i))
    # identical slots in a group: use lower-numbered ones first (symmetry breaking keeps the search fast)
    by_group = {}
    for v, slot in enumerate(inp.slots): by_group.setdefault(slot["group"], []).append(v)
    for vs in by_group.values():
        for a, b in zip(vs, vs[1:]):
            solver.Add(routing.ActiveVehicleVar(b) <= routing.ActiveVehicleVar(a))

    for L in inp.loads:
        pi, di = mgr.NodeToIndex(L["p_node"]), mgr.NodeToIndex(L["d_node"])
        routing.AddPickupAndDelivery(pi, di)
        solver.Add(routing.VehicleVar(pi) == routing.VehicleVar(di))
        solver.Add(time_dim.CumulVar(pi) <= time_dim.CumulVar(di))
        profit_c = int(round((L["rev"] - L["pay"] + (L["fsc"] if inp.fsc_in_profit else 0)) * 100))
        routing.AddDisjunction([pi], max(profit_c + URGENCY_BONUS, 1))
        routing.AddDisjunction([di], 0)
        if L["earliest"] is not None or L["latest"] is not None:
            time_dim.CumulVar(pi).SetRange(L["earliest"] or 0, L["latest"] if L["latest"] is not None else 1440)
    for nd_i, nd in enumerate(nodes):
        if nd["kind"] == "yard": continue
        o, c = hm_to_min(nd["loc"].open_time), hm_to_min(nd["loc"].close_time)
        if o is None and c is None: continue
        o = o or 0; c = c if c is not None else 1439
        var = time_dim.CumulVar(mgr.NodeToIndex(nd_i))
        if o <= c:
            if o > 0: var.RemoveInterval(0, o - 1)
            var.RemoveInterval(c + 1, o + 1440 - 1)
            var.RemoveInterval(c + 1440 + 1, HORIZON_MIN)
        else:
            var.RemoveInterval(c + 1, o - 1); var.RemoveInterval(c + 1440 + 1, o + 1440 - 1)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(max(3, time_limit_s))
    sol = routing.SolveWithParameters(params)
    if sol is None: return dict(ok=False, error="No feasible plan found — check site windows and hour limits.")

    # ---- read the solution into plain shift/stop records (the same shape the editor recomputes later) ----
    shifts, assigned = [], set()
    for v, slot in enumerate(inp.slots):
        idx = routing.Start(v); stops = []; prev = mgr.IndexToNode(idx)
        t0 = sol.Value(time_dim.CumulVar(idx))
        stops.append(dict(kind="yard", loc_id=slot["yard"].id, name=slot["yard"].name))
        idx = sol.Value(routing.NextVar(idx))
        while not routing.IsEnd(idx):
            node = mgr.IndexToNode(idx); nd = nodes[node]; L = nd["load"]
            stops.append(dict(kind=nd["kind"], loc_id=nd["loc"].id, name=nd["loc"].name, load_key=f'{L["lane"].id}-{L["unit"]}'))
            if nd["kind"] == "dropoff": assigned.add(id(L))
            prev = node; idx = sol.Value(routing.NextVar(idx))
        stops.append(dict(kind="yard", loc_id=slot["yard"].id, name=slot["yard"].name))
        shifts.append(dict(slot=slot["label"], group=slot["group"], yard=slot["yard"].name, yard_id=slot["yard"].id, is_sub=slot["is_sub"],
                           share=slot["share"], tier=slot["tier"], fuel_name=slot["fuel"].name if slot["fuel"] else "", cpm=slot["cpm"],
                           duty_cap_h=slot["duty_cap"] / 60, drive_cap_h=slot["drive_cap"] / 60, start=t0, stops=stops, driver="", start_override=None))
    unassigned = [dict(load_key=f'{L["lane"].id}-{L["unit"]}', lane=f'{L["lane"].pickup.name} → {L["lane"].dropoff.name}',
                       customer=L["lane"].customer or "", rev=round(L["rev"], 2), profit=round(L["rev"] - L["pay"], 2), qty=L["qty"])
                  for L in inp.loads if id(L) not in assigned]
    result = dict(ok=True, generated_at=datetime.utcnow().isoformat(timespec="seconds"), shifts=shifts, unassigned=unassigned)
    return recompute(inp, result)


# ---------------------------------------------------------------------------------------------------------------
# Recompute: turn an ordered list of stops per shift into times, hours, miles, money and rule checks.
# Used right after solving and again after every drag-and-drop edit (only the touched shifts need it, but it's cheap).
# ---------------------------------------------------------------------------------------------------------------
def load_index(inp: Inputs):
    return {f'{L["lane"].id}-{L["unit"]}': L for L in inp.loads}


def recompute_shift(inp: Inputs, sh: dict, loads: dict) -> dict:
    stops = sh["stops"]
    t = sh["start_override"] if sh.get("start_override") is not None else sh["start"]
    drive_m = 0; loaded_mi = empty_mi = 0.0; rev = pay = fsc = subpay = 0.0; n = 0; warnings = []
    prev_loc = stops[0]["loc_id"]; loaded = False
    stops[0].update(arrive=t, depart=t, miles=0, drive_min=0)
    for st in stops[1:]:
        loc = inp.locs[st["loc_id"]]
        mi = inp.miles(prev_loc, st["loc_id"]); dm = inp.drive_min(prev_loc, st["loc_id"])
        if loaded: loaded_mi += mi
        else: empty_mi += mi
        drive_m += dm; t += dm
        svc = 0 if st["kind"] == "yard" else inp.service_min(loc, st["kind"])
        # wait for the site to open
        o, c = hm_to_min(loc.open_time), hm_to_min(loc.close_time)
        if st["kind"] != "yard" and (o is not None or c is not None):
            tod = t % 1440; o = o or 0; c = c if c is not None else 1439
            if o <= c:
                if tod < o: t += o - tod
                elif tod > c: warnings.append(f"arrives {loc.name} at {min_to_hm(t)} after it closes ({loc.close_time})")
            else:
                if c < tod < o: t += o - tod
        st.update(arrive=t, depart=t + svc, miles=round(mi, 1), drive_min=dm)
        L = loads.get(st.get("load_key"))
        if st["kind"] == "pickup":
            loaded = True
            if L: st.update(lane=f'{L["lane"].pickup.name} → {L["lane"].dropoff.name}', customer=L["lane"].customer or "", qty=L["qty"])
        elif st["kind"] == "dropoff":
            loaded = False
            if L:
                st.update(lane=f'{L["lane"].pickup.name} → {L["lane"].dropoff.name}', customer=L["lane"].customer or "", qty=L["qty"],
                          rev=round(L["rev"], 2), fsc=round(L["fsc"], 2), pay=round(L["pay"], 2))
                rev += L["rev"]; pay += L["pay"]; fsc += L["fsc"]; n += 1
                if sh["is_sub"]: subpay += L["rev"] * sh["share"] / 100.0
        t += svc; prev_loc = st["loc_id"]
    t_end = stops[-1]["arrive"] if len(stops) > 1 else t
    duty_m = t_end - (sh["start_override"] if sh.get("start_override") is not None else sh["start"])
    hrs = duty_m / 60
    used = n > 0
    if hrs > sh["duty_cap_h"] + 1e-6: warnings.append(f"on-duty {hrs:.1f} h exceeds the {sh['duty_cap_h']:.1f} h cap")
    if drive_m / 60 > sh["drive_cap_h"] + 1e-6: warnings.append(f"driving {drive_m / 60:.1f} h exceeds the {sh['drive_cap_h']:.1f} h cap")
    if sh["is_sub"]:
        fuel = 0.0; fixed = 0.0
        profit = rev - subpay
        fsc_kept = 0.0 if inp.fsc_to_subs else fsc
    else:
        fuel = (loaded_mi + empty_mi) * sh["cpm"]; fixed = inp.fixed if used else 0.0
        profit = rev - pay - fuel - fixed
        fsc_kept = fsc
    sh.update(used=used, loads=n, end=t_end, duty_hours=round(hrs, 2), drive_hours=round(drive_m / 60, 2),
              loaded_miles=round(loaded_mi, 1), empty_miles=round(empty_mi, 1),
              loaded_pct=round(100 * loaded_mi / (loaded_mi + empty_mi), 1) if (loaded_mi + empty_mi) else 0,
              revenue=round(rev, 2), fsc=round(fsc, 2), driver_pay=round(0 if sh["is_sub"] else pay, 2), fuel=round(fuel, 2), fixed=round(fixed, 2),
              sub_pay=round(subpay, 2), profit=round(profit, 2), profit_fsc=round(profit + fsc_kept, 2),
              per_hour=round(rev / hrs, 2) if hrs else 0, per_hour_fsc=round((rev + fsc) / hrs, 2) if hrs else 0,
              margin_per_hour=round(profit / hrs, 2) if hrs else 0, meets_target=(rev / hrs >= inp.target) if hrs else False,
              warnings=warnings)
    return sh


def recompute(inp: Inputs, result: dict) -> dict:
    loads = load_index(inp)
    for sh in result["shifts"]: recompute_shift(inp, sh, loads)
    used = [x for x in result["shifts"] if x["used"]]
    own = [x for x in used if not x["is_sub"]]
    hrs = sum(x["duty_hours"] for x in used); own_hrs = sum(x["duty_hours"] for x in own)
    tot_mi = sum(x["loaded_miles"] + x["empty_miles"] for x in used); tot_loaded = sum(x["loaded_miles"] for x in used)
    result["totals"] = dict(
        loads=sum(x["loads"] for x in used), slots_used=len(used), slots=len(result["shifts"]),
        revenue=round(sum(x["revenue"] for x in used), 2), fsc=round(sum(x["fsc"] for x in used), 2),
        driver_pay=round(sum(x["driver_pay"] for x in used), 2), fuel=round(sum(x["fuel"] for x in used), 2),
        fixed=round(sum(x["fixed"] for x in used), 2), sub_pay=round(sum(x["sub_pay"] for x in used), 2),
        profit=round(sum(x["profit"] for x in used), 2), profit_fsc=round(sum(x["profit_fsc"] for x in used), 2),
        hours=round(hrs, 2), miles=round(tot_mi, 1), loaded_miles=round(tot_loaded, 1),
        loaded_pct=round(100 * tot_loaded / tot_mi, 1) if tot_mi else 0,
        per_hour=round(sum(x["revenue"] for x in own) / own_hrs, 2) if own_hrs else 0,
        unassigned=len(result["unassigned"]), warnings=sum(len(x["warnings"]) for x in used), target=inp.target,
        fuel_price=inp.fuel_price, fsc_pct=money.fsc_fraction(inp.st, inp.fuel_price) * 100)
    by = {}
    for x in used:
        key = x["yard"] + (f" · sub {int(x['share'])}%" if x["is_sub"] else "")
        g = by.setdefault(key, dict(group=key, is_sub=x["is_sub"], slots=0, loads=0, hours=0.0, revenue=0.0, sub_pay=0.0, profit=0.0, loaded_miles=0.0, miles=0.0))
        g["slots"] += 1; g["loads"] += x["loads"]; g["hours"] += x["duty_hours"]; g["revenue"] += x["revenue"]; g["sub_pay"] += x["sub_pay"]
        g["profit"] += x["profit"]; g["loaded_miles"] += x["loaded_miles"]; g["miles"] += x["loaded_miles"] + x["empty_miles"]
    for g in by.values():
        for k in ("hours", "revenue", "sub_pay", "profit", "loaded_miles", "miles"): g[k] = round(g[k], 2)
    result["by_group"] = sorted(by.values(), key=lambda g: (g["is_sub"], g["group"]))
    return result


def recompute_saved(s: Session, cid: int, st: dict, plan: Plan) -> dict:
    """Rebuild the numbers of a saved (possibly hand-edited) plan without re-optimizing."""
    inputs = json.loads(plan.inputs_json or "{}"); result = json.loads(plan.result_json or "{}")
    inp = Inputs(s, cid, st, inputs)
    if not result.get("shifts"): return result
    return recompute(inp, result)
