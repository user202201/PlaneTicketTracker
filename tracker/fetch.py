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
    skip_months = tuple(mx.get("months", [])) if route["id"] in mx.get("routes", []) else ()
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


def search(route, dep, ret, currency):
    from fast_flights import FlightQuery, Passengers, create_query, get_flights
    legs = [FlightQuery(date=dep.isoformat(), from_airport=route["origin"],
                        to_airport=route["dest"], max_stops=route.get("max_stops"))]
    if ret:
        legs.append(FlightQuery(date=ret.isoformat(), from_airport=route["dest"],
                                to_airport=route["origin"], max_stops=route.get("max_stops")))
    q = create_query(flights=legs, seat=route.get("cabin", "economy"),
                     trip="round-trip" if ret else "one-way",
                     passengers=Passengers(adults=route.get("adults", 1),
                                           children=route.get("children", 0)),
                     language="en-US", currency=currency)
    res = get_flights(q)
    out = []
    for f in res:
        fl = f.flights
        if not fl:
            continue
        dur = sum(getattr(s, "duration", 0) or 0 for s in fl)
        out.append({
            "price": f.price, "airlines": f.airlines, "stops": len(fl) - 1,
            "duration": dur,
            "via": [s.to_airport.code for s in fl[:-1]],
            "dep": "%04d-%02d-%02d %02d:%02d" % (*fl[0].departure.date, *fl[0].departure.time),
            "arr": "%04d-%02d-%02d %02d:%02d" % (*fl[-1].arrival.date, *fl[-1].arrival.time),
        })
    return [o for o in out if o["price"]]


def run_matrix(route, cfg, cur, doc, today, errors):
    """Depart x return price grid for one month. One query per cell; stores min price per cell."""
    mx = cfg["matrix"]
    offs = mx["return_offsets"]
    stay = cfg.get("stay_days")
    grid, fails_in_row, n_ok = {}, 0, 0
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
                    if off == stay:         # feed the weekday / lead-time analysis too
                        offers.sort(key=lambda o: (o["stops"], o["duration"], o["price"]))
                        doc["latest"][d.isoformat()] = {"return": ret.isoformat(), "offers": offers[:MAX_OFFERS]}
                        doc["history"].setdefault(d.isoformat(), {})[today] = row[-1]
                else:
                    fails_in_row += 1
                    row.append(None)
                pace()
            grid[d.isoformat()] = row
    if n_ok:
        mdir = DATA / "matrix" / route["id"]
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / f"{today}.json").write_text(json.dumps(
            {"months": mx["months"], "offsets": offs, "currency": cur, "p": dict(sorted(grid.items()))},
            separators=(",", ":")))
        idx_path = mdir / "index.json"
        idx = json.loads(idx_path.read_text()) if idx_path.exists() else {"months": mx["months"], "snapshots": []}
        if today not in idx["snapshots"]:
            idx["snapshots"].append(today)
        idx_path.write_text(json.dumps(idx))
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
    now = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    today = dt.date.today().isoformat()
    ok = fail = 0
    errors = []
    pace_init(cfg)
    for route in cfg["routes"]:
        path = DATA / f"{route['id']}.json"
        doc = json.loads(path.read_text()) if path.exists() else {"history": {}, "latest": {}}
        doc["route"] = {**route, "windows": cfg["windows"], "stay_days": cfg.get("stay_days")}
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
                pace()
                continue
            if not offers:
                fail += 1
                errors.append(f"{route['id']} {key}: no offers")
                continue
            offers.sort(key=lambda o: (o["stops"], o["duration"], o["price"]))
            doc["latest"][key] = {"return": ret.isoformat() if ret else None,
                                  "offers": offers[:MAX_OFFERS]}
            doc["history"].setdefault(key, {})[today] = min(o["price"] for o in offers)
            ok += 1
            pace()
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
