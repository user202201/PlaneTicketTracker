"""Send email when a tracked route's current minimum price is low.
Env: SMTP_USER, SMTP_PASS (Gmail app password), ALERT_TO (optional override)."""
import json, os, smtplib, ssl, pathlib, statistics
from email.message import EmailMessage

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"


def route_stats(doc):
    """Cheapest today vs. average of all recorded daily minimums across dates."""
    today_min, past = None, []
    for dep, by_day in doc.get("history", {}).items():
        days = sorted(by_day)
        if not days:
            continue
        p = by_day[days[-1]]
        today_min = p if today_min is None else min(today_min, p)
        past += [by_day[d] for d in days[:-1]]
    return today_min, (statistics.mean(past) if len(past) >= 5 else None)


def send(subject, body, to):
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    if not (user and pw and to):
        print("[alert] SMTP not configured; would send:\n", subject, "\n", body)
        return
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = subject, user, to
    m.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pw)
        s.send_message(m)


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    a = cfg.get("alert", {})
    to = os.environ.get("ALERT_TO") or a.get("email_to")
    lines = []
    for r in cfg["routes"]:
        p = DATA / f"{r['id']}.json"
        if not p.exists():
            continue
        doc = json.loads(p.read_text())
        cur, avg = route_stats(doc)
        if cur is None:
            continue
        hit = []
        if a.get("below_price") and cur <= a["below_price"]:
            hit.append(f"低于目标价 {a['below_price']}")
        if avg and a.get("pct_below_avg") and cur <= avg * (1 - a["pct_below_avg"] / 100):
            hit.append(f"比历史均价 {avg:.0f} 低 {100 - cur / avg * 100:.0f}%")
        if hit:
            best = min(doc["latest"].items(),
                       key=lambda kv: min(o["price"] for o in kv[1]["offers"]))
            lines.append(f"{r['origin']} → {r['dest']}: 当前最低 {cur} {doc['currency']}"
                         f"（出发 {best[0]}）— " + "，".join(hit))
    status = json.loads((DATA / "status.json").read_text()) if (DATA / "status.json").exists() else {}
    if status.get("ok", 1) == 0 and status.get("failed", 0) > 0:
        send("[机票追踪] 今日抓取全部失败", "\n".join(status.get("errors", [])), to)
    if lines:
        send("[机票追踪] 降价提醒", "\n".join(lines), to)


if __name__ == "__main__":
    main()
