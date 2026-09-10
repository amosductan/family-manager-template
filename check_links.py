"""Every link on the trip page resolves, or it does not ship.

A dead link on a page somebody opens at a ticket booth is worse than no link: it costs
the same tap, spends the trust, and returns nothing. This walks every `url` on a trip
and reports one of three states -- reachable / the server said no / nobody could look --
because a network failure here must never be read as "the link is broken", which is the
same error geo.py exists to avoid.

    python check_links.py                    # every URL on the live trip
    python check_links.py --self-test        # offline, no network

Exit 0 when every link resolved, 1 when any link is genuinely dead, 2 when at least one
could not be checked at all (so a flaky connection cannot report a clean bill of health).
"""

from __future__ import annotations

import argparse
import ssl
import sys
import urllib.error
import urllib.request

import db

TIMEOUT_S = 20
# A default urllib UA gets 403d by several of these operators, which would arrive as
# "dead" for a link that works perfectly in a browser.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

OK, DEAD, UNKNOWN = "ok", "dead", "unknown"


def _ctx():
    """certifi over the machine store -- a store carrying an expired root would report
    every https link dead. See geo._ssl_context."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:                                     # noqa: BLE001
        return None


_SSL = _ctx()


def check_url(url: str) -> tuple[str, str]:
    """(state, note). A 4xx/5xx is DEAD; a timeout or DNS failure is UNKNOWN."""
    if not url or not url.strip():
        return UNKNOWN, "no url"
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=_SSL) as r:
            return OK, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        # 403 and 405 are the site refusing THIS request, not a missing page. Treated as
        # unknown rather than dead: calling a working page dead would send somebody to
        # fix a link that needs no fixing.
        if e.code in (401, 403, 405, 429):
            return UNKNOWN, f"HTTP {e.code} (blocked, not necessarily broken)"
        return DEAD, f"HTTP {e.code}"
    except Exception as e:                                # noqa: BLE001
        return UNKNOWN, f"{type(e).__name__}: {str(e)[:60]}"


def _next_trip() -> str | None:
    """The trip that hasn't ended yet, soonest first -- what "the live trip" means."""
    import db
    con = db.connect()
    row = con.execute("SELECT slug FROM trips WHERE COALESCE(end_date, start_date, '') >= date('now') "
                      "ORDER BY start_date LIMIT 1").fetchone()
    return row["slug"] if row else None


def run(trip: str | None = None) -> int:
    trip = trip or _next_trip()
    if not trip:
        print("No upcoming trip to check. Pass --trip <slug>.")
        return 2
    con = db.connect()
    rows = con.execute("SELECT title, url, plan, kind FROM trip_items WHERE trip=? "
                       "AND url IS NOT NULL AND TRIM(url) <> '' ORDER BY title",
                       (trip,)).fetchall()
    con.close()
    dead, unknown = [], []
    print(f"{len(rows)} links on {trip}\n")
    for i, r in enumerate(rows, 1):
        state, note = check_url(r["url"])
        mark = {OK: "OK   ", DEAD: "DEAD ", UNKNOWN: "?    "}[state]
        print(f"  [{i}/{len(rows)}] {mark} {r['title'][:44]:<44} {note}")
        if state == DEAD:
            dead.append((r["title"], r["url"], note))
        elif state == UNKNOWN:
            unknown.append((r["title"], r["url"], note))
    print()
    if dead:
        print(f"DEAD ({len(dead)}):")
        for t, u, n in dead:
            print(f"  {t}\n    {u}  -> {n}")
    if unknown:
        print(f"COULD NOT CHECK ({len(unknown)}) -- NOT the same as broken:")
        for t, u, n in unknown:
            print(f"  {t}  -> {n}")
    if dead:
        return 1
    if unknown:
        return 2
    print("ALL LINKS RESOLVE")
    return 0


def _self_test() -> int:
    fails = []

    def check(name, cond):
        print(("  PASS  " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    check("an empty url is unknown, never dead", check_url("")[0] == UNKNOWN)
    check("a None url is unknown", check_url(None)[0] == UNKNOWN)
    # A host that cannot resolve is "nobody could look", NOT "this link is broken".
    st, _ = check_url("https://this-host-does-not-exist-fm-selftest.invalid/")
    check("an unresolvable host is unknown, not dead", st == UNKNOWN)
    check("certifi is what we verify against", _SSL is not None)
    print(("\nSELF-TEST FAILED: " + ", ".join(fails)) if fails else "\nSELF-TEST PASSED")
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--trip", default=None, help="trip slug (default: the next trip that has not ended)")
    a = ap.parse_args()
    sys.exit(_self_test() if a.self_test else run(a.trip))
