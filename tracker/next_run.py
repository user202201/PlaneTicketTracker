"""Relay decision: print the delay (minutes) before the next batch run, or nothing if no run is needed.
Continue only while work remains today, the last run made progress, and the daily cap is not hit."""
import json, pathlib, random, datetime as dt

p = pathlib.Path(__file__).resolve().parent.parent / "docs" / "data" / "progress.json"
MAX_RUNS_PER_DAY = 30
try:
    g = json.loads(p.read_text())
except Exception:
    raise SystemExit(0)
today = dt.datetime.now(dt.timezone.utc).date().isoformat()
if g.get("date") != today or g.get("pending", 0) <= 0:
    raise SystemExit(0)                       # nothing left today -> no more runs until tomorrow
if g.get("runs_today", 0) >= MAX_RUNS_PER_DAY or g.get("ok", 0) <= 0:
    raise SystemExit(0)                       # no progress / cap reached -> the 3-hourly watchdog retries later
print(random.randint(30, 45) if g.get("stop") == "throttled" else random.randint(4, 12))
