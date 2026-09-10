"""Where everything is, and how long it takes to drive between the two.

The trip page could always say WHAT you were doing and WHEN. It could never say whether
two things half an hour apart on the page were half an hour apart on the ground -- and on
this trip that is the question that actually decides the day, because the safari park is
25 minutes from the rental and the waterfront is nine.

Two free services, no API key between them:
  * Nominatim (OpenStreetMap) turns `location` into coordinates.
  * OSRM turns two coordinates into a real driving distance and duration.

THE RULE THIS MODULE EXISTS TO ENFORCE: an error is not a zero. Every lookup returns one
of three states -- found / the service answered and had nothing / nobody could look --
and only the first is ever cached. A cached timeout is a permanent wrong answer wearing
the costume of a real one, and "0 min away" is the most dangerous thing this page could
render. Callers get `None` for a distance nobody could measure and are expected to say
so on the page, in words.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import os

import db
import family

# Nominatim's usage policy asks for a real identifying UA and at most 1 request/sec.
# A generic urllib UA gets 403d, which would arrive here as "error" for every row and
# look exactly like the geocoder having no idea where the town is. The contact is
# whoever runs this copy: FM_CONTACT_EMAIL, else the household's own mailbox.
_CONTACT = (os.environ.get("FM_CONTACT_EMAIL") or family.SELF_EMAIL or "").strip()
USER_AGENT = ("FamilyManager/1.0 (personal family trip planner"
              + (f"; contact {_CONTACT}" if _CONTACT else "") + ")")
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSRM = "https://router.project-osrm.org/route/v1/driving"
PAUSE_S = 1.1          # Nominatim's published rate limit, honored rather than tested.
TIMEOUT_S = 20

# States a lookup can end in. `nohit` and `error` are DIFFERENT and collapsing them is
# the bug: one means "this address does not exist", the other means "ask again later".
OK, NOHIT, ERROR, NOADDRESS = "ok", "nohit", "error", "noaddress"

# Free text in `location` that is a description, not somewhere a geocoder can find. These
# rows get `noaddress` -- an honest "no address on this item", never a failed lookup.
_VAGUE = re.compile(r"^\s*$")


class LookupError_(Exception):
    """Raised when nobody could look -- network, rate limit, malformed answer."""


def _ssl_context():
    """Verify against certifi's roots rather than the machine's own certificate store.

    An always-on machine can carry an EXPIRED root in the Windows certificate store, and
    CPython on Windows verifies against that store. Every HTTPS routing host then fails
    with "certificate has expired" while the same calls succeed on another machine. Two
    unrelated servers don't expire on the same day, which is what gives it away. And a
    machine whose antivirus re-signs TLS locally can pass for the wrong reason, so the
    one that "works" isn't always the reliable witness.

    certifi ships current roots and is already installed. Falling back to the system
    default keeps this working on a machine without certifi rather than turning a
    missing package into a hard failure.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:                                     # noqa: BLE001
        return None


_SSL = _ssl_context()


def _get(url: str) -> object:
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
        # An HTML error page parses as neither JSON nor "no results". Treating it as
        # "no results" is how a rate limit becomes a permanent blank on the page.
        raise LookupError_(f"non-JSON answer ({body[:80]!r})") from exc


def clean_location(raw: str | None) -> str | None:
    """The part of a free-text `location` a geocoder can actually use.

    `location` is written for a human -- "120 Main St to LKA", "Home to Lakeside
    Airport (LKA), Terminal 1". Handing that to Nominatim whole returns nothing, or
    worse returns something plausible for the wrong half of the sentence.
    """
    if not raw or _VAGUE.match(raw):
        return None
    s = raw.strip()
    # "A to B" is a leg, not a place. The origin is the one the row is about.
    s = re.split(r"\s+to\s+", s)[0].strip()
    # Trailing parenthetical airport codes and terminal notes help a human, not a lookup.
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    s = re.sub(r",?\s*Terminal\s+\d+\s*$", "", s, flags=re.I).strip()
    s = s.rstrip(" ,;-")
    return s or None


def _norm_key(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def coord_key(lat: float, lng: float) -> str:
    """5 decimal places is about a metre -- far finer than any of this needs, and stable
    enough that the same place always produces the same cache key."""
    return f"{lat:.5f},{lng:.5f}"


def haversine_m(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """Straight-line metres. Never presented as a driving distance -- it is the floor
    under one, and on a river road the difference is a bridge."""
    r = 6371008.8
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# --------------------------------------------------------------------------- geocoding

def geocode(con, raw_location: str | None, *, live: bool = True) -> dict:
    """One location -> {status, lat, lng, label, query}.

    Cache is consulted first and written ONLY on a hit. `live=False` makes this a
    cache-only read, which is what the web request path uses: a page render must never
    block on somebody else's server.
    """
    q = clean_location(raw_location)
    if not q:
        return {"status": NOADDRESS, "lat": None, "lng": None, "label": None, "query": None}
    key = _norm_key(q)
    row = con.execute("SELECT lat, lng, label FROM geo_cache WHERE query=?", (key,)).fetchone()
    if row:
        return {"status": OK, "lat": row["lat"], "lng": row["lng"],
                "label": row["label"], "query": q}
    if not live:
        return {"status": ERROR, "lat": None, "lng": None, "label": None, "query": q}

    url = NOMINATIM + "?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 1, "addressdetails": 0})
    try:
        data = _get(url)
    except LookupError_:
        # Nobody could look. NOT a miss, and nothing is written to the cache.
        return {"status": ERROR, "lat": None, "lng": None, "label": None, "query": q}
    if not isinstance(data, list) or not data:
        # The service answered and genuinely had nothing. Also not cached: an address
        # that is wrong today gets fixed and re-asked, and a miss is cheap to repeat.
        return {"status": NOHIT, "lat": None, "lng": None, "label": None, "query": q}
    hit = data[0]
    try:
        lat, lng = float(hit["lat"]), float(hit["lon"])
    except (KeyError, TypeError, ValueError):
        return {"status": ERROR, "lat": None, "lng": None, "label": None, "query": q}
    label = str(hit.get("display_name") or "")[:300]
    con.execute("INSERT OR REPLACE INTO geo_cache(query, lat, lng, label, fetched_at) "
                "VALUES (?,?,?,?,?)", (key, lat, lng, label, db.now()))
    con.commit()
    return {"status": OK, "lat": lat, "lng": lng, "label": label, "query": q}


# ---------------------------------------------------------------------------- routing

def drive(con, a: tuple[float, float], b: tuple[float, float], *,
          live: bool = True) -> dict | None:
    """Driving distance and time between two points, or None if nobody could measure it.

    Returns {meters, seconds, source}. `source` is 'osrm' for a real route and
    'haversine' for the straight-line floor -- the caller MUST show which, because a
    straight line across the gorge is not a drive anybody can take.
    """
    if a == b:
        return {"meters": 0.0, "seconds": 0.0, "source": "same"}
    ka, kb = coord_key(*a), coord_key(*b)
    row = con.execute("SELECT meters, seconds, source FROM drive_cache "
                      "WHERE origin=? AND dest=?", (ka, kb)).fetchone()
    if row:
        return {"meters": row["meters"], "seconds": row["seconds"], "source": row["source"]}
    if not live:
        return None

    url = (f"{OSRM}/{a[1]:.6f},{a[0]:.6f};{b[1]:.6f},{b[0]:.6f}"
           "?overview=false&alternatives=false")
    try:
        data = _get(url)
    except LookupError_:
        return None
    if not isinstance(data, dict) or data.get("code") != "Ok" or not data.get("routes"):
        return None
    r = data["routes"][0]
    try:
        meters, seconds = float(r["distance"]), float(r["duration"])
    except (KeyError, TypeError, ValueError):
        return None
    con.execute("INSERT OR REPLACE INTO drive_cache"
                "(origin, dest, meters, seconds, source, fetched_at) VALUES (?,?,?,?,?,?)",
                (ka, kb, meters, seconds, "osrm", db.now()))
    con.commit()
    return {"meters": meters, "seconds": seconds, "source": "osrm"}


# ------------------------------------------------------------------------- formatting

def fmt_km(meters: float | None) -> str:
    if meters is None:
        return "—"
    km = meters / 1000.0
    return f"{km:.1f} km" if km < 10 else f"{km:.0f} km"


def fmt_mins(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    m = int(round(seconds / 60.0))
    if m < 1:
        return "under a minute"
    if m < 60:
        return f"{m} min"
    h, rem = divmod(m, 60)
    return f"{h} hr" if rem == 0 else f"{h} hr {rem} min"


# ------------------------------------------------------------------------------- batch

def geocode_trip(slug: str, *, live: bool = True, verbose: bool = True) -> dict:
    """Fill lat/lng for every item in one trip. Prints i/N -- this takes about a second
    per uncached row by policy, so a silent minute of nothing would read as a hang."""
    con = db.connect()
    rows = con.execute("SELECT id, title, location, geo_hint, lat, lng, geo_status "
                       "FROM trip_items WHERE trip=? ORDER BY id", (slug,)).fetchall()
    tally = {OK: 0, NOHIT: 0, ERROR: 0, NOADDRESS: 0, "cached": 0}
    n = len(rows)
    for i, r in enumerate(rows, 1):
        if r["lat"] is not None and r["geo_status"] == OK:
            tally["cached"] += 1
            continue
        before = con.execute("SELECT COUNT(*) c FROM geo_cache").fetchone()["c"]
        # The hint wins where there is one: `location` is written to be read by a
        # human at a gate, and that is frequently a terrible search query.
        res = geocode(con, r["geo_hint"] or r["location"], live=live)
        con.execute("UPDATE trip_items SET lat=?, lng=?, geo_status=?, geo_query=?, "
                    "geo_label=?, geo_at=? WHERE id=?",
                    (res["lat"], res["lng"], res["status"], res["query"],
                     res["label"], db.now(), r["id"]))
        con.commit()
        tally[res["status"]] += 1
        if verbose:
            print(f"  [{i}/{n}] {res['status']:9s} {r['title'][:44]!r}"
                  + (f" -> {res['label'][:60]}" if res["label"] else ""))
        after = con.execute("SELECT COUNT(*) c FROM geo_cache").fetchone()["c"]
        if live and after > before:
            time.sleep(PAUSE_S)
    con.close()
    if verbose:
        print(f"geocode {slug}: " + ", ".join(f"{k}={v}" for k, v in tally.items()))
    return tally


def warm_routes(slug: str, *, live: bool = True, verbose: bool = True) -> dict:
    """Pre-fetch every pair of located places in a trip so the page never waits.

    Deliberately every ORDERED pair: driving A->B and B->A differ, and quietly halving
    the work by assuming symmetry would put a wrong number on the page for one direction.
    """
    con = db.connect()
    rows = con.execute("SELECT id, title, lat, lng FROM trip_items WHERE trip=? "
                       "AND lat IS NOT NULL AND geo_status='ok'", (slug,)).fetchall()
    pts = {}
    for r in rows:
        pts[coord_key(r["lat"], r["lng"])] = (r["lat"], r["lng"])
    keys = list(pts)
    todo = [(a, b) for a in keys for b in keys if a != b]
    got = miss = 0
    for i, (a, b) in enumerate(todo, 1):
        res = drive(con, pts[a], pts[b], live=live)
        if res:
            got += 1
        else:
            miss += 1
        if verbose and (i % 10 == 0 or i == len(todo)):
            print(f"  routes [{i}/{len(todo)}] ok={got} unmeasured={miss}")
    con.close()
    if verbose:
        print(f"warm_routes {slug}: {len(pts)} places, {got} legs measured, "
              f"{miss} nobody could measure")
    return {"places": len(pts), "ok": got, "miss": miss}


# -------------------------------------------------------------------------- self-test

def _self_test() -> int:
    """Offline. Every assertion here is about the honesty rules, not about the network:
    the failure this module exists to prevent is a lookup that failed rendering as a
    confident number, and that is exactly what a network-free test can pin down."""
    fails = []

    def check(name, cond):
        print(("  PASS  " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    # clean_location
    check("a leg keeps only its origin",
          clean_location("120 Main St to LKA") == "120 Main St")
    check("trailing airport parenthetical is dropped",
          clean_location("Lakeside Airport (LKA)") == "Lakeside Airport")
    check("terminal suffix is dropped",
          clean_location("Lakeside Airport Terminal 1") == "Lakeside Airport")
    check("a real address survives intact",
          clean_location("2821 Mill Rd, Riverside, ON")
          == "2821 Mill Rd, Riverside, ON")
    check("empty location is no address", clean_location("   ") is None)
    check("None is no address", clean_location(None) is None)

    # haversine sanity: the rental to the waterfront is well under 3 km
    d = haversine_m(43.0896, -79.0849, 43.0790, -79.0784)
    check("haversine gives a sane short distance", 500 < d < 3000)
    check("haversine is zero for one point",
          haversine_m(43.1, -79.1, 43.1, -79.1) == 0.0)

    # formatting never invents a number for a missing one
    check("unmeasured distance renders as a dash", fmt_km(None) == "—")
    check("unmeasured time renders as a dash", fmt_mins(None) == "—")
    check("35 minutes formats as minutes", fmt_mins(35 * 60) == "35 min")
    check("90 minutes formats as hours", fmt_mins(90 * 60) == "1 hr 30 min")
    check("60 minutes has no stray zero", fmt_mins(3600) == "1 hr")
    check("km under ten keeps a decimal", fmt_km(4200) == "4.2 km")

    # the cache must refuse failures
    con = db.connect_memory() if hasattr(db, "connect_memory") else None
    if con is None:
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)
    n0 = con.execute("SELECT COUNT(*) c FROM geo_cache").fetchone()["c"]
    res = geocode(con, "120 Main St, Riverside", live=False)
    n1 = con.execute("SELECT COUNT(*) c FROM geo_cache").fetchone()["c"]
    check("a cache-only miss reports error, never nohit", res["status"] == ERROR)
    check("a failed geocode writes NOTHING to the cache", n0 == n1)
    check("a failed geocode carries no coordinates", res["lat"] is None)

    res2 = geocode(con, "   ", live=False)
    check("a blank location is 'noaddress', not an error", res2["status"] == NOADDRESS)

    d2 = drive(con, (43.09, -79.08), (43.10, -79.09), live=False)
    check("an unmeasured drive is None, never 0", d2 is None)
    d3 = drive(con, (43.09, -79.08), (43.09, -79.08), live=False)
    check("a point to itself is a real zero", d3 is not None and d3["seconds"] == 0.0)

    con.execute("INSERT INTO geo_cache(query,lat,lng,label,fetched_at) VALUES (?,?,?,?,?)",
                ("120 main st, riverside", 43.0896, -79.0849, "Main St", "x"))
    res3 = geocode(con, "120 Main St, Riverside", live=False)
    check("a cached hit is served without the network", res3["status"] == OK
          and abs(res3["lat"] - 43.0896) < 1e-9)

    # Not a network call. It asserts only that we chose a trust bundle we control
    # instead of the machine store, which is the one that can carry an expired root.
    check("the Nominatim UA identifies a contact or at least the app",
          USER_AGENT.startswith("FamilyManager/"))
    try:
        import certifi                                     # noqa: F401
        check("TLS verifies against certifi, not the machine store", _SSL is not None)
    except ImportError:
        check("without certifi the system store is the documented fallback",
              _SSL is None)

    print(("\nSELF-TEST FAILED: " + ", ".join(fails)) if fails else "\nSELF-TEST PASSED")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--trip", help="slug to geocode")
    ap.add_argument("--routes", action="store_true", help="also warm every driving leg")
    ap.add_argument("--offline", action="store_true", help="cache only, no network")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if not a.trip:
        ap.error("--trip or --self-test")
    geocode_trip(a.trip, live=not a.offline)
    if a.routes:
        warm_routes(a.trip, live=not a.offline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
