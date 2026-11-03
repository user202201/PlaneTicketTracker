"""Rule-based summary of collected matrix snapshots -> docs/data/summary.json.
Pure statistics (no AI/API cost). Refuses to give advice until enough days exist."""
import json, pathlib, statistics as st, datetime as dt

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
MIN_DAYS_ADVICE = 14     # buy/wait verdict
MIN_DAYS_WEEKDAY = 28    # which weekday to purchase


def wd(s):
    return dt.date.fromisoformat(s).weekday()


def add(d, n):
    return (dt.date.fromisoformat(d) + dt.timedelta(days=n)).isoformat()


def load(route_id):
    mdir = DATA / "matrix" / route_id
    if not (mdir / "index.json").exists():
        return None
    snaps = sorted(json.loads((mdir / "index.json").read_text())["snapshots"])
    data, offs, cur = {}, None, "CAD"
    for s in snaps:
        f = json.loads((mdir / f"{s}.json").read_text())
        data[s], offs, cur = f["p"], f["offsets"], f.get("currency", cur)
    return data, offs, cur


def row_min(row):
    v = [x for x in row if x is not None]
    return min(v) if v else None


def month_avgs(p):
    by = {}
    for dep, row in p.items():
        m = row_min(row)
        if m is not None:
            by.setdefault(dep[:7], []).append(m)
    return {k: st.mean(v) for k, v in by.items()}


def summarize(route_id):
    loaded = load(route_id)
    if not loaded:
        return None
    data, offs, cur = loaded
    days = sorted(data)
    last = days[-1]
    p = data[last]
    out = []
    n = len(days)
    out.append(f"已收集 {n} 天的数据（最新：{last}）。" + ("" if n >= MIN_DAYS_ADVICE else
               f"至少积累 {MIN_DAYS_ADVICE} 天后才会给出买入/观望建议，目前以下仅为当前价格的描述。"))

    # 1. cheapest months now
    ma = month_avgs(p)
    if ma:
        rank = sorted(ma.items(), key=lambda kv: kv[1])
        out.append("各月当前平均最低价（低→高）：" + "，".join(f"{m[5:].lstrip('0')}月 {v:.0f}" for m, v in rank) + f" {cur}")

    # 2. cheapest cells
    cells = [(v, dep, add(dep, offs[i])) for dep, row in p.items() for i, v in enumerate(row) if v is not None]
    if cells:
        top = sorted(cells)[:3]
        out.append("当前最便宜的组合：" + "；".join(f"{d[5:]} 出发 / {r[5:]} 返程 {v} {cur}" for v, d, r in top))

    # 3. depart weekday & stay length (relative, so month level doesn't distort)
    dw = {i: [] for i in range(7)}
    for dep, row in p.items():
        m = row_min(row)
        mon = ma.get(dep[:7])
        if m is not None and mon:
            dw[wd(dep)].append(m / mon)
    good = {i: st.mean(v) for i, v in dw.items() if len(v) >= 4}
    if len(good) >= 5:
        b = min(good, key=good.get)
        w = max(good, key=good.get)
        out.append(f"出发星期：{WD[b]}出发平均比当月均价低 {(1 - good[b]) * 100:.1f}%，{WD[w]}出发最贵（高 {(good[w] - 1) * 100:.1f}%）。")
    so = {o: [] for o in offs}
    for row in p.values():
        m = row_min(row)
        if m:
            for i, v in enumerate(row):
                if v is not None:
                    so[offs[i]].append(v / m)
    sa = {o: st.mean(v) for o, v in so.items() if v}
    if sa:
        b = min(sa, key=sa.get)
        out.append(f"停留天数：在 {offs[0]}–{offs[-1]} 天中，停留 {b} 天平均最便宜（比同一出发日的最低价高 {(sa[b] - 1) * 100:.1f}%）。")

    # 4. trend vs ~7 days ago
    trend = {}
    ref = next((d for d in reversed(days[:-1]) if (dt.date.fromisoformat(last) - dt.date.fromisoformat(d)).days >= 7), None)
    if ref:
        old = month_avgs(data[ref])
        for m, v in ma.items():
            if m in old and old[m]:
                trend[m] = (v / old[m] - 1) * 100
        if trend:
            out.append(f"近一周变化（对比 {ref}）：" + "，".join(f"{m[5:].lstrip('0')}月 {t:+.1f}%" for m, t in sorted(trend.items())))

    # 5. verdict per month
    if n >= MIN_DAYS_ADVICE:
        hist = {}
        for d in days:
            for m, v in month_avgs(data[d]).items():
                hist.setdefault(m, []).append(v)
        for m, v in sorted(ma.items(), key=lambda kv: kv[1]):
            lo, mean = min(hist[m]), st.mean(hist[m])
            gap = (v / lo - 1) * 100
            t = trend.get(m)
            if gap <= 3:
                s = "接近已观察到的最低价，价格合适可考虑入手"
            elif t is not None and t > 3:
                s = f"高于已观察低点 {gap:.0f}% 且一周涨 {t:.0f}%，上涨中，不想再涨可尽早决定"
            elif v < mean:
                s = f"低于观察期均价，高于最低点 {gap:.0f}%，可继续观察"
            else:
                s = f"高于观察期均价，高于最低点 {gap:.0f}%，建议观望"
            out.append(f"{m[5:].lstrip('0')}月：{s}。")

    # 6. best weekday to buy (detrended across daily snapshots)
    if n >= MIN_DAYS_WEEKDAY:
        g = {i: [] for i in range(7)}
        have = set(days)
        for dep, row in p.items():
            for i in range(len(offs)):
                s = {d: data[d].get(dep, [None] * len(offs))[i] for d in days}
                for d, v in s.items():
                    nb = [s.get(add(d, k)) if add(d, k) in have else None for k in (-3, -2, -1, 0, 1, 2, 3)]
                    if v is not None and all(x is not None for x in nb):
                        g[wd(d)].append(v / (sum(nb) / 7))
        res = {i: st.mean(v) for i, v in g.items() if len(v) >= 30}
        if len(res) == 7:
            b, w = min(res, key=res.get), max(res, key=res.get)
            out.append(f"买票星期：{WD[b]}查到的价格平均比前后一周均价低 {(1 - res[b]) * 100:.1f}%，{WD[w]}最高（高 {(res[w] - 1) * 100:.1f}%）。差异在 1–2% 以内时没有实际意义。")
    else:
        out.append(f"“星期几买票最划算”需要至少 {MIN_DAYS_WEEKDAY} 天数据（目前 {n} 天）。")
    return {"days": n, "latest": last, "bullets": out}


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    res = {"updated": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z", "routes": {}}
    for r in cfg["routes"]:
        s = summarize(r["id"])
        if s:
            res["routes"][r["id"]] = s
    (DATA / "summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print("summary for", list(res["routes"]))


if __name__ == "__main__":
    main()
