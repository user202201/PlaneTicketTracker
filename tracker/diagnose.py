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


def main():
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
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
