"""Shift Planner — the driver-less profit planner. FastAPI + Jinja2; one company per account."""
import os, io, csv, json, hashlib, re
from datetime import date, datetime, timedelta
from fastapi import FastAPI, Request, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload
from db import (SessionLocal, Company, Setting, Location, Lane, Distance, FuelType, Plan, init_db,
                RATE_BASES, PAY_BASES, FUEL_KINDS)
from seed import SETTINGS, SETTING_GROUPS, bootstrap, ensure_company, settings_map, load_petrol
import mileage, engine, money

PRODUCT = os.environ.get("PRODUCT_NAME", "Shift Planner")
PASSWORD = os.environ.get("PLANNER_PASSWORD", "").strip()
SECRET = os.environ.get("SESSION_SECRET") or hashlib.sha256(("planner-" + PASSWORD).encode()).hexdigest()

app = FastAPI(title=PRODUCT)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.filters["hm"] = engine.min_to_hm
templates.env.filters["money"] = lambda v: f"${(v or 0):,.0f}"
templates.env.filters["money2"] = lambda v: f"${(v or 0):,.2f}"
templates.env.globals["css_v"] = str(int(os.path.getmtime("static/app.css")))
templates.env.globals["product"] = PRODUCT
KINDS = ["pickup", "dropoff", "both", "yard"]


@app.on_event("startup")
def _startup():
    bootstrap()


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if PASSWORD and not request.session.get("ok") and not (path.startswith("/login") or path.startswith("/static")):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)

app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=30 * 24 * 3600, same_site="lax")


# ---------------- helpers ----------------
def get_db():
    s = SessionLocal()
    try: yield s
    finally: s.close()


def company(s: Session) -> Company:
    return ensure_company(s)


def render(request, tpl, s: Session, **ctx):
    c = company(s)
    ctx.setdefault("request", request); ctx.setdefault("company", c)
    ctx.setdefault("msg", request.query_params.get("msg")); ctx.setdefault("no_pw", not PASSWORD)
    return templates.TemplateResponse(tpl, ctx)


def fnum(v):
    v = (v or "").strip().replace("$", "").replace(",", "").replace("%", "")
    return float(v) if v else None


def fint(v):
    v = (v or "").strip()
    return int(float(v)) if v else None


def st_of(s: Session, c: Company) -> dict:
    return settings_map(s, c.id)


def lane_preview(s: Session, c: Company, st: dict, lane: Lane, dist: dict):
    """Money for one load on this lane, for the lane list and the plan builder."""
    lm = dist.get((lane.pickup_id, lane.dropoff_id))
    if lm is None: lm = round(mileage.haversine_miles(lane.pickup, lane.dropoff) * mileage.STRAIGHT_LINE_FACTOR, 1)
    fuel = next((f for f in s.query(FuelType).filter(FuelType.company_id == c.id).all() if f.is_default), None)
    rev = money.revenue(lane, lane.pickup, lane.dropoff, lm, st)
    pay = money.driver_pay(lane, lane.pickup, lane.dropoff, lm, rev, st)
    fsc = money.surcharge(rev, lm, st, fuel.price if fuel else None)
    qty = money.load_qty(lane, lane.pickup, st) if lane.rate_basis == "unit" else (lane.pickup.avg_qty or 0)
    return dict(miles=lm, rev=rev, pay=pay, fsc=fsc, qty=qty, margin=rev - pay)


def dist_map(s: Session, c: Company):
    out = {}
    for d in s.query(Distance).filter(Distance.company_id == c.id).all():
        m = d.override_miles if d.override_miles else d.google_miles
        if m is not None: out[(d.origin_id, d.dest_id)] = m
    return out


# ---------------- login ----------------
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "msg": request.query_params.get("msg"), "no_pw": not PASSWORD, "company": None})


@app.post("/login")
async def login_post(request: Request):
    f = await request.form()
    if PASSWORD and f.get("password", "") == PASSWORD:
        request.session["ok"] = True
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login?msg=Wrong+password", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------- dashboard ----------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, s: Session = Depends(get_db)):
    c = company(s); st = st_of(s, c)
    cov = mileage.coverage(s, c.id)
    counts = {k: s.query(Location).filter(Location.company_id == c.id, Location.kind == k, Location.active == True).count() for k in KINDS}
    lanes_n = s.query(Lane).filter(Lane.company_id == c.id, Lane.active == True).count()
    no_rate = s.query(Lane).filter(Lane.company_id == c.id, Lane.active == True, Lane.rate == None).count()
    plans = s.query(Plan).filter(Plan.company_id == c.id).order_by(Plan.id.desc()).limit(8).all()
    fuel = next((f for f in s.query(FuelType).filter(FuelType.company_id == c.id).all() if f.is_default), None)
    todo = []
    if not counts["yard"]: todo.append("Add at least one yard (Locations → type = yard).")
    if not lanes_n: todo.append("Add lanes with rates, or import them (Import data).")
    if no_rate: todo.append(f"{no_rate} active lane(s) have no rate.")
    if cov["missing"]: todo.append(f"{cov['missing']} of {cov['needed']} mileage pairs still need road miles (Mileage & routes).")
    if not PASSWORD: todo.append("No password set — add PLANNER_PASSWORD under Environment so only your team can open this.")
    return render(request, "dashboard.html", s, counts=counts, lanes_n=lanes_n, cov=cov, plans=plans, todo=todo, st=st, fuel=fuel,
                  fsc_pct=money.fsc_fraction(st, fuel.price if fuel else None) * 100, has_key=bool(mileage.get_api_key()))


# ---------------- plans ----------------
def _yards(s, c):
    return s.query(Location).filter(Location.company_id == c.id, Location.kind == "yard", Location.active == True).order_by(Location.name).all()


def _lanes(s, c):
    rows = s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff)).filter(Lane.company_id == c.id, Lane.active == True).all()
    rows.sort(key=lambda l: (l.pickup.name, l.dropoff.name))
    return rows


@app.get("/plans", response_class=HTMLResponse)
def plans(request: Request, s: Session = Depends(get_db)):
    c = company(s)
    rows = s.query(Plan).filter(Plan.company_id == c.id).order_by(Plan.id.desc()).limit(200).all()
    return render(request, "plans.html", s, rows=rows)


@app.get("/plan/new", response_class=HTMLResponse)
def plan_new(request: Request, copy: int | None = None, s: Session = Depends(get_db)):
    c = company(s); st = st_of(s, c)
    inputs = {"loads": [], "groups": []}; name = ""; pdate = date.today().isoformat()
    if copy:
        src = s.get(Plan, copy)
        if src and src.company_id == c.id:
            inputs = json.loads(src.inputs_json or "{}"); name = (src.name or "") + " (copy)"; pdate = src.plan_date or pdate
    lanes = _lanes(s, c); dist = dist_map(s, c)
    previews = {l.id: lane_preview(s, c, st, l, dist) for l in lanes}
    fuels = s.query(FuelType).filter(FuelType.company_id == c.id, FuelType.active == True).all()
    return render(request, "plan_new.html", s, lanes=lanes, previews=previews, yards=_yards(s, c), fuels=fuels, st=st,
                  inputs=inputs, name=name, pdate=pdate, plan_id=None,
                  lanes_json=json.dumps([dict(id=l.id, label=f"{l.pickup.name} → {l.dropoff.name}" + (f" ({l.customer})" if l.customer else ""),
                                              rev=round(previews[l.id]["rev"]), qty=round(previews[l.id]["qty"]), miles=previews[l.id]["miles"]) for l in lanes]))


@app.get("/plan/{plan_id}/edit", response_class=HTMLResponse)
def plan_edit(request: Request, plan_id: int, s: Session = Depends(get_db)):
    c = company(s); st = st_of(s, c)
    p = s.get(Plan, plan_id)
    if not p or p.company_id != c.id: return RedirectResponse("/plans?msg=Plan+not+found", status_code=303)
    lanes = _lanes(s, c); dist = dist_map(s, c)
    previews = {l.id: lane_preview(s, c, st, l, dist) for l in lanes}
    fuels = s.query(FuelType).filter(FuelType.company_id == c.id, FuelType.active == True).all()
    return render(request, "plan_new.html", s, lanes=lanes, previews=previews, yards=_yards(s, c), fuels=fuels, st=st,
                  inputs=json.loads(p.inputs_json or "{}"), name=p.name or "", pdate=p.plan_date, plan_id=p.id,
                  lanes_json=json.dumps([dict(id=l.id, label=f"{l.pickup.name} → {l.dropoff.name}" + (f" ({l.customer})" if l.customer else ""),
                                              rev=round(previews[l.id]["rev"]), qty=round(previews[l.id]["qty"]), miles=previews[l.id]["miles"]) for l in lanes]))


def _parse_inputs(f) -> dict:
    loads, groups = [], []
    for lane_id, count, qty in zip(f.getlist("ld_lane"), f.getlist("ld_count"), f.getlist("ld_qty")):
        if lane_id and fint(count): loads.append(dict(lane_id=int(lane_id), count=fint(count), qty=fnum(qty)))
    for yard, count, duty, drive, fuel, kind, share, label in zip(f.getlist("g_yard"), f.getlist("g_count"), f.getlist("g_duty"), f.getlist("g_drive"),
                                                                 f.getlist("g_fuel"), f.getlist("g_kind"), f.getlist("g_share"), f.getlist("g_label")):
        if yard and fint(count):
            groups.append(dict(yard_id=int(yard), count=fint(count), max_duty=fnum(duty), max_drive=fnum(drive), fuel_id=fint(fuel),
                               kind=kind or "company", share=fnum(share), label=(label or "").strip() or None))
    return dict(loads=loads, groups=groups)


@app.post("/plan/build")
async def plan_build(request: Request, s: Session = Depends(get_db)):
    c = company(s); st = st_of(s, c)
    f = await request.form()
    inputs = _parse_inputs(f)
    pid = fint(f.get("plan_id"))
    p = s.get(Plan, pid) if pid else None
    if not p or p.company_id != c.id:
        p = Plan(company_id=c.id); s.add(p)
    p.name = (f.get("name") or "").strip() or None
    p.plan_date = f.get("plan_date") or date.today().isoformat()
    p.inputs_json = json.dumps(inputs)
    if f.get("action") == "save":
        p.status = p.status if p.result_json else "draft"; s.commit()
        return RedirectResponse(f"/plan/{p.id}/edit?msg=Saved", status_code=303)
    res = engine.solve(s, c.id, st, inputs)
    if not res.get("ok"):
        s.commit()
        return RedirectResponse(f"/plan/{p.id}/edit?msg={res['error']}", status_code=303)
    p.result_json = json.dumps(res); p.status = "built"
    t = res["totals"]
    p.summary = f"{t['loads']} loads · {t['slots_used']} of {t['slots']} slots · {t['revenue']:,.0f} revenue · {t['profit']:,.0f} profit · {t['unassigned']} not fitted"
    s.commit()
    return RedirectResponse(f"/plan/{p.id}", status_code=303)


def _plan_ctx(s, c, st, p):
    res = json.loads(p.result_json or "{}")
    yards = {y.id: y for y in _yards(s, c)}
    return dict(p=p, res=res, T=res.get("totals", {}), st=st, yards=yards)


@app.get("/plan/{plan_id}", response_class=HTMLResponse)
def plan_view(request: Request, plan_id: int, s: Session = Depends(get_db)):
    c = company(s); st = st_of(s, c)
    p = s.get(Plan, plan_id)
    if not p or p.company_id != c.id: return RedirectResponse("/plans?msg=Plan+not+found", status_code=303)
    if not p.result_json: return RedirectResponse(f"/plan/{p.id}/edit", status_code=303)
    return render(request, "plan.html", s, **_plan_ctx(s, c, st, p))


@app.get("/plan/{plan_id}/print", response_class=HTMLResponse)
def plan_print(request: Request, plan_id: int, s: Session = Depends(get_db)):
    c = company(s); st = st_of(s, c)
    p = s.get(Plan, plan_id)
    if not p or p.company_id != c.id or not p.result_json: return RedirectResponse("/plans", status_code=303)
    return render(request, "plan_print.html", s, **_plan_ctx(s, c, st, p))


@app.post("/plan/{plan_id}/rebuild")
def plan_rebuild(plan_id: int, s: Session = Depends(get_db)):
    c = company(s); st = st_of(s, c)
    p = s.get(Plan, plan_id)
    if not p or p.company_id != c.id: return RedirectResponse("/plans", status_code=303)
    res = engine.solve(s, c.id, st, json.loads(p.inputs_json or "{}"))
    if not res.get("ok"): return RedirectResponse(f"/plan/{p.id}?msg={res['error']}", status_code=303)
    p.result_json = json.dumps(res); p.status = "built"; t = res["totals"]
    p.summary = f"{t['loads']} loads · {t['slots_used']} of {t['slots']} slots · {t['revenue']:,.0f} revenue · {t['profit']:,.0f} profit · {t['unassigned']} not fitted"
    s.commit()
    return RedirectResponse(f"/plan/{p.id}?msg=Plan+rebuilt", status_code=303)


@app.post("/plan/{plan_id}/assign")
async def plan_assign(request: Request, plan_id: int, s: Session = Depends(get_db)):
    """Driver names and actual start times typed onto the shifts; times are recomputed from the new starts."""
    c = company(s); st = st_of(s, c)
    p = s.get(Plan, plan_id)
    if not p or p.company_id != c.id: return RedirectResponse("/plans", status_code=303)
    f = await request.form()
    res = json.loads(p.result_json or "{}")
    for i, sh in enumerate(res.get("shifts", [])):
        sh["driver"] = (f.get(f"driver_{i}") or "").strip()
        t = f.get(f"start_{i}") or ""
        sh["start_override"] = engine.hm_to_min(t) if t else None
    res = engine.recompute(engine.Inputs(s, c.id, st, json.loads(p.inputs_json or "{}")), res)
    p.result_json = json.dumps(res); s.commit()
    return RedirectResponse(f"/plan/{p.id}?msg=Drivers+and+start+times+saved", status_code=303)


@app.post("/plan/{plan_id}/delete")
def plan_delete(plan_id: int, s: Session = Depends(get_db)):
    c = company(s); p = s.get(Plan, plan_id)
    if p and p.company_id == c.id: s.delete(p); s.commit()
    return RedirectResponse("/plans?msg=Plan+deleted", status_code=303)


@app.get("/plan/{plan_id}/export.csv")
def plan_csv(plan_id: int, s: Session = Depends(get_db)):
    c = company(s); p = s.get(Plan, plan_id)
    if not p or p.company_id != c.id or not p.result_json: return RedirectResponse("/plans", status_code=303)
    res = json.loads(p.result_json)
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["plan", "date", "slot", "driver", "yard", "type", "stop #", "kind", "location", "lane", "customer", "arrive", "depart", "miles", "qty", "revenue", "surcharge"])
    for sh in res["shifts"]:
        if not sh["used"]: continue
        for i, st in enumerate(sh["stops"]):
            w.writerow([p.name or "", p.plan_date, sh["slot"], sh.get("driver", ""), sh["yard"], f"sub {int(sh['share'])}%" if sh["is_sub"] else "company", i,
                        st["kind"], st["name"], st.get("lane", ""), st.get("customer", ""), engine.min_to_hm(st["arrive"]), engine.min_to_hm(st["depart"]),
                        st.get("miles", ""), st.get("qty", "") if st["kind"] == "dropoff" else "", st.get("rev", "") if st["kind"] == "dropoff" else "", st.get("fsc", "") if st["kind"] == "dropoff" else ""])
    w.writerow([]); w.writerow(["shift summary"])
    w.writerow(["slot", "driver", "start", "end", "loads", "on-duty h", "drive h", "loaded mi", "empty mi", "loaded %", "revenue", "surcharge", "driver pay", "fuel", "sub pay", "profit", "$/hr"])
    for sh in res["shifts"]:
        if not sh["used"]: continue
        w.writerow([sh["slot"], sh.get("driver", ""), engine.min_to_hm(sh["start_override"] if sh.get("start_override") is not None else sh["start"]), engine.min_to_hm(sh["end"]), sh["loads"], sh["duty_hours"], sh["drive_hours"],
                    sh["loaded_miles"], sh["empty_miles"], sh["loaded_pct"], sh["revenue"], sh["fsc"], sh["driver_pay"], sh["fuel"], sh["sub_pay"], sh["profit"], sh["per_hour"]])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="plan_{p.plan_date}_{p.id}.csv"'})


# ---------------- lanes ----------------
@app.get("/lanes", response_class=HTMLResponse)
def lanes(request: Request, q: str = "", show_inactive: int = 0, s: Session = Depends(get_db)):
    c = company(s); st = st_of(s, c)
    qry = s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff)).filter(Lane.company_id == c.id)
    if not show_inactive: qry = qry.filter(Lane.active == True)
    rows = qry.all()
    if q:
        ql = q.lower(); rows = [l for l in rows if ql in f"{l.pickup.name} {l.dropoff.name} {l.customer or ''} {l.product or ''}".lower()]
    rows.sort(key=lambda l: (l.pickup.name, l.dropoff.name))
    dist = dist_map(s, c)
    previews = {l.id: lane_preview(s, c, st, l, dist) for l in rows}
    return render(request, "lanes.html", s, rows=rows, q=q, show_inactive=show_inactive, previews=previews, st=st,
                  bases=dict(RATE_BASES), pays=dict(PAY_BASES))


def _lane_form(request, s, c, lane):
    locs = s.query(Location).filter(Location.company_id == c.id, Location.active == True).order_by(Location.name).all()
    return render(request, "lane_form.html", s, lane=lane, pickups=[l for l in locs if l.kind in ("pickup", "both")],
                  dropoffs=[l for l in locs if l.kind in ("dropoff", "both")], bases=RATE_BASES, pays=PAY_BASES, st=st_of(s, c))


@app.get("/lanes/new", response_class=HTMLResponse)
def lane_new(request: Request, s: Session = Depends(get_db)):
    return _lane_form(request, s, company(s), None)


@app.get("/lanes/{lane_id}", response_class=HTMLResponse)
def lane_edit(request: Request, lane_id: int, s: Session = Depends(get_db)):
    c = company(s); l = s.get(Lane, lane_id)
    if not l or l.company_id != c.id: return RedirectResponse("/lanes", status_code=303)
    return _lane_form(request, s, c, l)


@app.post("/lanes/save")
async def lane_save(request: Request, s: Session = Depends(get_db)):
    c = company(s); f = await request.form()
    l = s.get(Lane, int(f["id"])) if f.get("id") else Lane(company_id=c.id)
    if l.company_id != c.id: return RedirectResponse("/lanes", status_code=303)
    l.customer = (f.get("customer") or "").strip() or None; l.product = (f.get("product") or "").strip() or None
    l.pickup_id = int(f["pickup_id"]); l.dropoff_id = int(f["dropoff_id"])
    l.rate_basis = f.get("rate_basis") or "unit"; l.rate = fnum(f.get("rate"))
    l.min_qty = fnum(f.get("min_qty")); l.qty_override = fnum(f.get("qty_override")); l.weight_tons = fnum(f.get("weight_tons"))
    l.pay_basis = f.get("pay_basis") or "pct"; l.pay_rate = fnum(f.get("pay_rate"))
    l.active = bool(f.get("active")); l.notes = (f.get("notes") or "").strip() or None
    s.add(l); s.commit()
    return RedirectResponse("/lanes?msg=Lane+saved", status_code=303)


# ---------------- locations ----------------
@app.get("/locations", response_class=HTMLResponse)
def locations(request: Request, kind: str = "", q: str = "", show_inactive: int = 0, s: Session = Depends(get_db)):
    c = company(s)
    qry = s.query(Location).filter(Location.company_id == c.id)
    if kind: qry = qry.filter(Location.kind == kind)
    if q: qry = qry.filter(Location.name.ilike(f"%{q}%"))
    if not show_inactive: qry = qry.filter(Location.active == True)
    rows = qry.order_by(Location.kind, Location.name).all()
    return render(request, "locations.html", s, rows=rows, kind=kind, q=q, show_inactive=show_inactive, kinds=KINDS, st=st_of(s, c))


@app.get("/locations/new", response_class=HTMLResponse)
def location_new(request: Request, s: Session = Depends(get_db)):
    return render(request, "location_form.html", s, loc=None, kinds=KINDS, lanes=[], st=st_of(s, company(s)))


@app.get("/locations/{loc_id}", response_class=HTMLResponse)
def location_edit(request: Request, loc_id: int, s: Session = Depends(get_db)):
    c = company(s); loc = s.get(Location, loc_id)
    if not loc or loc.company_id != c.id: return RedirectResponse("/locations", status_code=303)
    lanes = s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff)).filter(or_(Lane.pickup_id == loc_id, Lane.dropoff_id == loc_id)).all()
    return render(request, "location_form.html", s, loc=loc, kinds=KINDS, lanes=lanes, st=st_of(s, c))


@app.post("/locations/save")
async def location_save(request: Request, s: Session = Depends(get_db)):
    c = company(s); f = await request.form()
    loc = s.get(Location, int(f["id"])) if f.get("id") else Location(company_id=c.id)
    if loc.company_id != c.id: return RedirectResponse("/locations", status_code=303)
    loc.name = f["name"].strip(); loc.kind = f["kind"]; loc.lat = float(f["lat"]); loc.lon = float(f["lon"])
    loc.active = bool(f.get("active")); loc.avg_qty = fnum(f.get("avg_qty")); loc.avg_weight_tons = fnum(f.get("avg_weight_tons"))
    loc.load_minutes = fint(f.get("load_minutes")); loc.unload_minutes = fint(f.get("unload_minutes"))
    loc.open_time = f.get("open_time") or None; loc.close_time = f.get("close_time") or None; loc.notes = (f.get("notes") or "").strip() or None
    s.add(loc); s.commit()
    return RedirectResponse(f"/locations/{loc.id}?msg=Saved", status_code=303)


# ---------------- mileage & routes ----------------
@app.get("/mileage", response_class=HTMLResponse)
def mileage_page(request: Request, q: str = "", only: str = "", s: Session = Depends(get_db)):
    c = company(s)
    cov = mileage.coverage(s, c.id)
    qry = s.query(Distance).options(joinedload(Distance.origin), joinedload(Distance.dest)).filter(Distance.company_id == c.id)
    rows = qry.all()
    if q:
        ql = q.lower(); rows = [d for d in rows if ql in d.origin.name.lower() or ql in d.dest.name.lower()]
    if only == "approved": rows = [d for d in rows if d.approved]
    elif only == "unapproved": rows = [d for d in rows if not d.approved]
    rows.sort(key=lambda d: (d.origin.name, d.dest.name))
    approved_n = s.query(Distance).filter(Distance.company_id == c.id, Distance.approved == True).count()
    return render(request, "mileage.html", s, rows=rows[:500], total=len(rows), q=q, only=only, cov=cov, approved_n=approved_n,
                  has_key=bool(mileage.get_api_key()), browser_key=bool(os.environ.get("GOOGLE_MAPS_BROWSER_KEY")))


@app.post("/mileage/fetch")
def mileage_fetch(s: Session = Depends(get_db)):
    c = company(s)
    r = mileage.fetch_from_google(s, c.id, mileage.get_api_key())
    if "error" in r: return RedirectResponse(f"/mileage?msg={r['error']}", status_code=303)
    msg = f"Fetched {r['fetched']} pairs from Google in {r['requests']} requests; {r['remaining']} still missing."
    if r["errors"]: msg += " Issues: " + "; ".join(r["errors"][:3])
    return RedirectResponse(f"/mileage?msg={msg}", status_code=303)


@app.post("/mileage/straight")
def mileage_straight(s: Session = Depends(get_db)):
    c = company(s); n = mileage.fill_straight_line(s, c.id)
    return RedirectResponse(f"/mileage?msg=Estimated+{n}+pairs+as+straight-line+x+1.3+(replace+with+Google+when+you+can)", status_code=303)


def _dist_row(s, c, a, b, create=True):
    d = s.query(Distance).filter_by(company_id=c.id, origin_id=a, dest_id=b).first()
    if not d and create:
        d = Distance(company_id=c.id, origin_id=a, dest_id=b); s.add(d); s.flush()
    return d


@app.get("/route/{a}/{b}", response_class=HTMLResponse)
def route_page(request: Request, a: int, b: int, back: str = "", s: Session = Depends(get_db)):
    c = company(s); st = st_of(s, c)
    o, d = s.get(Location, a), s.get(Location, b)
    if not o or not d or o.company_id != c.id: return RedirectResponse("/mileage?msg=Unknown+location", status_code=303)
    dist = _dist_row(s, c, a, b, create=False)
    return render(request, "route.html", s, o=o, d=d, dist=dist, via_json=(dist.via_json if dist and dist.via_json else "[]"), back=back.replace("%23", "#"),
                  browser_key=os.environ.get("GOOGLE_MAPS_BROWSER_KEY", "").strip(), speed=st.get("avg_speed_mph") or 41)


def _approve(s, c, a, b, miles, minutes, kind, via, poly, note):
    d = _dist_row(s, c, a, b)
    d.override_miles = miles; d.override_minutes = minutes; d.approved = True; d.approved_at = datetime.utcnow(); d.route_kind = kind
    d.via_json = via or None; d.polyline = poly or None
    if note is not None: d.override_note = note or None
    if d.google_miles is None: d.google_miles, d.source = miles, "google"


def _back(back, msg):
    back = (back or "").replace("%23", "#")
    base, frag = (back.split("#", 1) + [""])[:2]
    return f"{base or '/mileage'}{'&' if '?' in base else '?'}msg={msg}" + ("#" + frag if frag else "")


@app.post("/route/{a}/{b}/approve")
async def route_approve(request: Request, a: int, b: int, s: Session = Depends(get_db)):
    c = company(s); f = await request.form()
    miles, mins = fnum(f.get("miles")), fnum(f.get("minutes"))
    if not miles: return RedirectResponse(f"/route/{a}/{b}?msg=No+route+to+approve+yet", status_code=303)
    _approve(s, c, a, b, miles, mins, f.get("kind") or "google-default", f.get("via"), f.get("polyline"), f.get("note"))
    msg = f"Approved: {miles} mi"
    if f.get("reverse") and fnum(f.get("rev_miles")):
        _approve(s, c, b, a, fnum(f.get("rev_miles")), fnum(f.get("rev_minutes")), f.get("kind") or "google-default", f.get("rev_via"), f.get("rev_polyline"), f.get("note"))
        msg += f" (reverse {fnum(f.get('rev_miles'))} mi)"
    s.commit()
    return RedirectResponse(_back(f.get("back"), msg), status_code=303)


@app.post("/route/{a}/{b}/manual")
async def route_manual(request: Request, a: int, b: int, s: Session = Depends(get_db)):
    c = company(s); f = await request.form(); miles = fnum(f.get("miles"))
    if not miles: return RedirectResponse(f"/route/{a}/{b}?msg=Enter+the+miles+first", status_code=303)
    _approve(s, c, a, b, miles, fnum(f.get("minutes")), "manual-miles", None, None, None)
    if f.get("reverse"): _approve(s, c, b, a, miles, fnum(f.get("minutes")), "manual-miles", None, None, None)
    s.commit()
    return RedirectResponse(_back(f.get("back"), f"Approved {miles} mi (typed)"), status_code=303)


@app.post("/route/{a}/{b}/unapprove")
async def route_unapprove(request: Request, a: int, b: int, s: Session = Depends(get_db)):
    c = company(s); f = await request.form(); d = _dist_row(s, c, a, b, create=False)
    if d:
        d.approved = False; d.approved_at = None; d.route_kind = None; d.via_json = None; d.polyline = None; d.override_miles = None; d.override_minutes = None
        s.commit()
    return RedirectResponse(_back(f.get("back"), "Approval cleared"), status_code=303)


# ---------------- settings ----------------
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, s: Session = Depends(get_db)):
    c = company(s); st = st_of(s, c)
    rows = [dict(key=k, label=label, unit=unit, note=note, group=grp, value=st.get(k), is_text=(val is None)) for k, val, txt, label, unit, note, grp in SETTINGS]
    fuels = s.query(FuelType).filter(FuelType.company_id == c.id).order_by(FuelType.id).all()
    return render(request, "settings.html", s, rows=rows, groups=SETTING_GROUPS, fuels=fuels, fuel_kinds=FUEL_KINDS, st=st,
                  has_key=bool(mileage.get_api_key()), has_browser_key=bool(os.environ.get("GOOGLE_MAPS_BROWSER_KEY")))


@app.post("/settings")
async def settings_save(request: Request, s: Session = Depends(get_db)):
    c = company(s); f = await request.form()
    have = {x.key: x for x in s.query(Setting).filter(Setting.company_id == c.id).all()}
    for k, val, txt, *_ in SETTINGS:
        if k not in f: continue
        row = have.get(k) or Setting(company_id=c.id, key=k)
        if val is None: row.text = (f.get(k) or "").strip() or txt; row.value = None
        else: row.value = fnum(f.get(k)); row.text = None
        s.add(row)
    s.commit()
    return RedirectResponse("/settings?msg=Settings+saved", status_code=303)


@app.post("/settings/company")
async def company_save(request: Request, s: Session = Depends(get_db), logo: UploadFile | None = File(None)):
    c = company(s); f = await request.form()
    c.name = (f.get("name") or "").strip() or c.name
    for k in ("address", "phone", "email", "website"): setattr(c, k, (f.get(k) or "").strip() or None)
    if logo and logo.filename:
        data = await logo.read()
        if data and len(data) < 2_000_000: c.logo, c.logo_mime = data, logo.content_type or "image/png"
    if f.get("remove_logo"): c.logo, c.logo_mime = None, None
    s.commit()
    return RedirectResponse("/settings?msg=Company+details+saved", status_code=303)


@app.get("/company/logo")
def company_logo(s: Session = Depends(get_db)):
    c = company(s)
    if not c.logo: return Response(status_code=404)
    return Response(c.logo, media_type=c.logo_mime or "image/png")


@app.post("/settings/fuel")
async def fuel_save(request: Request, s: Session = Depends(get_db)):
    c = company(s); f = await request.form()
    if f.get("action") == "delete":
        ft = s.get(FuelType, int(f["id"]))
        if ft and ft.company_id == c.id and not ft.is_default: s.delete(ft); s.commit()
        return RedirectResponse("/settings?msg=Fuel+type+removed#fuel", status_code=303)
    ft = s.get(FuelType, int(f["id"])) if f.get("id") else FuelType(company_id=c.id)
    if ft.company_id != c.id: return RedirectResponse("/settings", status_code=303)
    ft.name = (f.get("name") or "").strip() or "Fuel"; ft.kind = f.get("kind") or "diesel"
    ft.economy = fnum(f.get("economy")); ft.price = fnum(f.get("price")); ft.active = True
    s.add(ft); s.flush()
    if f.get("is_default"):
        for x in s.query(FuelType).filter(FuelType.company_id == c.id).all(): x.is_default = (x.id == ft.id)
    elif not s.query(FuelType).filter(FuelType.company_id == c.id, FuelType.is_default == True).count():
        ft.is_default = True
    s.commit()
    return RedirectResponse("/settings?msg=Fuel+type+saved#fuel", status_code=303)


# ---------------- import ----------------
@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, s: Session = Depends(get_db)):
    c = company(s)
    return render(request, "import.html", s, n_loc=s.query(Location).filter(Location.company_id == c.id).count(),
                  n_lane=s.query(Lane).filter(Lane.company_id == c.id).count(), n_dist=s.query(Distance).filter(Distance.company_id == c.id).count(),
                  petrol_available=os.path.exists("data/lanes.csv"))


@app.get("/import/template/{what}.csv")
def import_template(what: str):
    tpl = {"locations": "name,kind,lat,lon,avg_qty,avg_weight_tons,load_minutes,unload_minutes,open_time,close_time,notes\nNorth Lease,pickup,35.4544,-119.036,165,,60,,,,\nMain Terminal,dropoff,35.3,-119.1,,,,60,06:00,18:00,\nHome Yard,yard,35.45,-119.03,,,,,,,\n",
           "lanes": "pickup,dropoff,customer,product,rate_basis,rate,min_qty,pay_basis,pay_rate,notes\nNorth Lease,Main Terminal,Acme Oil,crude oil,unit,3.50,150,pct,23.5,\n",
           "mileage": "origin,dest,miles,minutes,approved\nHome Yard,North Lease,42.1,58,0\n"}.get(what)
    if not tpl: return Response(status_code=404)
    return Response(tpl, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{what}_template.csv"'})


@app.post("/import/{what}")
async def import_csv(what: str, request: Request, s: Session = Depends(get_db), file: UploadFile = File(...)):
    c = company(s)
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    by_name = {l.name.strip().lower(): l for l in s.query(Location).filter(Location.company_id == c.id).all()}
    n = skipped = 0
    try:
        if what == "locations":
            for r in rows:
                name = (r.get("name") or "").strip()
                if not name or not r.get("lat"): skipped += 1; continue
                loc = by_name.get(name.lower()) or Location(company_id=c.id, name=name)
                loc.kind = (r.get("kind") or "pickup").strip().lower(); loc.lat = float(r["lat"]); loc.lon = float(r["lon"])
                loc.avg_qty = fnum(r.get("avg_qty")); loc.avg_weight_tons = fnum(r.get("avg_weight_tons"))
                loc.load_minutes = fint(r.get("load_minutes")); loc.unload_minutes = fint(r.get("unload_minutes"))
                loc.open_time = (r.get("open_time") or "").strip() or None; loc.close_time = (r.get("close_time") or "").strip() or None
                loc.notes = (r.get("notes") or "").strip() or None; loc.active = True
                s.add(loc); by_name[name.lower()] = loc; n += 1
            s.commit()
        elif what == "lanes":
            for r in rows:
                p, d = by_name.get((r.get("pickup") or "").strip().lower()), by_name.get((r.get("dropoff") or "").strip().lower())
                if not p or not d: skipped += 1; continue
                cust = (r.get("customer") or "").strip() or None
                l = s.query(Lane).filter(Lane.company_id == c.id, Lane.pickup_id == p.id, Lane.dropoff_id == d.id, Lane.customer == cust).first() \
                    or Lane(company_id=c.id, pickup_id=p.id, dropoff_id=d.id, customer=cust)
                l.product = (r.get("product") or "").strip() or None; l.rate_basis = (r.get("rate_basis") or "unit").strip() or "unit"
                l.rate = fnum(r.get("rate")); l.min_qty = fnum(r.get("min_qty")); l.pay_basis = (r.get("pay_basis") or "pct").strip() or "pct"
                l.pay_rate = fnum(r.get("pay_rate")); l.notes = (r.get("notes") or "").strip() or None; l.active = True
                s.add(l); n += 1
            s.commit()
        elif what == "mileage":
            existing = {(x.origin_id, x.dest_id): x for x in s.query(Distance).filter(Distance.company_id == c.id).all()}
            for r in rows:
                o, d = by_name.get((r.get("origin") or "").strip().lower()), by_name.get((r.get("dest") or "").strip().lower())
                miles = fnum(r.get("miles"))
                if not o or not d or miles is None: skipped += 1; continue
                x = existing.get((o.id, d.id)) or Distance(company_id=c.id, origin_id=o.id, dest_id=d.id)
                x.google_miles = miles; x.google_minutes = fnum(r.get("minutes")); x.source = "import"; x.fetched_at = datetime.utcnow()
                if (r.get("approved") or "").strip() in ("1", "yes", "true"):
                    x.override_miles = miles; x.override_minutes = fnum(r.get("minutes")); x.approved = True; x.approved_at = datetime.utcnow(); x.route_kind = x.route_kind or "manual-miles"
                s.add(x); existing[(o.id, d.id)] = x; n += 1
            s.commit()
        else:
            return RedirectResponse("/import?msg=Unknown+import", status_code=303)
    except Exception as e:
        s.rollback()
        return RedirectResponse(f"/import?msg=Import+failed:+{type(e).__name__}+{str(e)[:120]}", status_code=303)
    return RedirectResponse(f"/import?msg={what}:+{n}+row(s)+imported,+{skipped}+skipped", status_code=303)


@app.post("/import/starter/petrol")
def import_petrol(s: Session = Depends(get_db)):
    c = company(s)
    return RedirectResponse(f"/import?msg={load_petrol(s, c.id)}", status_code=303)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
