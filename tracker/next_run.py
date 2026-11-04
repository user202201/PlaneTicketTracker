"""Relay decision: print the delay (minutes) before the next batch run, or nothing if no run is needed.
Continue while work remains today and the daily cap is not hit. A run that got NO data (e.g. this runner's IP
was throttled by Google) is retried on a fresh runner after a longer wait, at most 4 times in a row."""
import json, pathlib, random, datetime as dt

p = pathlib.Path(__file__).resolve().parent.parent / "docs" / "data" / "progress.json"
MAX_RUNS_PER_DAY = 30
MAX_ZERO_STREAK = 4
try:
    g = json.loads(p.read_text())
except Exception:
    raise SystemExit(0)
today = dt.datetime.now(dt.timezone.utc).date().isoformat()
if g.get("date") != today or g.get("pending", 0) <= 0:
    raise SystemExit(0)                       # nothing left today -> no more runs until tomorrow
if g.get("runs_today", 0) >= MAX_RUNS_PER_DAY:
    raise SystemExit(0)
if g.get("ok", 0) <= 0:
    if g.get("zero_streak", 0) >= MAX_ZERO_STREAK:
        raise SystemExit(0)                   # give up for now; the 3-hourly watchdog tries again later
    print(random.randint(25, 40))             # blocked runner: new runner (new IP) after a longer pause
    raise SystemExit(0)
print(random.randint(30, 45) if g.get("stop") == "throttled" else random.randint(4, 12))
