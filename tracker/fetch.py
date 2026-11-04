"""Daily price collector. Reads config.json, queries Google Flights via fast-flights,
appends to docs/data/<route>.json. Never crashes the whole run on one failure."""
import json, sys, time, datetime as dt, pathlib, random

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
MAX_OFFERS = 12
HISTORY_DAYS = 800


PACE = {"left": 0, "deadline": None, "cfg": {}, "scale": 1.0, "batches": 0}


def pace_init(cfg):
    """Human-like pacing: queries in random-sized batches, short random gaps inside a batch,
    a random multi-minute pause between batches, and a hard time budget."""
    import os
    p = {"batch_size_range": [40, 80], "pause_minutes_range": [1, 3],
         "query_sleep_seconds_range": [1, 3], "max_runtime_minutes": 200}
    p.update(cfg.get("pacing") or {})
    PACE.update(cfg=p, left=random.randint(*p["batch_size_range"]), batches=0,
                scale=float(os.environ.get("PACE_SCALE", "1")),
                deadline=time.time() + p["max_runtime_minutes"] * 60 * float(os.environ.get("PACE_SCALE", "1")))


def expired():
    return PACE["deadline"] is not None and time.time() > PACE["deadline"]


def pace():
    p, sc = PACE["cfg"], PACE["scale"]
    PACE["left"] -= 1
    if PACE["left"] <= 0:
        PACE["batches"] += 1
        pause = random.uniform(*p["pause_minutes_range"]) * 60 * sc
        print(f"[pace] batch {PACE['batches']} done, pausing {pause / 60:.1f} min", flush=True)
        time.sleep(pause)
        PACE["left"] = random.randint(*p["batch_size_range"])
    else:
        time.sleep(random.uniform(*p["query_sleep_seconds_range"]) * sc)


def dates_for(route, cfg):
    mx = cfg.get("matrix") or {}
    skip_months = tuple(mx.get("months", [])) if (route["id"] in mx.get("routes", []) and PRICING["mode"] != "calendar") else ()
    """All (depart, return) pairs: every window's centre +/- flex, return = depart + stay."""
    flex = route.get("flex_days", cfg.get("flex_days", 7))
    step = route.get("sample_step", cfg.get("sample_step", 2))
    stay = route.get("stay_days", cfg.get("stay_days"))
    today = dt.date.today()
    seen = set()
    for w in cfg["windows"]:
        c = dt.date.fromisoformat(w["center"])
        for off in range(-flex, flex + 1, step):
            d = c + dt.timedelta(days=off)
            if d > today and d not in seen and not d.isoformat().startswith(skip_months):
                seen.add(d)
                yield d, (d + dt.timedelta(days=stay) if stay else None)


# modes: "list"       = real round-trip list from Google (one request per date pair; default)
#        "calendar"      = matrix from Google's date grid (real round-trip prices); flight lists use one-way sums
#        "oneway_sum" = everything estimated as outbound + return one-way fares
#        "roundtrip"  = per-trip round-trip query (not working with the current scraper)
PRICING = {"mode": "oneway_sum"}
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


def _sf():
    if "sf" not in _SF:
        from fli.search import SearchFlights
        _SF["sf"] = SearchFlights()
    return _SF["sf"]


def fli_list(route, dep, ret, currency):
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
    res, last = None, None
    for attempt in range(3):
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
        raise RuntimeError(f"no result after 3 attempts ({type(last).__name__ + ': ' + str(last)[:100] if last else 'empty/error response'})")
    pairs = _fli_offers(res)
    offers = [p[0] for p in pairs]
    if ret and EXPAND["returns"] and pairs:
        try:                                    # full round trip for the cheapest outbound: also fetch its return leg
            rr = _fli_return(route, f, pairs[0][1], currency)
            if rr:
                pairs[0][0]["ret"] = rr
        except Exception:
            pass
    return offers


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
        "legs": [{"a": l.departure_airport.name, "an": l.departure_airport_name or l.departure_airport.value,
                  "b": l.arrival_airport.name, "bn": l.arrival_airport_name or l.arrival_airport.value,
                  "dep": l.departure_datetime.strftime("%Y-%m-%dT%H:%M:%S"),
                  "arr": l.arrival_datetime.strftime("%Y-%m-%dT%H:%M:%S"),
                  "min": l.duration, "al": getattr(l.airline, "value", str(l.airline)),
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
    for attempt in range(3):
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


def search(route, dep, ret, currency):
    """Offers for a trip. "list": real Google round-trip prices (default). oneway_sum: legacy estimate."""
    if PRICING["mode"] == "list":
        return _ok_stops(route, fli_list(route, dep, ret, currency))
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
    for mo in mx["months"]:
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
            {"months": mx["months"], "offsets": offs, "currency": cur, "pricing": "calendar",
             "p": dict(sorted(grid.items()))}, separators=(",", ":")))
        idx_path = mdir / "index.json"
        idx = json.loads(idx_path.read_text()) if idx_path.exists() else {"months": mx["months"], "snapshots": []}
        if today not in idx["snapshots"]:
            idx["snapshots"].append(today)
        idx_path.write_text(json.dumps(idx))
    return n_ok


def run_matrix(route, cfg, cur, doc, today, errors):
    if PRICING["mode"] == "calendar":
        n = run_matrix_calendar(route, cfg, cur, doc, today, errors)
        if n:
            doc["route"]["pricing"] = "calendar"
            return n
        errors.append(f"{route['id']}: calendar failed, falling back to one-way estimates")
        PRICING["mode"] = "oneway_sum"
        doc["route"]["pricing"] = "oneway_sum"
    return run_matrix_cells(route, cfg, cur, doc, today, errors)


def _pick(offers, n=4):
    """Cheapest n offers plus the fastest one (keeps the file small but still useful)."""
    by_price = sorted(offers, key=lambda o: o["price"])
    keep = by_price[:n]
    fastest = min(offers, key=lambda o: (o["duration"], o["price"]))
    if fastest not in keep:
        keep.append(fastest)
    return keep


def run_matrix_cells(route, cfg, cur, doc, today, errors):
    """Depart x return price grid for one month. One query per cell; stores min price per cell."""
    mx = cfg["matrix"]
    offs = mx["return_offsets"]
    stay = cfg.get("stay_days")
    grid, fails_in_row, n_ok = {}, 0, 0
    cells = {}                      # dep -> {offset: {ret, offers}} (latest day only; powers click-through)
    days = []
    for mo in mx["months"]:
        d = dt.date.fromisoformat(mo + "-01")
        while d.month == dt.date.fromisoformat(mo + "-01").month:
            days.append(d)
            d += dt.timedelta(days=1)
    random.shuffle(days)            # random order: no sequential scanning pattern
    for d in days:
        if expired():
            errors.append(f"{route['id']} matrix: time budget reached, remaining dates skipped")
            break
        if d > dt.date.today():
            row = []
            for off in offs:
                if fails_in_row >= 25:      # looks blocked: stop wasting runner time
                    row.append(None)
                    continue
                ret = d + dt.timedelta(days=off)
                try:
                    offers = search(route, d, ret, cur)
                except Exception as e:
                    offers = []
                    errors.append(f"{route['id']} matrix {d}->{ret}: {type(e).__name__}: {str(e)[:100]}")
                if offers:
                    fails_in_row = 0
                    n_ok += 1
                    row.append(min(o["price"] for o in offers))
                    cells.setdefault(d.isoformat(), {})[str(off)] = {"ret": ret.isoformat(), "offers": _pick(offers)}
                    if off == stay:         # feed the weekday / lead-time analysis too
                        doc["latest"][d.isoformat()] = {"return": ret.isoformat(), "offers": sorted(offers, key=lambda o: o["price"])[:MAX_OFFERS]}
                        doc["history"].setdefault(d.isoformat(), {})[today] = row[-1]
                else:
                    fails_in_row += 1
                    row.append(None)
            grid[d.isoformat()] = row
    if n_ok:
        mdir = DATA / "matrix" / route["id"]
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / f"{today}.json").write_text(json.dumps(
            {"months": mx["months"], "offsets": offs, "currency": cur, "pricing": PRICING["mode"], "p": dict(sorted(grid.items()))},
            separators=(",", ":")))
        idx_path = mdir / "index.json"
        idx = json.loads(idx_path.read_text()) if idx_path.exists() else {"months": mx["months"], "snapshots": []}
        if today not in idx["snapshots"]:
            idx["snapshots"].append(today)
        idx_path.write_text(json.dumps(idx))
        if cells:
            (mdir / "offers.json").write_text(json.dumps(dict(sorted(cells.items())), separators=(",", ":"), ensure_ascii=False))
    return n_ok


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
    for route in cfg["routes"]:
        path = DATA / f"{route['id']}.json"
        doc = json.loads(path.read_text()) if path.exists() else {"history": {}, "latest": {}}
        doc["route"] = {**route, "windows": cfg["windows"], "stay_days": cfg.get("stay_days"), "pricing": PRICING["mode"]}
        doc["currency"] = cur
        doc["updated"] = now
        pairs = list(dates_for(route, cfg))
        random.shuffle(pairs)
        for dep, ret in pairs:
            if expired():
                errors.append(f"{route['id']}: time budget reached")
                break
            key = dep.isoformat()
            try:
                offers = search(route, dep, ret, cur)
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
        if route["id"] in (cfg.get("matrix") or {}).get("routes", []):
            n = run_matrix(route, cfg, cur, doc, today, errors)
            ok += n
            if not n:
                fail += 1
        cutoff = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
        for k, v in doc["history"].items():
            doc["history"][k] = {d: p for d, p in v.items() if d >= cutoff}
        path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    (DATA / "status.json").write_text(json.dumps(
        {"updated": now, "ok": ok, "failed": fail, "errors": errors[:30]}, indent=1))
    print(f"ok={ok} failed={fail}")
    for e in errors[:10]:
        print(" ", e)
    # exit 0 so data is committed; alert.py reports total failure
    return 0


if __name__ == "__main__":
    sys.exit(main())
