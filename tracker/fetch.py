"""Daily price collector. Reads config.json, queries Google Flights via fast-flights,
appends to docs/data/<route>.json. Never crashes the whole run on one failure."""
import json, sys, time, datetime as dt, pathlib, random

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
MAX_OFFERS = 12
HISTORY_DAYS = 800


PACE = {"left": 0, "deadline": None, "cfg": {}, "scale": 1.0, "batches": 0, "count": 0, "quota": None}


def pace_init(cfg):
    """Human-like pacing: queries in random-sized batches, short random gaps inside a batch,
    a random multi-minute pause between batches, and a hard time budget."""
    import os
    p = {"batch_size_range": [40, 80], "pause_minutes_range": [1, 3],
         "query_sleep_seconds_range": [1, 3], "max_runtime_minutes": 200}
    p.update(cfg.get("pacing") or {})
    q = (cfg.get("schedule") or {}).get("quota_range")
    PACE.update(cfg=p, left=random.randint(*p["batch_size_range"]), batches=0, count=0,
                quota=random.randint(*q) if q else None,
                scale=float(os.environ.get("PACE_SCALE", "1")),
                deadline=time.time() + p["max_runtime_minutes"] * 60 * float(os.environ.get("PACE_SCALE", "1")))


CHK = {"last": time.time()}
PENDING = {"n": 0}                  # tasks still missing for today when this run ends (drives the relay)


def git_push(msg):
    """Commit and push docs/data from inside the run (CI only), so results show up while the run continues."""
    import os, subprocess
    if not os.environ.get("GITHUB_ACTIONS"):
        return

    def run(*a):
        return subprocess.run(a, capture_output=True, text=True)
    run("git", "add", "docs/data")
    if run("git", "diff", "--cached", "--quiet").returncode == 0:
        return
    run("git", "commit", "-m", msg)
    br = os.environ.get("GITHUB_REF_NAME", "")
    run("git", "pull", "--rebase", "--autostash", "-X", "theirs", "origin", br)
    r = run("git", "push", "origin", "HEAD:" + br)
    print("[checkpoint]", "pushed" if r.returncode == 0 else "push failed: " + r.stderr[:160], flush=True)


def checkpoint(save, force=False):
    """Every ~45 min (or when forced): write files and push them."""
    if force or time.time() - CHK["last"] >= 45 * 60 * PACE["scale"]:
        save()
        git_push("data: checkpoint")
        CHK["last"] = time.time()


NET = {"fails": 0, "pauses": 0, "stop": False}


def expired():
    return (NET["stop"] or (PACE["quota"] is not None and PACE["count"] >= PACE["quota"])
            or (PACE["deadline"] is not None and time.time() > PACE["deadline"]))


def guard():
    """Google throttles bursts. After 3 failed lookups in a row: wait 8 min once or twice, then give up for
    this run (partial data is saved; the next scheduled run resumes with a fresh runner/IP)."""
    if NET["fails"] >= 3:
        if NET["pauses"] < 1:
            NET["pauses"] += 1
            NET["fails"] = 0
            print(f"[guard] looks throttled, pausing 8 min (pause {NET['pauses']}/1)", flush=True)
            time.sleep(480 * PACE["scale"])
        else:
            NET["stop"] = True
            print("[guard] still throttled, stopping this run (partial data is kept)", flush=True)
            raise RuntimeError("throttled by Google; run stopped")


def pace():
    p, sc = PACE["cfg"], PACE["scale"]
    PACE["count"] += 1
    PACE["left"] -= 1
    if PACE["left"] <= 0:
        PACE["batches"] += 1
        pause = random.uniform(*p["pause_minutes_range"]) * 60 * sc
        print(f"[pace] batch {PACE['batches']} done, pausing {pause / 60:.1f} min", flush=True)
        time.sleep(pause)
        PACE["left"] = random.randint(*p["batch_size_range"])
    else:
        time.sleep(random.uniform(*p["query_sleep_seconds_range"]) * sc)


def month_specs(mx):
    """Matrix months may be plain 'YYYY-MM' or {'month','from_day','return_by'}."""
    out = []
    for m in mx.get("months", []):
        out.append({"month": m, "from_day": 1, "return_by": None} if isinstance(m, str)
                   else {"month": m["month"], "from_day": m.get("from_day", 1), "return_by": m.get("return_by")})
    return out


def dates_for(route, cfg):
    mx = cfg.get("matrix") or {}
    skip_months = tuple(sp["month"] for sp in month_specs(mx)) if (route["id"] in mx.get("list_routes", mx.get("routes", [])) and PRICING["mode"] != "calendar") else ()
    """All (depart, return) pairs: every window's centre +/- flex, return = depart + stay."""
    flex = route.get("flex_days", cfg.get("flex_days", 7))
    step = route.get("sample_step", cfg.get("sample_step", 2))
    stay = route.get("stay_days", cfg.get("stay_days"))
    today = dt.date.today()
    seen = set()
    for w in cfg["windows"]:
        c = dt.date.fromisoformat(w["center"])
        rb = dt.date.fromisoformat(w["return_by"]) if w.get("return_by") else None
        for off in range(-flex, flex + 1, step):
            d = c + dt.timedelta(days=off)
            if rb and stay and d + dt.timedelta(days=stay) > rb:
                continue
            if stay and d + dt.timedelta(days=stay) > today + dt.timedelta(days=HORIZON["days"]):
                continue            # not on sale yet
            if d > today and d not in seen and not d.isoformat().startswith(skip_months):
                seen.add(d)
                yield d, (d + dt.timedelta(days=stay) if stay else None)


# modes: "list"       = real round-trip list from Google (one request per date pair; default)
#        "calendar"      = matrix from Google's date grid (real round-trip prices); flight lists use one-way sums
#        "oneway_sum" = everything estimated as outbound + return one-way fares
#        "roundtrip"  = per-trip round-trip query (not working with the current scraper)
PRICING = {"mode": "oneway_sum"}
HORIZON = {"days": 330}           # airlines sell roughly 330 days ahead; later dates have no prices (not a failure)
EXPAND = {"returns": True}          # fetch the return leg for the cheapest outbound of each date pair
CACHE = {}                          # (origin, dest, date, cabin, adults, children) -> ("ok", offers) | ("err", msg)


def _parse_offers(res):
    out = []
    for f in res:
        fl = f.flights
        if not fl:
            continue
        fly = sum(getattr(s, "duration", 0) or 0 for s in fl)
        lay = 0
        for x, y in zip(fl, fl[1:]):          # layover = next departure - arrival, both in the local time of the connection airport
            try:
                arr = dt.datetime(*x.arrival.date, *x.arrival.time)
                dep_ = dt.datetime(*y.departure.date, *y.departure.time)
                lay += max(0, int((dep_ - arr).total_seconds() // 60))
            except Exception:
                pass
        out.append({
            "price": f.price, "airlines": f.airlines, "stops": len(fl) - 1,
            "duration": fly + lay,
            "via": [s.to_airport.code for s in fl[:-1]],
            "dep": "%04d-%02d-%02d %02d:%02d" % (*fl[0].departure.date, *fl[0].departure.time),
            "arr": "%04d-%02d-%02d %02d:%02d" % (*fl[-1].arrival.date, *fl[-1].arrival.time),
        })
    return [o for o in out if o["price"]]


def _net(route, legs, trip, currency):
    """One real request to Google (followed by human-like pacing). Stops are NOT sent to Google
    (its stop filter errors out); they are filtered locally."""
    from fast_flights import FlightQuery, Passengers, create_query, get_flights
    q = create_query(flights=[FlightQuery(date=d.isoformat(), from_airport=a, to_airport=b) for d, a, b in legs],
                     seat=route.get("cabin", "economy"), trip=trip,
                     passengers=Passengers(adults=route.get("adults", 1), children=route.get("children", 0)),
                     language="en-US", currency=currency)
    try:
        try:
            res = get_flights(q)
        except (TypeError, AttributeError, IndexError, KeyError) as e:
            # parser hit a page without flight data (no results / format change)
            raise RuntimeError(f"unexpected Google response structure ({type(e).__name__}: {e})") from e
        return _parse_offers(res)
    finally:
        pace()


def oneway(route, a, b, date, currency):
    key = (a, b, date.isoformat(), route.get("cabin"), route.get("adults"), route.get("children"))
    if key not in CACHE:
        try:
            CACHE[key] = ("ok", _net(route, [(date, a, b)], "one-way", currency))
        except Exception as e:
            CACHE[key] = ("err", f"{type(e).__name__}: {str(e)[:120]}")
    kind, val = CACHE[key]
    if kind == "err":
        raise RuntimeError(val)
    return val


def _ok_stops(route, offers):
    ms = route.get("max_stops")
    return [o for o in offers if ms is None or o["stops"] <= ms]


_SF = {}
AIRPORTS = {}                       # code -> full name, written once to docs/data/airports.json (not repeated per leg)


def _sf():
    if "sf" not in _SF:
        from fli.search import SearchFlights
        _SF["sf"] = SearchFlights()
    return _SF["sf"]


def fli_list(route, dep, ret, currency, expand=True):
    """Real Google result list (what the Flights page shows): for a round trip each row is an outbound
    itinerary priced as the full round trip. Stops are filtered by Google (correct enum); transient
    error responses are retried."""
    from fli.models import (Airport, FlightSearchFilters, FlightSegment, MaxStops, PassengerInfo,
                            SeatType, TripType)
    o, d = Airport[route["origin"]], Airport[route["dest"]]
    stops = {None: MaxStops.ANY, 0: MaxStops.NON_STOP, 1: MaxStops.ONE_STOP_OR_FEWER,
             2: MaxStops.TWO_OR_FEWER_STOPS}[route.get("max_stops")]
    seat = {"economy": SeatType.ECONOMY, "premium-economy": SeatType.PREMIUM_ECONOMY,
            "business": SeatType.BUSINESS, "first": SeatType.FIRST}[route.get("cabin", "economy")]
    segs = [FlightSegment(departure_airport=[[o, 0]], arrival_airport=[[d, 0]], travel_date=dep.isoformat())]
    if ret:
        segs.append(FlightSegment(departure_airport=[[d, 0]], arrival_airport=[[o, 0]], travel_date=ret.isoformat()))
    f = FlightSearchFilters(trip_type=TripType.ROUND_TRIP if ret else TripType.ONE_WAY,
                            passenger_info=PassengerInfo(adults=route.get("adults", 1), children=route.get("children", 0)),
                            flight_segments=segs, stops=stops, seat_type=seat)
    guard()
    res, last = None, None
    for attempt in range(2):
        try:
            res = _sf()._fetch_flights(f, currency=currency, language=None, country=None, capture_session=False)
        except Exception as e:
            last, res = e, None
        finally:
            pace()
        if res:
            break
        time.sleep(random.uniform(3, 6) * PACE["scale"])
    if not res:
        NET["fails"] += 1
        raise RuntimeError(f"no result after 2 attempts ({type(last).__name__ + ': ' + str(last)[:100] if last else 'empty/error response'})")
    NET["fails"] = 0
    pairs = _fli_offers(res)
    offers = [p[0] for p in pairs]
    do_expand = expand(offers[0]["price"]) if (callable(expand) and offers) else bool(expand)
    if ret and do_expand and EXPAND["returns"] and pairs:
        try:                                    # full round trip for the cheapest outbound: also fetch its return leg
            rr = _fli_return(route, f, pairs[0][1], currency)
            if rr:
                pairs[0][0]["ret"] = rr
        except Exception:
            pass
    return offers


def _ap(airport, google_name):
    AIRPORTS.setdefault(airport.name, google_name or airport.value)
    return airport.name


def _save_airports():
    p = DATA / "airports.json"
    try:
        cur = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        cur = {}
    if AIRPORTS and any(cur.get(k) != v for k, v in AIRPORTS.items()):
        cur.update(AIRPORTS)
        p.write_text(json.dumps(dict(sorted(cur.items())), ensure_ascii=False, separators=(",", ":")))


def _itin(x):
    """One itinerary (FlightResult) as a compact dict, incl. per-leg details."""
    legs = x.legs
    airlines = []
    for l in legs:
        n = getattr(l.airline, "value", str(l.airline))
        if n not in airlines:
            airlines.append(n)
    return {
        "airlines": airlines, "stops": x.stops, "duration": x.duration,
        "via": [l.arrival_airport.name for l in legs[:-1]],
        "dep": legs[0].departure_datetime.strftime("%Y-%m-%d %H:%M"),
        "arr": legs[-1].arrival_datetime.strftime("%Y-%m-%d %H:%M"),
        "lay": [getattr(v, "duration", None) for v in (x.layovers or [])],
        "legs": [{"a": _ap(l.departure_airport, l.departure_airport_name),
                  "b": _ap(l.arrival_airport, l.arrival_airport_name),
                  "dep": l.departure_datetime.strftime("%Y-%m-%dT%H:%M:%S"),
                  "arr": l.arrival_datetime.strftime("%Y-%m-%dT%H:%M:%S"),
                  "min": l.duration, "al": getattr(l.airline, "value", str(l.airline)), "c": getattr(l.airline, "name", ""),
                  "no": l.flight_number, "ac": l.aircraft} for l in legs],
    }


def _fli_offers(res):
    """-> list of (offer_dict, FlightResult) sorted by price."""
    out = []
    for x in res:
        if x.price is None or not x.legs:
            continue
        o = _itin(x)
        o["price"] = int(round(x.price))
        out.append((o, x))
    out.sort(key=lambda p: p[0]["price"])
    return out


def _fli_return(route, f_out, outbound_fr, currency):
    """Return-leg options for a chosen outbound (one extra request); cheapest option as an itinerary dict."""
    from copy import deepcopy
    f2 = deepcopy(f_out)
    f2.flight_segments[0].selected_flight = outbound_fr
    res, last = None, None
    for attempt in range(2):
        try:
            res = _sf()._fetch_flights(f2, currency=currency, language=None, country=None, capture_session=False)
        except Exception as e:
            last, res = e, None
        finally:
            pace()
        if res:
            break
        time.sleep(random.uniform(3, 6) * PACE["scale"])
    if not res:
        return None
    best = min((x for x in res if x.price is not None and x.legs), key=lambda x: x.price, default=None)
    if best is None:
        return None
    r = _itin(best)
    r["total"] = int(round(best.price))
    return r


def search(route, dep, ret, currency, expand=True):
    """Offers for a trip. "list": real Google round-trip prices (default). oneway_sum: legacy estimate."""
    if PRICING["mode"] == "list":
        return _ok_stops(route, fli_list(route, dep, ret, currency, expand))
    if PRICING["mode"] == "roundtrip" and ret:
        legs = [(dep, route["origin"], route["dest"]), (ret, route["dest"], route["origin"])]
        return _ok_stops(route, _net(route, legs, "round-trip", currency))
    out = _ok_stops(route, oneway(route, route["origin"], route["dest"], dep, currency))
    if not ret:
        return out
    back = _ok_stops(route, oneway(route, route["dest"], route["origin"], ret, currency))
    if not out or not back:
        return []
    bmin = min(o["price"] for o in back)
    return [{**o, "price": o["price"] + bmin, "est": True} for o in out]


def calendar_prices(route, first, last, duration, currency):
    """Google 'date grid' (GetCalendarGraph): real round-trip price for every departure date in
    [first, last] with return = departure + duration. Up to 61 days per request; stop filter is sent
    to Google using its correct enum. Returns {departure_iso: price}."""
    from fli.models import (Airport, DateSearchFilters, FlightSegment, MaxStops, PassengerInfo,
                            SeatType, TripType)
    from fli.search import SearchDates
    stops = {None: MaxStops.ANY, 0: MaxStops.NON_STOP, 1: MaxStops.ONE_STOP_OR_FEWER,
             2: MaxStops.TWO_OR_FEWER_STOPS}[route.get("max_stops")]
    seat = {"economy": SeatType.ECONOMY, "premium-economy": SeatType.PREMIUM_ECONOMY,
            "business": SeatType.BUSINESS, "first": SeatType.FIRST}[route.get("cabin", "economy")]
    o, d = Airport[route["origin"]], Airport[route["dest"]]
    first = max(first, dt.date.today() + dt.timedelta(days=1))
    if first > last:
        return {}
    f = DateSearchFilters(
        trip_type=TripType.ROUND_TRIP,
        passenger_info=PassengerInfo(adults=route.get("adults", 1), children=route.get("children", 0)),
        flight_segments=[
            FlightSegment(departure_airport=[[o, 0]], arrival_airport=[[d, 0]], travel_date=first.isoformat()),
            FlightSegment(departure_airport=[[d, 0]], arrival_airport=[[o, 0]],
                          travel_date=(first + dt.timedelta(days=duration)).isoformat())],
        stops=stops, seat_type=seat, from_date=first.isoformat(), to_date=last.isoformat(), duration=duration)
    try:
        res = SearchDates().search(f, currency=currency)
    finally:
        pace()
    out = {}
    for r in res or []:
        out[r.date[0].date().isoformat()] = int(round(r.price))
    return out


def run_matrix_calendar(route, cfg, cur, doc, today, errors):
    """Whole matrix from ~7 calendar requests per month (one per trip length)."""
    mx = cfg["matrix"]
    offs, stay = mx["return_offsets"], cfg.get("stay_days")
    grid, n_ok, fails = {}, 0, 0
    for mo in [sp["month"] for sp in month_specs(mx)]:
        first = dt.date.fromisoformat(mo + "-01")
        last = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        cols = {}
        for off in offs:
            if expired():
                errors.append(f"{route['id']} calendar: time budget reached")
                break
            try:
                cols[off] = calendar_prices(route, first, last, off, cur)
                if cols[off]:
                    n_ok += 1
                    fails = 0
                else:
                    fails += 1
            except Exception as e:
                cols[off] = {}
                fails += 1
                errors.append(f"{route['id']} calendar {mo} +{off}d: {type(e).__name__}: {str(e)[:120]}")
            if fails >= 6 and n_ok == 0:
                return 0                       # calendar not working at all -> caller falls back
        day = first
        while day <= last:
            k = day.isoformat()
            row = [cols.get(o, {}).get(k) for o in offs]
            if any(v is not None for v in row):
                grid[k] = row
                if stay in offs and row[offs.index(stay)] is not None:
                    doc["history"].setdefault(k, {})[today] = row[offs.index(stay)]
            day += dt.timedelta(days=1)
    if n_ok:
        mdir = DATA / "matrix" / route["id"]
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / f"{today}.json").write_text(json.dumps(
            {"months": [sp["month"] for sp in month_specs(mx)], "offsets": offs, "currency": cur, "pricing": "calendar",
             "p": dict(sorted(grid.items()))}, separators=(",", ":")))
        idx_path = mdir / "index.json"
        idx = json.loads(idx_path.read_text()) if idx_path.exists() else {"snapshots": []}
        idx["months"] = [sp["month"] for sp in month_specs(mx)]
        if today not in idx["snapshots"]:
            idx["snapshots"].append(today)
        idx_path.write_text(json.dumps(idx))
    return n_ok


def run_matrix(route, cfg, cur, doc, today, errors, flush=None):
    if PRICING["mode"] == "calendar":
        n = run_matrix_calendar(route, cfg, cur, doc, today, errors)
        if n:
            doc["route"]["pricing"] = "calendar"
            return n
        errors.append(f"{route['id']}: calendar failed, falling back to one-way estimates")
        PRICING["mode"] = "oneway_sum"
        doc["route"]["pricing"] = "oneway_sum"
    return run_matrix_cells(route, cfg, cur, doc, today, errors, flush)


def _pick(offers, n=3):
    """Cheapest n offers plus the fastest one (keeps the file small but still useful)."""
    by_price = sorted(offers, key=lambda o: o["price"])
    keep = by_price[:n]
    fastest = min(offers, key=lambda o: (o["duration"], o["price"]))
    if fastest not in keep:
        keep.append(fastest)
    return keep


def run_matrix_cells(route, cfg, cur, doc, today, errors, flush=None):
    """Depart x stay price grid.
    - calendar columns (e.g. 32 days): Google's date grid, ONE request per month, dense (every date) and rarely throttled;
    - list columns (e.g. 42 days): exact prices from the result list, sampled dates only, with flight details.
    Resumable: cells already stored for today are kept; progress is committed periodically."""
    mx = cfg["matrix"]
    cal_offs = list(mx.get("calendar_offsets", []))
    list_on = route["id"] in mx.get("list_routes", mx.get("routes", []))
    list_offs = list(mx["return_offsets"]) if list_on else []
    allo = cal_offs + list_offs
    nc = len(cal_offs)
    stay = cfg.get("stay_days")
    mdir = DATA / "matrix" / route["id"]
    snap_path, offers_path = mdir / f"{today}.json", mdir / "offers.json"
    grid, cells = {}, {}
    try:
        if snap_path.exists():
            prev = json.loads(snap_path.read_text())
            if prev.get("offsets") == allo:
                grid = prev["p"]
        if offers_path.exists():
            po = json.loads(offers_path.read_text())
            if po.get("_date") == today:
                cells = {k: v for k, v in po.items() if k != "_date"}
    except Exception:
        grid, cells = {}, {}
    n_prev = sum(1 for r in grid.values() for v in r if v is not None)
    stat = {"new": 0}
    td = dt.date.today()
    days, rby = [], {}
    for sp in month_specs(mx):
        first = dt.date.fromisoformat(sp["month"] + "-01")
        d = first.replace(day=sp["from_day"])
        while d.month == first.month:
            if d > td:
                days.append(d)
                rby[d] = dt.date.fromisoformat(sp["return_by"]) if sp["return_by"] else None
            d += dt.timedelta(days=1)

    step = int(mx.get("date_step", 1))
    pct = cfg.get("expand_within_pct", 100)

    def want_expand(price):
        prices = [v for r in grid.values() for v in r[nc:] if v is not None] or [v for r in grid.values() for v in r if v is not None]
        return (not prices) or price <= min(prices) * (1 + pct / 100)

    def ok_dates(d, off):
        ret = d + dt.timedelta(days=off)
        if rby.get(d) and ret > rby[d]:
            return False                # after the allowed return date for this month
        return ret <= td + dt.timedelta(days=HORIZON["days"])   # else: not on sale yet (skipped silently)

    def valid(d, off, is_cal):
        if not is_cal and step > 1 and d.toordinal() % step != 0:
            return False                # sampled dates only (same dates every day -> clean time series)
        return ok_dates(d, off)

    for d in days:                      # a row exists for every date that has at least one valid combination
        if any(valid(d, o, i < nc) for i, o in enumerate(allo)):
            grid.setdefault(d.isoformat(), [None] * len(allo))

    def write_files():
        if n_prev + stat["new"] == 0:
            return
        mdir.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(json.dumps(
            {"months": [sp["month"] for sp in month_specs(mx)], "offsets": allo, "cal": cal_offs, "currency": cur,
             "pricing": PRICING["mode"], "p": dict(sorted(grid.items()))}, separators=(",", ":")))
        idx_path = mdir / "index.json"
        idx = json.loads(idx_path.read_text()) if idx_path.exists() else {"snapshots": []}
        idx["months"] = [sp["month"] for sp in month_specs(mx)]
        if today not in idx["snapshots"]:
            idx["snapshots"].append(today)
        idx_path.write_text(json.dumps(idx))
        if list_on:
            offers_path.write_text(json.dumps({"_date": today, **dict(sorted(cells.items()))},
                                              separators=(",", ":"), ensure_ascii=False))

    def save_all():
        write_files()
        if flush:
            flush()

    def pending():
        return sum(1 for d in days for i, off in enumerate(allo)
                   if valid(d, off, i < nc) and grid[d.isoformat()][i] is None)

    # ---- 1) dense calendar columns first: one request per (month, stay) ----
    for ci, off in enumerate(cal_offs):
        for sp in month_specs(mx):
            mdays = [d for d in days if d.strftime("%Y-%m") == sp["month"] and valid(d, off, True)
                     and grid[d.isoformat()][ci] is None]
            if not mdays:
                continue
            if expired():
                break
            try:
                res = calendar_prices(route, min(mdays), max(mdays), off, cur)
            except Exception as e:
                errors.append(f"{route['id']} calendar {sp['month']} +{off}d: {type(e).__name__}: {str(e)[:100]}")
                continue
            for d in mdays:
                price = res.get(d.isoformat())
                if price:
                    grid[d.isoformat()][ci] = price
                    stat["new"] += 1
                    doc["history"].setdefault(d.isoformat(), {})[today] = price     # dense daily series
        save_all()

    # ---- 2) exact list columns (sampled dates), the 'stay' column first ----
    order = sorted(range(len(list_offs)), key=lambda j: (list_offs[j] != stay, j))
    for j in order:
        off, i = list_offs[j], nc + j
        todo = [d for d in days if valid(d, off, False) and grid[d.isoformat()][i] is None]
        random.shuffle(todo)            # random order: no sequential scanning pattern
        for d in todo:
            if expired():
                errors.append(f"{route['id']} matrix: stopped for this run (request quota / time budget / throttling); the next run resumes")
                save_all()
                PENDING["n"] += pending()
                return n_prev + stat["new"]
            k, ret = d.isoformat(), d + dt.timedelta(days=off)
            try:
                offers = search(route, d, ret, cur, expand=(want_expand if off == stay else False))
            except Exception as e:
                errors.append(f"{route['id']} matrix {d}->{ret}: {type(e).__name__}: {str(e)[:100]}")
                continue
            if offers:
                stat["new"] += 1
                grid[k][i] = min(o["price"] for o in offers)
                cells.setdefault(k, {})[str(off)] = {"ret": ret.isoformat(), "offers": _pick(offers)}
                if off == stay:         # feeds the overview flight list
                    doc["latest"][k] = {"return": ret.isoformat(), "offers": sorted(offers, key=lambda o: o["price"])[:MAX_OFFERS]}
                    doc["history"].setdefault(k, {}).setdefault(today, grid[k][i])   # never overrides the dense calendar value
            checkpoint(save_all)
    save_all()
    PENDING["n"] += pending()
    return n_prev + stat["new"]


def preflight():
    """Fail loudly (red run) if the scraper can't even be imported - not a silent 'all failed'."""
    try:
        import fast_flights  # noqa: F401
    except Exception as e:
        print(f"FATAL: cannot import fast_flights: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    preflight()
    cfg = json.loads((ROOT / "config.json").read_text())
    cur = cfg.get("currency", "CAD")
    DATA.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
    today = dt.date.today().isoformat()
    ok = fail = 0
    errors = []
    pace_init(cfg)
    PRICING["mode"] = cfg.get("pricing", "oneway_sum")
    EXPAND["returns"] = bool(cfg.get("expand_returns", True))
    HORIZON["days"] = int(cfg.get("booking_horizon_days", 330))
    routes = list(cfg["routes"])
    random.shuffle(routes)          # each run starts with a random route, so all routes make progress
    for route in routes:
        path = DATA / f"{route['id']}.json"
        doc = json.loads(path.read_text()) if path.exists() else {"history": {}, "latest": {}}
        doc["route"] = {**route, "windows": cfg["windows"], "stay_days": cfg.get("stay_days"), "pricing": PRICING["mode"]}
        doc["currency"] = cur
        doc["updated"] = now
        doc["latest"] = {k: v for k, v in doc.get("latest", {}).items() if k > today}   # departed dates: details are useless
        def save_doc(doc=doc, path=path):
            _save_airports()
            cutoff = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
            for k, v in doc["history"].items():
                doc["history"][k] = {d: p for d, p in v.items() if d >= cutoff}
            path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))

        pairs = list(dates_for(route, cfg))
        random.shuffle(pairs)
        for dep, ret in pairs:
            if expired():
                errors.append(f"{route['id']}: time budget reached")
                break
            key = dep.isoformat()
            if doc["history"].get(key, {}).get(today) is not None and key in doc["latest"]:
                continue                # already collected today by an earlier run
            def want(price, doc=doc):
                pr = [v.get(today) for v in doc["history"].values() if v.get(today) is not None]
                return (not pr) or price <= min(pr) * (1 + cfg.get("expand_within_pct", 100) / 100)
            try:
                offers = search(route, dep, ret, cur, expand=want)
            except Exception as e:  # network block, parse change, no results...
                fail += 1
                errors.append(f"{route['id']} {key}: {type(e).__name__}: {str(e)[:120]}")
                continue
            if not offers:
                fail += 1
                errors.append(f"{route['id']} {key}: no offers")
                continue
            offers.sort(key=lambda o: o["price"])
            doc["latest"][key] = {"return": ret.isoformat() if ret else None,
                                  "offers": offers[:MAX_OFFERS]}
            if not (PRICING["mode"] == "calendar" and route["id"] in (cfg.get("matrix") or {}).get("routes", [])):
                doc["history"].setdefault(key, {})[today] = min(o["price"] for o in offers)
            ok += 1
            checkpoint(save_doc)
        PENDING["n"] += sum(1 for dep, ret in pairs
                            if not (doc["history"].get(dep.isoformat(), {}).get(today) is not None and dep.isoformat() in doc["latest"]))
        if route["id"] in (cfg.get("matrix") or {}).get("routes", []):
            n = run_matrix(route, cfg, cur, doc, today, errors, flush=save_doc)
            ok += n
            if not n:
                fail += 1
        save_doc()
        checkpoint(save_doc, force=True)
    (DATA / "status.json").write_text(json.dumps(
        {"updated": now, "ok": ok, "failed": fail, "errors": errors[:30]}, indent=1))
    prev = {}
    try:
        prev = json.loads((DATA / "progress.json").read_text())
    except Exception:
        pass
    runs_today = (prev.get("runs_today", 0) + 1) if prev.get("date") == today else 1
    zero_streak = (prev.get("zero_streak", 0) + 1 if prev.get("date") == today else 1) if (ok == 0 and PACE["count"] > 0) else 0
    stop = "done" if PENDING["n"] == 0 else ("throttled" if NET["stop"] else "quota")
    (DATA / "progress.json").write_text(json.dumps(
        {"date": today, "pending": PENDING["n"], "updated": now, "ok": ok, "requests": PACE["count"],
         "stop": stop, "runs_today": runs_today, "zero_streak": zero_streak}, indent=1))
    print(f"ok={ok} failed={fail} requests={PACE['count']} quota={PACE['quota']}")
    for e in errors[:10]:
        print(" ", e)
    # exit 0 so data is committed; alert.py reports total failure
    return 0


if __name__ == "__main__":
    sys.exit(main())
