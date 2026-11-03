"""Daily price collector. Reads config.json, queries Google Flights via fast-flights,
appends to docs/data/<route>.json. Never crashes the whole run on one failure."""
import json, sys, time, datetime as dt, pathlib, random

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
MAX_OFFERS = 12
HISTORY_DAYS = 800


def dates_for(route):
    c = dt.date.fromisoformat(route["depart"])
    r = dt.date.fromisoformat(route["return"]) if route.get("return") else None
    step, flex = route.get("sample_step", 3), route.get("flex_days", 7)
    today = dt.date.today()
    for off in range(-flex, flex + 1, step):
        d = c + dt.timedelta(days=off)
        if d > today:
            yield d, (r + dt.timedelta(days=off) if r else None)


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


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    cur = cfg.get("currency", "CAD")
    DATA.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    today = dt.date.today().isoformat()
    ok = fail = 0
    errors = []
    for route in cfg["routes"]:
        path = DATA / f"{route['id']}.json"
        doc = json.loads(path.read_text()) if path.exists() else {"history": {}, "latest": {}}
        doc["route"] = route
        doc["currency"] = cur
        doc["updated"] = now
        for dep, ret in dates_for(route):
            key = dep.isoformat()
            try:
                offers = search(route, dep, ret, cur)
            except Exception as e:  # network block, parse change, no results...
                fail += 1
                errors.append(f"{route['id']} {key}: {type(e).__name__}: {str(e)[:120]}")
                time.sleep(2)
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
            time.sleep(random.uniform(2, 5))
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
