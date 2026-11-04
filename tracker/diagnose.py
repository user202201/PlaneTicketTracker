"""Quick diagnostic (~1 min): runs a handful of queries and records exactly what Google returns,
so we can tell 'blocked' from 'wrong query' from 'no inventory'. Writes docs/data/diagnose.json."""
import json, pathlib, datetime as dt, time, traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data" / "diagnose.json"


def shape(x, depth=0):
    if x is None:
        return None
    if isinstance(x, list):
        if depth >= 2:
            return f"list[{len(x)}]"
        return [shape(i, depth + 1) for i in x[:10]]
    return type(x).__name__


def run_case(name, legs, trip, stops, seat="economy", cur="CAD"):
    from fast_flights import FlightQuery, Passengers, create_query
    from fast_flights.fetcher import URL
    from fast_flights.parser import parse
    from primp import Client
    r = {"case": name}
    if isinstance(legs, str):               # natural-language query variant
        q = legs
        r["url"] = "q=" + legs
    else:
        q = create_query(flights=[FlightQuery(date=d, from_airport=a, to_airport=b, max_stops=stops) for d, a, b in legs],
                         seat=seat, trip=trip, passengers=Passengers(adults=1), language="en-US", currency=cur)
        r["url"] = q.url()
    try:
        c = Client(impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True)
        res = c.get(URL, params=({"q": q, "hl": "en-US", "curr": cur} if isinstance(q, str) else q.params()))
        html = res.text
        r.update(status=res.status_code, length=len(html), final_url=str(res.url)[:200])
        import re
        r["ds_scripts"] = sorted(set(re.findall(r'class="(ds:\d+)"', html)))
        r["has_ds1"] = 'class="ds:1"' in html or "ds:1" in html
        low = html.lower()
        r["hints"] = [k for k in ("consent", "unusual traffic", "captcha", "before you continue", "sorry") if k in low]
        from selectolax.lexbor import LexborHTMLParser
        title = LexborHTMLParser(html).css_first("title")
        r["title"] = title.text()[:120] if title else None
        node = LexborHTMLParser(html).css_first(r"script.ds\:1")
        if node is not None:
            js = node.text()
            data = js.split("data:", 1)[1].rsplit(",", 1)[0]
            r["error_flag"] = data.endswith("errorHasStatus: true")
            if not r["error_flag"]:
                payload = json.loads(data)
                r["payload_shape"] = shape(payload)
                try:
                    r["payload5"] = json.dumps(payload[5], ensure_ascii=False)[:3000]   # price-insight block (typical range / level / history?)
                except Exception:
                    pass
        try:
            flights = parse(html)
            r["n_flights"] = len(flights)
            r["sample"] = [{"price": f.price, "airlines": f.airlines, "legs": len(f.flights)} for f in flights[:3]]
        except Exception as e:
            r["parse_error"] = f"{type(e).__name__}: {e}"
        if not r["has_ds1"]:
            r["html_head"] = html[:400]
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {e}"
        r["trace"] = traceback.format_exc()[-600:]
    print(json.dumps({k: v for k, v in r.items() if k not in ("trace", "html_head", "payload_shape")}, ensure_ascii=False))
    return r


def calendar_case():
    """Google date grid (fli): real round-trip prices, <=1 stop, one request for a whole month."""
    r = {"case": "calendar grid YUL-PEK round-trip Mar 1-31, 42 days, <=1 stop"}
    try:
        import sys
        sys.path.insert(0, str(ROOT / "tracker"))
        import fetch
        route = {"origin": "YUL", "dest": "PEK", "max_stops": 1, "cabin": "economy", "adults": 1, "children": 0}
        fetch.pace = lambda: None
        res = fetch.calendar_prices(route, dt.date(2027, 3, 1), dt.date(2027, 3, 31), 42, "CAD")
        r["n_dates"] = len(res)
        r["sample"] = dict(list(sorted(res.items()))[:6])
        r["min"] = min(res.values()) if res else None
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {e}"
        r["trace"] = traceback.format_exc()[-800:]
    print(json.dumps({k: v for k, v in r.items() if k != "trace"}, ensure_ascii=False))
    return r


def fli_cases():
    """Real round-trip outbound list (what Google's page shows) + date-grid density experiments."""
    out = []
    try:
        from fli.models import (Airport, DateSearchFilters, FlightSearchFilters, FlightSegment, MaxStops,
                                PassengerInfo, SeatType, TripType)
        from fli.search import SearchDates, SearchFlights
    except Exception as e:
        return [{"case": "fli import", "error": f"{type(e).__name__}: {e}"}]
    o, d_ = Airport.YUL, Airport.PEK
    pax = PassengerInfo(adults=1)

    def seg(a, b, date):
        return FlightSegment(departure_airport=[[a, 0]], arrival_airport=[[b, 0]], travel_date=date)

    # A) round-trip outbound list, <=1 stop, real round-trip prices + leg details
    r = {"case": "fli round-trip list YUL-PEK Mar10/Apr21 <=1 stop"}
    try:
        t0 = time.time()
        f = FlightSearchFilters(trip_type=TripType.ROUND_TRIP, passenger_info=pax,
                                flight_segments=[seg(o, d_, "2027-03-10"), seg(d_, o, "2027-04-21")],
                                stops=MaxStops.ONE_STOP_OR_FEWER, seat_type=SeatType.ECONOMY)
        res = SearchFlights()._fetch_flights(f, currency="CAD", language=None, country=None, capture_session=False)
        r["seconds"] = round(time.time() - t0, 1)
        r["n"] = len(res or [])
        r["items"] = [{"price": x.price, "currency": x.currency, "stops": x.stops, "duration": x.duration,
                       "layovers": [getattr(l, "duration", None) for l in (x.layovers or [])],
                       "legs": [{"airline": getattr(l.airline, "value", str(l.airline)), "no": l.flight_number,
                                 "from": getattr(l.departure_airport, "name", str(l.departure_airport)),
                                 "to": getattr(l.arrival_airport, "name", str(l.arrival_airport)),
                                 "dep": l.departure_datetime.isoformat(), "arr": l.arrival_datetime.isoformat(),
                                 "min": l.duration, "aircraft": l.aircraft} for l in x.legs]}
                      for x in (res or [])[:4]]
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {e}"
        r["trace"] = traceback.format_exc()[-700:]
    # raw peek: what does the list endpoint really answer? (round trip and one-way)
    def peek(name, trip, segs):
        pr = {"case": "raw list peek: " + name}
        try:
            from fli.search._urls import with_locale_params
            sf = SearchFlights()
            f2 = FlightSearchFilters(trip_type=trip, passenger_info=pax, flight_segments=segs,
                                     stops=MaxStops.ONE_STOP_OR_FEWER, seat_type=SeatType.ECONOMY)
            url = with_locale_params(sf.BASE_URL, "CAD", None, None)
            resp = sf.client.post(url=url, data=f"f.req={f2.encode()}", impersonate="chrome", allow_redirects=True)
            pr.update(status=resp.status_code, length=len(resp.text), head=resp.text[:300].replace("\n", " | "))
            try:
                res2 = sf._fetch_flights(f2, currency="CAD", language=None, country=None, capture_session=False)
                pr["n"] = len(res2 or [])
                if res2:
                    pr["prices"] = [x.price for x in res2[:8]]
            except Exception as e:
                pr["fetch_error"] = f"{type(e).__name__}: {e}"[:300]
        except Exception as e:
            pr["error"] = f"{type(e).__name__}: {e}"[:300]
        print(json.dumps(pr, ensure_ascii=False)[:300])
        out.append(pr)
        time.sleep(3)

    peek("one-way YUL-PEK 2027-03-10", TripType.ONE_WAY, [seg(o, d_, "2027-03-10")])
    peek("round-trip YUL-PEK Mar10/Apr21", TripType.ROUND_TRIP, [seg(o, d_, "2027-03-10"), seg(d_, o, "2027-04-21")])
    peek("round-trip YUL-PEK Mar10/Apr09 (30d)", TripType.ROUND_TRIP, [seg(o, d_, "2027-03-10"), seg(d_, o, "2027-04-09")])
    out.append(r)
    time.sleep(3)

    # B) date-grid density experiments
    def grid(name, trip, stops, first, last, dur):
        r = {"case": "grid: " + name}
        try:
            segs = [seg(o, d_, first)]
            if trip == TripType.ROUND_TRIP:
                import datetime as _d
                segs.append(seg(d_, o, (_d.date.fromisoformat(first) + _d.timedelta(days=dur)).isoformat()))
            f = DateSearchFilters(trip_type=trip, passenger_info=pax, flight_segments=segs, stops=stops,
                                  seat_type=SeatType.ECONOMY, from_date=first, to_date=last,
                                  duration=dur if trip == TripType.ROUND_TRIP else None)
            res = SearchDates().search(f, currency="CAD") or []
            r["n_dates"] = len(res)
            r["sample"] = [[x.date[0].date().isoformat(), x.price] for x in res[:8]]
        except Exception as e:
            r["error"] = f"{type(e).__name__}: {e}"
        print(json.dumps(r, ensure_ascii=False)[:300])
        out.append(r)
        time.sleep(3)

    RT, OW = TripType.ROUND_TRIP, TripType.ONE_WAY
    grid("RT Mar1-31 42d <=1stop", RT, MaxStops.ONE_STOP_OR_FEWER, "2027-03-01", "2027-03-31", 42)
    grid("RT Mar1-31 42d ANY stops", RT, MaxStops.ANY, "2027-03-01", "2027-03-31", 42)
    grid("RT Mar1-7 42d <=1stop", RT, MaxStops.ONE_STOP_OR_FEWER, "2027-03-01", "2027-03-07", 42)
    grid("RT Mar1-31 30d ANY", RT, MaxStops.ANY, "2027-03-01", "2027-03-31", 30)
    grid("RT Dec1-31 14d <=1stop (near)", RT, MaxStops.ONE_STOP_OR_FEWER, "2026-12-01", "2026-12-31", 14)
    grid("OW Mar1-31 <=1stop", OW, MaxStops.ONE_STOP_OR_FEWER, "2027-03-01", "2027-03-31", 0)
    for dur in (31, 35, 38, 40, 45, 60):
        grid(f"RT Mar1-31 {dur}d ANY", RT, MaxStops.ANY, "2027-03-01", "2027-03-31", dur)
    for dur in (30, 35, 42):
        grid(f"RT Mar1-31 {dur}d <=1stop", RT, MaxStops.ONE_STOP_OR_FEWER, "2027-03-01", "2027-03-31", dur)
    return out


def list_case():
    """The exact trip the user checked on Google Flights: Mar 3 -> Apr 16, <=1 stop (Google showed CA$1,899)."""
    r = {"case": "list-mode check YUL-PEK 2027-03-03/2027-04-16 <=1 stop (Google page: 1899)"}
    try:
        import sys
        sys.path.insert(0, str(ROOT / "tracker"))
        import fetch
        fetch.pace = lambda: None
        route = {"origin": "YUL", "dest": "PEK", "max_stops": 1, "cabin": "economy", "adults": 1, "children": 0}
        offers = fetch.fli_list(route, dt.date(2027, 3, 3), dt.date(2027, 4, 16), "CAD")
        r["n"] = len(offers)
        r["cheapest"] = [{"price": o["price"], "airlines": o["airlines"], "stops": o["stops"], "dur": o["duration"],
                          "via": o["via"], "lay": o["lay"], "legs": [(l["al"] + " " + str(l["no"]), l["a"] + ">" + l["b"], l["dep"][11:16], l["arr"][11:16]) for l in o["legs"]]}
                         for o in offers[:3]]
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {e}"
        r["trace"] = traceback.format_exc()[-700:]
    print(json.dumps({k: v for k, v in r.items() if k != "trace"}, ensure_ascii=False)[:400])
    return r


def list_probe():
    """Why does the list endpoint sometimes answer with an error? Try several date pairs / stop filters,
    shared vs fresh client, with a few spaced attempts each, recording what comes back."""
    out = []
    try:
        from fli.models import (Airport, FlightSearchFilters, FlightSegment, MaxStops, PassengerInfo, SeatType, TripType)
        from fli.search import SearchFlights
        from fli.search._urls import with_locale_params
    except Exception as e:
        return [{"case": "probe import", "error": str(e)}]
    o, d_ = Airport.YUL, Airport.PEK
    shared = SearchFlights()

    def mk(dep, ret, stops):
        segs = [FlightSegment(departure_airport=[[o, 0]], arrival_airport=[[d_, 0]], travel_date=dep)]
        if ret:
            segs.append(FlightSegment(departure_airport=[[d_, 0]], arrival_airport=[[o, 0]], travel_date=ret))
        return FlightSearchFilters(trip_type=TripType.ROUND_TRIP if ret else TripType.ONE_WAY,
                                   passenger_info=PassengerInfo(adults=1), flight_segments=segs,
                                   stops=stops, seat_type=SeatType.ECONOMY)

    cases = [
        ("Mar3/Apr16 <=1", "2027-03-03", "2027-04-16", MaxStops.ONE_STOP_OR_FEWER, False),
        ("Mar3/Apr16 ANY", "2027-03-03", "2027-04-16", MaxStops.ANY, False),
        ("Mar3/Apr16 <=1 FRESH client", "2027-03-03", "2027-04-16", MaxStops.ONE_STOP_OR_FEWER, True),
        ("Mar10/Apr21 <=1", "2027-03-10", "2027-04-21", MaxStops.ONE_STOP_OR_FEWER, False),
        ("Mar3/Apr15 <=1", "2027-03-03", "2027-04-15", MaxStops.ONE_STOP_OR_FEWER, False),
        ("Mar4/Apr17 <=1", "2027-03-04", "2027-04-17", MaxStops.ONE_STOP_OR_FEWER, False),
        ("Mar10/Apr09 30d <=1", "2027-03-10", "2027-04-09", MaxStops.ONE_STOP_OR_FEWER, False),
        ("Dec1/Dec15 <=1 (near)", "2026-12-01", "2026-12-15", MaxStops.ONE_STOP_OR_FEWER, False),
        ("OW Mar3 <=1", "2027-03-03", None, MaxStops.ONE_STOP_OR_FEWER, False),
    ]
    for name, dep, ret, stops, fresh in cases:
        r = {"case": "probe: " + name, "attempts": []}
        sf = SearchFlights() if fresh else shared
        f = mk(dep, ret, stops)
        for i in range(3):
            a = {}
            try:
                t0 = time.time()
                url = with_locale_params(sf.BASE_URL, "CAD", None, None)
                resp = sf.client.post(url=url, data=f"f.req={f.encode()}", impersonate="chrome", allow_redirects=True)
                a["http"] = resp.status_code
                a["len"] = len(resp.text)
                a["errtype"] = "ErrorResponse" in resp.text[:600]
                res = sf._fetch_flights(f, currency="CAD", language=None, country=None, capture_session=False)
                a["n"] = len(res or [])
                if res:
                    a["min"] = min(x.price for x in res if x.price is not None)
                a["s"] = round(time.time() - t0, 1)
            except Exception as e:
                a["err"] = f"{type(e).__name__}: {e}"[:160]
            r["attempts"].append(a)
            if a.get("n"):
                break
            time.sleep(6)
        print(json.dumps(r, ensure_ascii=False)[:300])
        out.append(r)
        time.sleep(3)
    return out


def calendar_probe():
    """Can dense one-way calendars + real round-trip samples rebuild a dense matrix for ANY stay length?
    Compares sum(outbound one-way, return one-way) with the real round-trip calendar (30-day stays)."""
    import statistics as stt
    r = {"case": "calendar probe: one-way sums vs real round-trip", "steps": []}
    try:
        from fli.models import (Airport, DateSearchFilters, FlightSegment, MaxStops, PassengerInfo, SeatType, TripType)
        from fli.search import SearchDates
        pax = PassengerInfo(adults=1)
        Y, P = Airport.YUL, Airport.PEK

        def seg(a, b, date):
            return FlightSegment(departure_airport=[[a, 0]], arrival_airport=[[b, 0]], travel_date=date)

        def cal(a, b, first, last, dur=None):
            segs = [seg(a, b, first)]
            if dur:
                segs.append(seg(b, a, (dt.date.fromisoformat(first) + dt.timedelta(days=dur)).isoformat()))
            f = DateSearchFilters(trip_type=TripType.ROUND_TRIP if dur else TripType.ONE_WAY, passenger_info=pax,
                                  flight_segments=segs, stops=MaxStops.ONE_STOP_OR_FEWER, seat_type=SeatType.ECONOMY,
                                  from_date=first, to_date=last, duration=dur)
            res = SearchDates().search(f, currency="CAD") or []
            return {x.date[0].date().isoformat(): x.price for x in res}

        t0 = time.time()
        out = cal(Y, P, "2027-03-01", "2027-03-31")
        time.sleep(3)
        back = cal(P, Y, "2027-03-25", "2027-05-10")            # <=61 days
        time.sleep(3)
        rt30 = cal(Y, P, "2027-03-01", "2027-03-31", 30)
        r["n_out"], r["n_back"], r["n_rt30"] = len(out), len(back), len(rt30)
        ratios = []
        for d, p in rt30.items():
            ret = (dt.date.fromisoformat(d) + dt.timedelta(days=30)).isoformat()
            if d in out and ret in back:
                ratios.append((out[d] + back[ret]) / p)
        if ratios:
            r["ratio_sum_over_rt30"] = {"n": len(ratios), "median": round(stt.median(ratios), 3),
                                        "min": round(min(ratios), 3), "max": round(max(ratios), 3)}
        r["sample"] = {d: [out.get(d), rt30.get(d)] for d in sorted(rt30)[:5]}
        r["seconds"] = round(time.time() - t0, 1)
        # stay-length threshold for the round-trip calendar
        for dur in (32, 33, 34):
            time.sleep(3)
            r["steps"].append({"rt_dur": dur, "n_dates": len(cal(Y, P, "2027-03-01", "2027-03-31", dur))})
        # throughput: many calendar calls in a row (is this endpoint throttled like the list endpoint?)
        ok = 0
        for i in range(12):
            try:
                x = cal(Y, P, f"2027-0{3 + i % 3}-01", f"2027-0{3 + i % 3}-28", 28 + i % 3)
                ok += 1 if x else 0
            except Exception as e:
                r["steps"].append({"burst_err": str(e)[:80]})
                break
            time.sleep(1.5)
        r["burst_ok_of_12"] = ok
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {e}"
        r["trace"] = traceback.format_exc()[-600:]
    print(json.dumps({k: v for k, v in r.items() if k != "trace"}, ensure_ascii=False)[:600])
    return r


def calendar_compare():
    """How close is the dense 32-day round-trip calendar to the exact 42-day list prices we already collected?"""
    import glob, statistics as stt
    r = {"case": "calendar(32d) vs exact list price (42d)", "rows": []}
    try:
        from fli.models import (Airport, DateSearchFilters, FlightSegment, MaxStops, PassengerInfo, SeatType, TripType)
        from fli.search import SearchDates
        exact = []                                               # (route, dep, price)
        for f in sorted(glob.glob(str(DATA / "matrix" / "YUL-PEK" / "20*.json")))[-1:]:
            m = json.loads(pathlib.Path(f).read_text())
            i = m["offsets"].index(42) if 42 in m["offsets"] else None
            if i is not None:
                exact += [("PEK", k, v[i]) for k, v in m["p"].items() if v[i] is not None]
        for rid, code in (("YUL-PEK", "PEK"), ("YUL-PVG", "PVG")):
            p = DATA / f"{rid}.json"
            if p.exists():
                doc = json.loads(p.read_text())
                exact += [(code, k, min(o["price"] for o in v["offers"])) for k, v in doc["latest"].items() if v.get("offers")]
        seen, cache = set(), {}
        for code, dep, price in exact:
            if (code, dep) in seen:
                continue
            seen.add((code, dep))
            ym = dep[:7]
            if (code, ym) not in cache:
                first = dt.date.fromisoformat(ym + "-01")
                last = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
                first = max(first, dt.date.today() + dt.timedelta(days=1))
                pax = PassengerInfo(adults=1)
                A, B = Airport.YUL, Airport[code]
                f = DateSearchFilters(trip_type=TripType.ROUND_TRIP, passenger_info=pax,
                                      flight_segments=[FlightSegment(departure_airport=[[A, 0]], arrival_airport=[[B, 0]], travel_date=first.isoformat()),
                                                       FlightSegment(departure_airport=[[B, 0]], arrival_airport=[[A, 0]], travel_date=(first + dt.timedelta(days=32)).isoformat())],
                                      stops=MaxStops.ONE_STOP_OR_FEWER, seat_type=SeatType.ECONOMY,
                                      from_date=first.isoformat(), to_date=last.isoformat(), duration=32)
                try:
                    res = SearchDates().search(f, currency="CAD") or []
                except Exception:
                    res = []
                cache[(code, ym)] = {x.date[0].date().isoformat(): x.price for x in res}
                time.sleep(2)
            cal = cache[(code, ym)].get(dep)
            r["rows"].append({"route": code, "dep": dep, "exact42": price, "cal32": cal,
                              "ratio": round(price / cal, 3) if cal else None})
        rat = [x["ratio"] for x in r["rows"] if x["ratio"]]
        if rat:
            r["median_ratio_exact_over_cal"] = round(stt.median(rat), 3)
            r["min"], r["max"] = min(rat), max(rat)
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {e}"
        r["trace"] = traceback.format_exc()[-500:]
    print(json.dumps({k: v for k, v in r.items() if k != "trace"}, ensure_ascii=False)[:700])
    return r


def main():
    import sys
    if "--all" not in sys.argv:         # default: light run (calendar probe only) so the runner's request budget isn't burned
        out = {"when": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "results": [calendar_compare()]}
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
        return
    d = dt.date.today()
    near = (d + dt.timedelta(days=45)).isoformat()
    mar, apr = "2027-03-10", "2027-04-21"
    cases = [
        ("near one-way YUL-PEK (+45d)", [(near, "YUL", "PEK")], "one-way", None),
        ("one-way YUL-PEK Mar10 any stops", [(mar, "YUL", "PEK")], "one-way", None),
        ("one-way YUL-PEK Mar10 <=1 stop", [(mar, "YUL", "PEK")], "one-way", 1),
        ("round-trip YUL-PEK Mar10/Apr21 any", [(mar, "YUL", "PEK"), (apr, "PEK", "YUL")], "round-trip", None),
        ("round-trip YUL-PEK Mar10/Apr21 <=1", [(mar, "YUL", "PEK"), (apr, "PEK", "YUL")], "round-trip", 1),
        ("one-way YUL-PVG Mar10", [(mar, "YUL", "PVG")], "one-way", None),
        ("one-way PEK-YUL Apr21 (return leg)", [(apr, "PEK", "YUL")], "one-way", None),
        ("round-trip natural-language q", "Flights from YUL to PEK on 2027-03-10 returning 2027-04-21", "round-trip", None),
        ("one-way YYZ-LHR (control, +45d)", [(near, "YYZ", "LHR")], "one-way", None),
    ]
    out = {"when": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "results": []}
    for name, legs, trip, stops in cases:
        out["results"].append(run_case(name, legs, trip, stops))
        time.sleep(4)
    out["results"].append(calendar_case())
    out["results"].extend(fli_cases())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
