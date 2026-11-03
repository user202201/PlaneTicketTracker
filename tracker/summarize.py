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


LANGS = ("zh", "en", "fr")
MON = {"zh": [f"{i}月" for i in range(1, 13)],
       "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
       "fr": ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]}
WDN = {"zh": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"],
       "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
       "fr": ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]}
CAT = {
 "zh": {
  "collected": "已收集 {n} 天的数据（最新：{last}）。",
  "need": "至少积累 {min} 天后才会给出买入/观望建议，目前以下仅为当前价格的描述。",
  "months": "各月当前平均最低价（低→高）：{list} {cur}",
  "cheap": "当前最便宜的组合：{list}", "cheap_i": "{d} 出发 / {r} 返程 {v} {cur}",
  "dwd": "出发星期：{b}出发平均比当月均价低 {lo}%，{w}出发最贵（高 {hi}%）。",
  "stay": "停留天数：在 {a}–{b} 天中，停留 {s} 天平均最便宜（比同一出发日的最低价高 {p}%）。",
  "wow": "近一周变化（对比 {ref}）：{list}",
  "near": "{m}：接近已观察到的最低价，价格合适可考虑入手。",
  "rise": "{m}：高于已观察低点 {gap}% 且一周涨 {t}%，上涨中，不想再涨可尽早决定。",
  "below": "{m}：低于观察期均价，高于最低点 {gap}%，可继续观察。",
  "wait": "{m}：高于观察期均价，高于最低点 {gap}%，建议观望。",
  "buywd": "买票星期：{b}查到的价格平均比前后一周均价低 {lo}%，{w}最高（高 {hi}%）。差异在 1–2% 以内时没有实际意义。",
  "buyneed": "“星期几买票最划算”需要至少 {min} 天数据（目前 {n} 天）。"},
 "en": {
  "collected": "Collected {n} days of data (latest: {last}).",
  "need": "Buy/wait advice appears after {min} days of data; for now this only describes current prices.",
  "months": "Current average lowest price by month (low→high): {list} {cur}",
  "cheap": "Cheapest combinations now: {list}", "cheap_i": "depart {d} / return {r} {v} {cur}",
  "dwd": "Departure weekday: leaving on {b} is on average {lo}% below the monthly average; {w} is the most expensive ({hi}% above).",
  "stay": "Trip length: within {a}–{b} days, {s} days is cheapest on average ({p}% above the cheapest for the same departure date).",
  "wow": "Change over the past week (vs {ref}): {list}",
  "near": "{m}: close to the lowest price seen so far — a reasonable time to buy.",
  "rise": "{m}: {gap}% above the lowest seen and up {t}% this week — rising; decide soon if you don't want to pay more.",
  "below": "{m}: below the average over the observed period, {gap}% above the lowest — keep watching.",
  "wait": "{m}: above the observed average, {gap}% above the lowest — suggest waiting.",
  "buywd": "Best day to buy: prices seen on {b} are on average {lo}% below the surrounding week; {w} is the highest ({hi}% above). Differences within 1–2% are not meaningful.",
  "buyneed": "“Best weekday to buy” needs at least {min} days of data (currently {n})."},
 "fr": {
  "collected": "{n} jours de données collectés (dernier : {last}).",
  "need": "Les conseils d'achat/attente apparaissent après {min} jours de données ; pour l'instant, simple description des prix actuels.",
  "months": "Prix minimum moyen actuel par mois (bas→haut) : {list} {cur}",
  "cheap": "Combinaisons les moins chères : {list}", "cheap_i": "départ {d} / retour {r} {v} {cur}",
  "dwd": "Jour de départ : partir le {b} coûte en moyenne {lo} % de moins que la moyenne du mois ; le {w} est le plus cher ({hi} % au-dessus).",
  "stay": "Durée du séjour : entre {a} et {b} jours, {s} jours est le moins cher en moyenne ({p} % au-dessus du minimum pour le même départ).",
  "wow": "Évolution sur la semaine (vs {ref}) : {list}",
  "near": "{m} : proche du prix le plus bas observé — bon moment pour acheter.",
  "rise": "{m} : {gap} % au-dessus du minimum observé et +{t} % cette semaine — en hausse ; décidez vite pour éviter de payer plus.",
  "below": "{m} : sous la moyenne observée, {gap} % au-dessus du minimum — continuez à surveiller.",
  "wait": "{m} : au-dessus de la moyenne observée, {gap} % au-dessus du minimum — mieux vaut attendre.",
  "buywd": "Meilleur jour pour acheter : les prix du {b} sont en moyenne {lo} % sous la semaine environnante ; le {w} est le plus élevé ({hi} % au-dessus). Un écart de 1 à 2 % n'est pas significatif.",
  "buyneed": "« Meilleur jour pour acheter » nécessite au moins {min} jours de données (actuellement {n})."},
}


def summarize(route_id):
    loaded = load(route_id)
    if not loaded:
        return None
    data, offs, cur = loaded
    days = sorted(data)
    last = days[-1]
    p = data[last]
    n = len(days)
    out = {lg: [] for lg in LANGS}

    def say(cls, key, fn=lambda lg: {}):
        """Append one bullet in every language; fn(lang) supplies language-specific fields."""
        for lg in LANGS:
            out[lg].append({"c": cls, "t": CAT[lg][key].format(**fn(lg))})

    def mname(m, lg):
        return MON[lg][int(m[5:7]) - 1]

    say("info", "collected", lambda lg: {"n": n, "last": last})
    if n < MIN_DAYS_ADVICE:
        say("info", "need", lambda lg: {"min": MIN_DAYS_ADVICE})

    ma = month_avgs(p)
    if ma:
        rank = sorted(ma.items(), key=lambda kv: kv[1])
        say("info", "months", lambda lg: {"list": ", ".join(f"{mname(m, lg)} {v:.0f}" for m, v in rank), "cur": cur})

    cells = [(v, dep, add(dep, offs[i])) for dep, row in p.items() for i, v in enumerate(row) if v is not None]
    if cells:
        top = sorted(cells)[:3]
        say("good", "cheap", lambda lg: {"list": "; ".join(
            CAT[lg]["cheap_i"].format(d=d[5:], r=r[5:], v=v, cur=cur) for v, d, r in top)})

    dw = {i: [] for i in range(7)}
    for dep, row in p.items():
        m = row_min(row)
        mon = ma.get(dep[:7])
        if m is not None and mon:
            dw[wd(dep)].append(m / mon)
    good = {i: st.mean(v) for i, v in dw.items() if len(v) >= 4}
    if len(good) >= 5:
        b, w = min(good, key=good.get), max(good, key=good.get)
        say("info", "dwd", lambda lg: {"b": WDN[lg][b], "w": WDN[lg][w],
                                       "lo": f"{(1 - good[b]) * 100:.1f}", "hi": f"{(good[w] - 1) * 100:.1f}"})
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
        say("info", "stay", lambda lg: {"a": offs[0], "b": offs[-1], "s": b, "p": f"{(sa[b] - 1) * 100:.1f}"})

    trend = {}
    ref = next((d for d in reversed(days[:-1]) if (dt.date.fromisoformat(last) - dt.date.fromisoformat(d)).days >= 7), None)
    if ref:
        old = month_avgs(data[ref])
        for m, v in ma.items():
            if m in old and old[m]:
                trend[m] = (v / old[m] - 1) * 100
        if trend:
            say("info", "wow", lambda lg: {"ref": ref, "list": ", ".join(
                f"{mname(m, lg)} {t:+.1f}%" for m, t in sorted(trend.items()))})

    if n >= MIN_DAYS_ADVICE:
        hist = {}
        for d in days:
            for m, v in month_avgs(data[d]).items():
                hist.setdefault(m, []).append(v)
        for m, v in sorted(ma.items(), key=lambda kv: kv[1]):
            lo, mean = min(hist[m]), st.mean(hist[m])
            gap = (v / lo - 1) * 100
            t = trend.get(m)
            kw = lambda lg, m=m, gap=gap, t=t: {"m": mname(m, lg), "gap": f"{gap:.0f}", "t": f"{t:.0f}" if t is not None else ""}
            if gap <= 3:
                say("good", "near", kw)
            elif t is not None and t > 3:
                say("wait", "rise", kw)
            elif v < mean:
                say("good", "below", kw)
            else:
                say("wait", "wait", kw)

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
            say("info", "buywd", lambda lg: {"b": WDN[lg][b], "w": WDN[lg][w],
                                             "lo": f"{(1 - res[b]) * 100:.1f}", "hi": f"{(res[w] - 1) * 100:.1f}"})
    else:
        say("info", "buyneed", lambda lg: {"min": MIN_DAYS_WEEKDAY, "n": n})
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
