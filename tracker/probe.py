"""Time-of-day probe: query the SAME fixed cells (stay column, every Nth date) at morning / noon / afternoon,
so price differences between times of day can be analysed. Writes docs/data/tod/<route>.json."""
import sys, json, pathlib, random, datetime as dt

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fetch

ROOT, DATA = fetch.ROOT, fetch.DATA
KEEP_DAYS = 400


def slot_now():
    h = dt.datetime.now(dt.timezone.utc).hour           # ET = UTC-5 (winter): 13Z=8am, 17Z=12pm, 21Z=4pm
    return "am" if h < 15 else ("noon" if h < 19 else "pm")


def probe_cells(route, cfg, every):
    """Deterministic subset of valid (depart, return) pairs: every Nth date of the stay column."""
    mx, stay, td = cfg["matrix"], cfg["stay_days"], dt.date.today()
    cells = []
    for sp in fetch.month_specs(mx):
        first = dt.date.fromisoformat(sp["month"] + "-01")
        d = first.replace(day=sp["from_day"])
        while d.month == first.month:
            ret = d + dt.timedelta(days=stay)
            rb = dt.date.fromisoformat(sp["return_by"]) if sp["return_by"] else None
            if d > td and (not rb or ret <= rb) and ret <= td + dt.timedelta(days=fetch.HORIZON["days"]):
                cells.append((d, ret))
            d += dt.timedelta(days=1)
    cells.sort()
    return cells[::every]


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    tod = cfg.get("tod") or {}
    if not tod.get("routes"):
        print("tod disabled")
        return 0
    cur = cfg.get("currency", "CAD")
    fetch.pace_init(cfg)
    fetch.PRICING["mode"] = cfg.get("pricing", "list")
    fetch.EXPAND["returns"] = False
    fetch.HORIZON["days"] = int(cfg.get("booking_horizon_days", 330))
    slot, today = slot_now(), dt.date.today().isoformat()
    out_dir = DATA / "tod"
    out_dir.mkdir(parents=True, exist_ok=True)
    n_ok = 0
    for route in cfg["routes"]:
        if route["id"] not in tod["routes"]:
            continue
        path = out_dir / f"{route['id']}.json"
        doc = json.loads(path.read_text()) if path.exists() else {"slots": ["am", "noon", "pm"], "obs": {}}
        cells = probe_cells(route, cfg, int(tod.get("every", 4)))
        random.shuffle(cells)
        cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()

        def save(doc=doc, path=path):
            for dep, byday in doc["obs"].items():
                doc["obs"][dep] = {d: v for d, v in byday.items() if d >= cutoff}
            path.write_text(json.dumps(doc, separators=(",", ":")))

        for dep, ret in cells:
            if fetch.expired():
                break
            if doc["obs"].get(dep.isoformat(), {}).get(today, {}).get(slot) is not None:
                continue                                  # this slot already recorded today
            try:
                offers = fetch.search(route, dep, ret, cur, expand=False)
            except Exception as e:
                print("probe fail", dep, str(e)[:80])
                continue
            if offers:
                doc["obs"].setdefault(dep.isoformat(), {}).setdefault(today, {})[slot] = min(o["price"] for o in offers)
                n_ok += 1
            fetch.checkpoint(save)
        save()
        fetch.checkpoint(save, force=True)
    print(f"probe slot={slot} ok={n_ok} requests={fetch.PACE['count']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
