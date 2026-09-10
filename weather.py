"""What the weather is going to do, tied to what you were planning to do in it.

A forecast on its own is decoration. The question worth answering is "Monday is 61°F and
54% rain and three of the four things on that day are outdoors" -- which needs the
forecast AND a view of which items have a roof. That pairing is the whole point of this
module; `shelter` on trip_items is the other half.

Open-Meteo: free, no API key, no account. Forecast horizon is about 16 days, so a trip
further out than that legitimately has no forecast yet -- which the page has to SAY,
because a blank column and "we don't know yet" look identical and only one is honest.

THREE THINGS THIS ENFORCES

1. A forecast is a MODEL, never a measurement. It carries the time it was issued and
   the page shows that age. A five-day-old forecast rendered as today's is the same
   failure as a stale calendar rendered as current -- it looks perfectly healthy.
2. An error is not "no rain". Every lookup ends as found / no forecast for that date /
   nobody could look, and only the first is ever cached.
3. It is cached with a TTL and REFRESHED, because unlike a geocode a forecast is
   supposed to change. A permanently cached forecast is a wrong answer that gets more
   wrong every hour.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import db

API = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "FamilyManager/1.0 (personal family trip planner)"
TIMEOUT_S = 25
# How long a cached forecast is served before it is re-fetched. Long enough that a page
# render never waits, short enough that "as of 3 hours ago" is still worth reading.
TTL_HOURS = 3
# Open-Meteo publishes roughly 16 days. Past that there is genuinely nothing to say.
HORIZON_DAYS = 16

OK, NOFORECAST, ERROR = "ok", "noforecast", "error"

# WMO weather codes, grouped the way a parent actually needs them: is it wet, is it
# rough, do we need the indoor plan. The exact shade of "slight drizzle" does not
# change any decision on this trip.
WMO = {
    0: ("Clear", "clear"), 1: ("Mainly clear", "clear"), 2: ("Partly cloudy", "cloud"),
    3: ("Overcast", "cloud"),
    45: ("Fog", "cloud"), 48: ("Freezing fog", "cloud"),
    51: ("Light drizzle", "wet"), 53: ("Drizzle", "wet"), 55: ("Heavy drizzle", "wet"),
    56: ("Freezing drizzle", "wet"), 57: ("Freezing drizzle", "wet"),
    61: ("Light rain", "wet"), 63: ("Rain", "wet"), 65: ("Heavy rain", "wet"),
    66: ("Freezing rain", "wet"), 67: ("Freezing rain", "wet"),
    71: ("Light snow", "wet"), 73: ("Snow", "wet"), 75: ("Heavy snow", "wet"),
    77: ("Snow grains", "wet"),
    80: ("Rain showers", "wet"), 81: ("Rain showers", "wet"),
    82: ("Heavy showers", "wet"),
    85: ("Snow showers", "wet"), 86: ("Snow showers", "wet"),
    95: ("Thunderstorm", "storm"), 96: ("Thunderstorm with hail", "storm"),
    99: ("Thunderstorm with hail", "storm"),
}

# Where the line sits between "take a jacket" and "use the indoor plan". Named
# constants rather than magic numbers buried in a template, because these are a
# judgement call somebody may want to argue with.
POP_WET = 50          # % chance of precipitation at which a day counts as WET
POP_IFFY = 25         # ...and at which it is worth having a fallback ready
COLD_F = 65           # a high below this is a jacket day in August


def _ssl_context():
    """certifi, not the machine store -- a machine's own store can carry an expired root.
    See geo._ssl_context."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:                                     # noqa: BLE001
        return None


_SSL = _ssl_context()


class LookupError_(Exception):
    """Nobody could look -- network, rate limit, malformed answer."""


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=_SSL) as r:
            body = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        raise LookupError_(str(exc)) from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise LookupError_(f"non-JSON answer ({body[:80]!r})") from exc


def describe(code) -> tuple[str, str]:
    """(text, bucket). An UNKNOWN code is said so, not silently called clear."""
    if code is None:
        return ("Not forecast", "unknown")
    return WMO.get(int(code), (f"Code {code}", "unknown"))


def _now():
    return datetime.now(timezone.utc)


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    # db.now() writes a NAIVE, LOCAL timestamp. Reading it as UTC made a forecast
    # fetched seconds ago render as "4 hr ago" -- the exact size of this machine's UTC
    # offset, which is the tell. A staleness figure that is wrong by a fixed offset is
    # worse than none: it makes fresh data look stale and stale data look ancient.
    if t.tzinfo is None:
        return (datetime.now() - t).total_seconds() / 3600.0
    return (_now() - t).total_seconds() / 3600.0


def fmt_age(iso: str | None) -> str:
    h = _age_hours(iso)
    if h is None:
        return "unknown age"
    if h < 1:
        return f"{int(h * 60)} min ago"
    if h < 48:
        return f"{int(h)} hr ago"
    return f"{int(h / 24)} days ago"


def fetch(lat: float, lng: float, start: str, end: str, tz="America/New_York") -> dict:
    """Daily + hourly forecast for a date range. Raises LookupError_ if nobody could look."""
    q = {
        "latitude": f"{lat:.4f}", "longitude": f"{lng:.4f}",
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                  "precipitation_probability_max,wind_speed_10m_max,sunrise,sunset"),
        "hourly": "precipitation_probability,temperature_2m,weather_code",
        "timezone": tz,
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "wind_speed_unit": "kmh",
        "start_date": start, "end_date": end,
    }
    return _get(API + "?" + urllib.parse.urlencode(q))


def refresh_trip(slug: str, *, force: bool = False, verbose: bool = True) -> dict:
    """Pull the forecast for a trip and store it. Only a GOOD answer is ever written.

    Beyond the forecast horizon Open-Meteo returns an error for the range; that is
    'noforecast', not a failure, and it must not blow away a forecast already stored.
    """
    con = db.connect()
    trip = con.execute("SELECT * FROM trips WHERE slug=?", (slug,)).fetchone()
    if not trip:
        con.close()
        raise SystemExit(f"no trip {slug!r}")
    base = con.execute("SELECT lat, lng, title FROM trip_items WHERE trip=? AND is_base=1 "
                       "AND lat IS NOT NULL", (slug,)).fetchone()
    if not base:
        con.close()
        if verbose:
            print("no home base with coordinates -- nothing to forecast against")
        return {"state": ERROR, "detail": "no base"}

    fresh = con.execute("SELECT fetched_at FROM weather_daily WHERE trip=? "
                        "ORDER BY fetched_at DESC LIMIT 1", (slug,)).fetchone()
    if not force and fresh:
        age = _age_hours(fresh["fetched_at"])
        if age is not None and age < TTL_HOURS:
            con.close()
            if verbose:
                print(f"forecast is {fmt_age(fresh['fetched_at'])}, inside the "
                      f"{TTL_HOURS}h TTL -- not re-fetching")
            return {"state": OK, "detail": "cached", "age_hours": age}

    start, end = trip["start_date"], trip["end_date"]
    today = datetime.now().date()
    if datetime.strptime(start, "%Y-%m-%d").date() - today > timedelta(days=HORIZON_DAYS):
        con.close()
        if verbose:
            print(f"{start} is beyond the ~{HORIZON_DAYS}-day forecast horizon -- "
                  "there is genuinely nothing to fetch yet")
        return {"state": NOFORECAST, "detail": "beyond horizon"}
    # Never ask for a past date: the forecast endpoint has no history and the whole
    # range errors, which would look like an outage.
    if datetime.strptime(start, "%Y-%m-%d").date() < today:
        start = today.isoformat()
    if datetime.strptime(end, "%Y-%m-%d").date() < today:
        con.close()
        if verbose:
            print("the trip is over -- no forecast to fetch")
        return {"state": NOFORECAST, "detail": "trip is in the past"}

    try:
        d = fetch(base["lat"], base["lng"], start, end)
    except LookupError_ as exc:
        con.close()
        if verbose:
            print(f"could not look: {exc}")
        return {"state": ERROR, "detail": str(exc)[:200]}

    daily, hourly = d.get("daily") or {}, d.get("hourly") or {}
    if not daily.get("time"):
        con.close()
        if verbose:
            print("the service answered with no days -- nothing stored")
        return {"state": NOFORECAST, "detail": "empty daily"}

    now = db.now()
    n_d = n_h = 0
    for i, day in enumerate(daily["time"]):
        con.execute(
            "INSERT OR REPLACE INTO weather_daily (trip, day, code, temp_max, temp_min,"
            " precip_in, pop_max, wind_kmh, sunrise, sunset, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (slug, day, _at(daily, "weather_code", i), _at(daily, "temperature_2m_max", i),
             _at(daily, "temperature_2m_min", i), _at(daily, "precipitation_sum", i),
             _at(daily, "precipitation_probability_max", i),
             _at(daily, "wind_speed_10m_max", i), _at(daily, "sunrise", i),
             _at(daily, "sunset", i), now))
        n_d += 1
    for i, stamp in enumerate(hourly.get("time", [])):
        day, _, hhmm = stamp.partition("T")
        con.execute(
            "INSERT OR REPLACE INTO weather_hourly (trip, day, hour, pop, temp_f, code,"
            " fetched_at) VALUES (?,?,?,?,?,?,?)",
            (slug, day, hhmm[:5], _at(hourly, "precipitation_probability", i),
             _at(hourly, "temperature_2m", i), _at(hourly, "weather_code", i), now))
        n_h += 1
    con.commit()
    con.close()
    if verbose:
        print(f"forecast for {slug}: {n_d} days, {n_h} hourly points, issued {now}")
    return {"state": OK, "days": n_d, "hours": n_h}


def _at(block, key, i):
    v = block.get(key)
    if not isinstance(v, list) or i >= len(v):
        return None
    return v[i]


def day_forecast(con, slug: str, day: str) -> dict | None:
    r = con.execute("SELECT * FROM weather_daily WHERE trip=? AND day=?",
                    (slug, day)).fetchone()
    if not r:
        return None
    text, bucket = describe(r["code"])
    pop = r["pop_max"]
    return {
        # NOT "pop". Jinja resolves `d.wx.pop` to dict.pop -- the METHOD -- and renders
        # "<built-in method pop of dict object at 0x...>" straight onto the page, with
        # no error anywhere. Exactly the trap `items` set on the checklist page.
        "day": day, "text": text, "bucket": bucket,
        "temp_max": r["temp_max"], "temp_min": r["temp_min"],
        "precip_in": r["precip_in"], "rain_pct": pop, "wind_kmh": r["wind_kmh"],
        "sunrise": r["sunrise"], "sunset": r["sunset"],
        "fetched_at": r["fetched_at"], "age": fmt_age(r["fetched_at"]),
        # The judgement, made once here rather than re-derived in a template.
        "wet": pop is not None and pop >= POP_WET,
        "iffy": pop is not None and POP_IFFY <= pop < POP_WET,
        "cold": r["temp_max"] is not None and r["temp_max"] < COLD_F,
    }


def hour_pop(con, slug: str, day: str, hhmm: str | None) -> dict | None:
    """Chance of rain at the hour an item actually happens.

    This is the number that changes a decision: the trip's Monday is 54% for the DAY,
    but the only outdoor thing on it is at 8pm, and the day figure cannot tell you
    whether 8pm is the wet part.
    """
    if not hhmm:
        return None
    hh = hhmm[:2] + ":00"
    r = con.execute("SELECT pop, temp_f, code FROM weather_hourly WHERE trip=? AND day=? "
                    "AND hour=?", (slug, day, hh)).fetchone()
    if not r:
        return None
    text, bucket = describe(r["code"])
    # Same reason as above: never a key called "pop" on a dict a template touches.
    return {"rain_pct": r["pop"], "temp_f": r["temp_f"], "text": text, "bucket": bucket,
            "hour": hh}


def _self_test() -> int:
    """Offline. About the honesty rules and the thresholds, not the network."""
    fails = []

    def check(name, cond):
        print(("  PASS  " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    check("a known code describes itself", describe(53)[0] == "Drizzle")
    check("drizzle counts as wet", describe(53)[1] == "wet")
    check("a thunderstorm is its own bucket", describe(95)[1] == "storm")
    check("clear is clear", describe(0)[1] == "clear")
    # The one that matters: an ABSENT code must not read as good weather.
    check("a missing code is 'Not forecast', never clear", describe(None)[0] == "Not forecast")
    check("a missing code is not the clear bucket", describe(None)[1] != "clear")
    check("an unknown code says its number rather than guessing",
          describe(4242)[0] == "Code 4242" and describe(4242)[1] == "unknown")

    check("age of nothing is unknown, not zero", fmt_age(None) == "unknown age")
    # The regression that shipped once: db.now() is naive LOCAL, and reading it as UTC
    # put this machine's whole offset onto every staleness figure.
    check("a naive local stamp reads as just now, not one UTC offset ago",
          "min ago" in fmt_age(datetime.now().isoformat(timespec="seconds")))
    check("a naive local stamp two hours back reads as 2 hr",
          fmt_age((datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")) == "2 hr ago")
    now = datetime.now(timezone.utc).isoformat()
    check("a just-fetched forecast reads in minutes", "min ago" in fmt_age(now))
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    check("a 30-hour-old forecast reads in hours", fmt_age(old) == "30 hr ago")
    older = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    check("a four-day-old forecast reads in days", fmt_age(older) == "4 days ago")

    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    check("a day with no stored forecast is None, not a sunny day",
          day_forecast(con, "t", "2026-08-24") is None)
    check("an item with no clock has no hourly answer",
          hour_pop(con, "t", "2026-08-24", None) is None)
    check("an hour with nothing stored is None",
          hour_pop(con, "t", "2026-08-24", "09:30") is None)

    con.execute("INSERT INTO weather_daily (trip, day, code, temp_max, temp_min, "
                "precip_in, pop_max, wind_kmh, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("t", "2026-08-24", 53, 61.0, 53.6, 0.078, 54, 21.3, now))
    f = day_forecast(con, "t", "2026-08-24")
    check("54% reads as a wet day", f["wet"] is True)
    check("the chance of rain is a value, not dict.pop", f["rain_pct"] == 54)
    check("a 61F high reads as cold for August", f["cold"] is True)
    check("a wet day is not also flagged iffy", f["iffy"] is False)
    con.execute("INSERT INTO weather_daily (trip, day, code, temp_max, pop_max, "
                "fetched_at) VALUES (?,?,?,?,?,?)", ("t", "2026-08-27", 80, 82.1, 28, now))
    g = day_forecast(con, "t", "2026-08-27")
    check("28% reads as iffy, not wet", g["iffy"] is True and g["wet"] is False)
    check("an 82F high is not cold", g["cold"] is False)
    # A day whose pop is NULL must be neither wet nor dry -- it is unknown.
    con.execute("INSERT INTO weather_daily (trip, day, code, fetched_at) "
                "VALUES (?,?,?,?)", ("t", "2026-08-28", None, now))
    h = day_forecast(con, "t", "2026-08-28")
    check("an unknown chance of rain is not treated as dry",
          h["wet"] is False and h["iffy"] is False and h["rain_pct"] is None)

    print(("\nSELF-TEST FAILED: " + ", ".join(fails)) if fails else "\nSELF-TEST PASSED")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--trip", help="slug to forecast")
    ap.add_argument("--upcoming", action="store_true",
                    help="every trip that has not ended yet (the nightly job's mode -- a "
                         "hardcoded slug forecast last summer's trip forever)")
    ap.add_argument("--force", action="store_true", help="ignore the TTL")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.upcoming:
        import db
        from datetime import date as _date
        con = db.connect()
        slugs = [r["slug"] for r in con.execute(
            "SELECT slug FROM trips WHERE end_date >= ? ORDER BY start_date", (_date.today().isoformat(),))]
        con.close()
        if not slugs:
            print("no upcoming trip -- nothing to forecast")
            return 0
        worst = 0
        for slug in slugs:
            res = refresh_trip(slug, force=a.force)
            print(slug, res["state"])
            worst = max(worst, 2 if res["state"] == ERROR else 0)
        return worst
    if not a.trip:
        ap.error("--trip, --upcoming or --self-test")
    res = refresh_trip(a.trip, force=a.force)
    return 0 if res["state"] == OK else (2 if res["state"] == ERROR else 0)


if __name__ == "__main__":
    sys.exit(main())
