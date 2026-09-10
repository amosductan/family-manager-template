"""Google Calendar ingest for Family Manager — read-only by default, rule-driven RSVP.

Why: the app read email only, so every date the family had already agreed on was invisible
to it. A kid's first day (2026-09-02) sat on the calendar from March and the checklist still
showed the supply list undated. 

What it does:
  1. Pulls events from every calendar on the account for a window.
  2. **Dedupes ACROSS calendars**, which the `events` table's UNIQUE(kid, title, event_date)
     cannot do — an auto-populated class calendar ("CC") writes the same day under three
     different titles ("First Day of Little Oaks" / "First Day of Little Oaks Preschool" /
     "First Day of Little Oaks 2026"). Dedupe is on (date, normalized title), keeping the
     entry with the richest description so the operational detail survives.
  3. Attributes a kid from the text (family.KID_PATTERNS: name, aliases, school), defaulting
     to Both rather than guessing.
  4. Upserts into `events` with source='gcal', source_ref='<calendar_id>:<event_id>', so a
     re-run updates in place.
  5. `--rsvp` answers invitations by rule. Opt-in, never on by default.

Deliberately NOT done: writing events to Google. v1 scope was "dashboard only, no calendar
writes" and reading does not break that. An RSVP is a response, not a new event.

Auth: OAuth installed-app flow, once, in a browser. See docs/SETUP_CALENDAR.md. The refresh
token lands in DATA_DIR/gcal_token.json and is gitignored with the rest of data/.
Uses google-auth + google-auth-oauthlib + requests — no googleapiclient, one less dep in
the container.

    python gcal.py --authorize            # one time, opens a browser
    python gcal.py --dry-run              # show what would be ingested
    python gcal.py                        # ingest into the events table
    python gcal.py --rsvp --dry-run       # show which invitations the rules would answer
    python gcal.py --rsvp                 # answer them
    python gcal.py --self-test            # offline, no network, no credentials
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import days_off
import db
import family

ROOT = Path(__file__).resolve().parent

# Calendar names carry punctuation the Windows console can't encode — a subscribed school
# "Days Off 25‑26" calendar used a non-breaking hyphen (U+2011), and printing it raised
# UnicodeEncodeError under cp1252, killing the run AFTER the events had been fetched. A
# scheduled task inherits that console, so this isn't cosmetic.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # not a real console (piped, captured)
        pass

DATA_DIR = Path(os.environ.get("FM_DATA_DIR", str(ROOT / "data")))
TOKEN_PATH = DATA_DIR / "gcal_token.json"
CLIENT_SECRET_PATH = Path(
    os.environ.get("FM_GCAL_CLIENT_SECRET", str(DATA_DIR / "gcal_client_secret.json")))

# calendar.events covers reading AND responding to invitations; calendar.readonly is what
# lets us enumerate the calendar list. Nothing here can create or delete an event.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]
API = "https://www.googleapis.com/calendar/v3"

def _local_tz(name: str):
    """The family's wall clock. Every comparison of two calendars' times happens here."""
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return ZoneInfo(os.environ.get("FM_TZ", name))
    except Exception:  # tzdata missing on a bare Windows Python
        return timezone(timedelta(hours=-4))


# Words that carry no meaning for matching one event against another.
_STOP = r"\b(the|a|an|of|for|at|to|and|school|day|20\d\d)\b"
# Abbreviations that hold everywhere; each kid's school names are added in bind_household().
_GENERIC_ABBREV = [
    (r"\bappt\b", "appointment"),   # "Nail Appointment" vs "Nail appt Ava", same day
]


def _school_abbrevs() -> list[tuple[str, str]]:
    """A school's full name and its short name collapse to one token, so "First Day of
    Little Oaks Preschool" and "First Day of Little Oaks" key the same. Full name first:
    once it's replaced, the short-name pattern has nothing left to match."""
    out = []
    for k in family.CONFIG.get("kids") or []:
        full = (k.get("school") or "").strip().lower()
        short = (k.get("school_short") or "").strip().lower()
        token = re.sub(r"[^a-z0-9]", "", short or full)
        for name in (full, short):
            if name and token:
                pat = r"\b" + re.escape(name).replace(r"\ ", r"\s+") + r"(?:\s+school)?\b"
                out.append((pat, token))
    return out


def _kid_alternation() -> str:
    """Every word that ties text to any kid (names, aliases, schools), as one alternation."""
    return "|".join(pat for _, pat in family.KID_PATTERNS)


def bind_household() -> None:
    """(Re)derive everything household-dependent from family. Runs at import; the
    self-test calls it again after family.use_example(). Env vars still win, so a machine
    can point at a different mailbox without editing the household file."""
    g = globals()
    g["LOCAL_TZ"] = _local_tz(family.TIMEZONE)
    # Who the family's own address is — needed to find "my" attendee record.
    g["SELF_EMAIL"] = os.environ.get("FM_SELF_EMAIL", family.SELF_EMAIL)
    # Whose invitations may be auto-accepted. A spouse's family logistics are not really
    # invitations; declining one is never the right automatic answer.
    env = os.environ.get("FM_RSVP_TRUSTED")
    g["TRUSTED_ORGANIZERS"] = ([e.strip().lower() for e in env.split(",") if e.strip()]
                               if env else list(family.TRUSTED_ORGANIZERS))
    g["KID_PATTERNS"] = list(family.KID_PATTERNS)
    g["_ABBREV"] = _school_abbrevs() + _GENERIC_ABBREV
    g["CATEGORY_PATTERNS"] = _category_patterns()
    g["_OURS_TITLE"] = days_off.self_title_regex()


def normalise_title(title: str) -> str:
    """Collapse the variants an auto-generator produces into one key.

    "First Day of Little Oaks", "First Day of Little Oaks Preschool" and
    "First Day of Little Oaks 2026" must all land on the same string, or the dedupe is
    decorative.
    """
    s = (title or "").lower()
    for pat, rep in _ABBREV:
        s = re.sub(pat, rep, s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(_STOP, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def kid_for(text: str) -> str:
    """Attribute an event to a child. Both, never a guess, when it's ambiguous: no kid
    named, or two or more named ("Ava and Leo 6 month dentist")."""
    hay = (text or "").lower()
    hits = [kid for kid, pat in KID_PATTERNS if re.search(pat, hay)]
    if len(hits) == 1:
        return hits[0]
    return "Both"


def event_date(ev: dict) -> str | None:
    start = ev.get("start") or {}
    raw = start.get("date") or start.get("dateTime")
    return raw[:10] if raw else None


def classify_type(title: str) -> str:
    t = (title or "").lower()
    if re.search(r"\bclosed\b|closure|no school", t):
        return "closure"
    if re.search(r"early dismissal|half day", t):
        return "early_dismissal"
    if re.search(r"meet the teacher|conference|back to school night|meet and greet", t):
        return "meeting"
    if re.search(r"first day|last day", t):
        return "event"
    if re.search(r"camp\b", t):
        return "camp"
    if re.search(r"due\b|payment|deadline|inspection", t):
        return "deadline"
    return "event"


# Categories exist so "comprehensive" does not mean "an undifferentiated wall of 543 rows".
# Everything is stored; the dashboard groups and collapses on this. Order matters — the
# first pattern that matches wins, and kids come first because "Leo swim payment" is a
# kid's thing before it is a money thing.
def _category_patterns() -> list[tuple[str, str]]:
    kids = _kid_alternation()
    return [
        # NOTE on "closure": a bare closed/closure is deliberately NOT here. It matched
        # "Statement closure date is the 20th" in a credit-card reminder's notes and put it
        # on both kids' pages. A real school closure names the school or the camp, so the
        # school tokens below already catch it — the bare word only ever added false
        # positives. The kid words (names, aliases, schools) come from family.KID_PATTERNS.
        ("kids_school", (kids + "|" if kids else "") +
                        r"school|camp\b|rec center|gymnastics|swim|playdate|teacher|"
                        r"conference|pta|ymca|art class|art studio|babysit|daycare|"
                        r"pediatric|dentist|back to school|first day|last day|early dismissal"),
        ("money", r"pay day|payday|payment|balance|loan|mortgage|statement|tuition|"
                  r"deposit|invoice|\bbill\b|\bdue\b|renew|premium|\$\d"),
        ("work", r"in the office|standup|stand-up|1:1|kickoff|"
                 r"all hands|\bqbr\b|sprint|retro|discovery call|client\b"),
        ("travel_home", r"flight|airport|hotel|trip\b|vacation|tree service|tick control|"
                        r"inspection|plumber|electrician|delivery|repair|cleaning|landscap|"
                        r"reservation|\bstay at\b|check ?in to|funeral|wedding|party\b|birthday"),
        ("errand", r"weigh in|ww visits|nail|massage|haircut|mani|pedi|gym\b|therapy|"
                   r"doctor|appointment|hang at"),
    ]


bind_household()


def classify_category(text: str) -> str:
    hay = (text or "").lower()
    for cat, pat in CATEGORY_PATTERNS:
        if re.search(pat, hay):
            return cat
    return "other"


def end_time_of(ev: dict) -> str | None:
    dt = (ev.get("end") or {}).get("dateTime")
    return start_time({"start": {"dateTime": dt}}) if dt else None


def richness(ev: dict) -> int:
    """How much operational detail an entry carries. The CC calendar's copies hold the
    'Remember to bring' / 'Cost' / 'Parking' notes, so when two copies collide the
    fuller one has to win or the dedupe throws away the reason to read it."""
    return len((ev.get("description") or "")) + len((ev.get("location") or ""))


def _tokens(title: str) -> frozenset[str]:
    return frozenset(normalise_title(title).split())


def start_time(ev: dict) -> str | None:
    """Local HH:MM for a timed event, None for all-day.

    Must convert, not slice. The CC calendar writes UTC ("...T14:00:00Z") while the primary
    writes local ("...T10:00:00-04:00") — the SAME instant. Comparing the raw strings
    reported 60 time conflicts that did not exist and would have buried the handful that do.
    """
    dt = (ev.get("start") or {}).get("dateTime")
    if not dt:
        return None
    try:
        parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except ValueError:
        return dt[11:16]
    if parsed.tzinfo is None:
        return parsed.strftime("%H:%M")
    return parsed.astimezone(LOCAL_TZ).strftime("%H:%M")


# Two copies of one happening disagree by minutes, not by half a day. "Ava Haircut" at
# 09:00 and "Haircut" at 21:00 pass the subset test but are two different appointments;
# the swim lesson's 10:00-vs-09:30 is one lesson written twice.
MERGE_WINDOW_HOURS = float(os.environ.get("FM_MERGE_WINDOW_HOURS", "3"))


def _near_in_time(a: dict, b: dict) -> bool:
    """All-day entries carry no clock, so they never block a merge."""
    ta, tb = start_time(a), start_time(b)
    if not ta or not tb:
        return True
    mins = [int(t[:2]) * 60 + int(t[3:]) for t in (ta, tb)]
    return abs(mins[0] - mins[1]) <= MERGE_WINDOW_HOURS * 60


def load_time_preferences() -> dict[str, str]:
    """title_key -> calendar name whose clock wins, as decided by a parent in the UI."""
    con = db.connect()
    rows = con.execute("SELECT title_key, prefer_calendar FROM time_preferences").fetchall()
    con.close()
    return {r["title_key"]: r["prefer_calendar"] for r in rows}


def _apply_time_preference(absorber: dict, other: dict, prefer: str) -> dict | None:
    """The preferred calendar's clock on the richer copy's detail, as a NEW dict.

    The two are separable and both matter: CC holds the instructor, address and cost for
    Leo's swim lesson, while the primary holds the time a parent says is correct. Copying the
    start/end across rather than switching which copy survives keeps both.

    Returns a copy rather than editing in place — `raw` is handed to rsvp_plan() after
    dedupe(), and an in-place edit here rewrote the caller's events. The self-test caught
    it: a second dedupe() over the same fixtures saw times that the first run had changed.
    """
    winner = next((e for e in (absorber, other)
                   if (e.get("_calendar_name") or "") == prefer), None)
    if winner is None:
        return None                      # neither copy is from the preferred calendar
    if winner is absorber:
        return absorber                  # already showing the preferred clock
    return {**absorber, "start": winner.get("start"), "end": winner.get("end")}


def dedupe(events: list[dict], conflicts: list | None = None,
           preferences: dict[str, str] | None = None) -> list[dict]:
    """One entry per real-world happening, keeping the richest copy.

    Two passes, because an exact key is not enough. Pass 1 collapses identical
    normalized titles. Pass 2 collapses same-day entries where one title's word set is
    a SUBSET of another's — which is how the duplicates actually presented in a real account:
    "Leo Swim Lessons" (CC) vs "Leo Swim Lessons - private lessons" (primary) is one
    weekly lesson written twice, and an exact key kept both for 109 rows. Likewise
    "Last Day of Camp 2026" vs "Little Oaks Last Day of Summer Camp".

    A collapse whose copies disagree on start time is appended to `conflicts` rather than
    silently resolved — the events table stores a date only, so the disagreement would
    otherwise vanish. This bit a swim lesson
    (CC 10:00am vs primary 9:30am).
    """
    best: dict[tuple, dict] = {}
    for ev in events:
        d = event_date(ev)
        if not d:
            continue
        key = (d, normalise_title(ev.get("summary")))
        if not key[1]:
            continue
        cur = best.get(key)
        if cur is None or richness(ev) > richness(cur):
            best[key] = ev

    by_date: dict[str, list[dict]] = {}
    for (d, _), ev in best.items():
        by_date.setdefault(d, []).append(ev)

    out: list[dict] = []
    for d, evs in by_date.items():
        # Richest first, so a subset always merges INTO the copy carrying the detail.
        evs = sorted(evs, key=richness, reverse=True)
        kept: list[dict] = []
        for ev in evs:
            t = _tokens(ev.get("summary"))
            absorber = next(
                (k for k in kept
                 if (t <= _tokens(k.get("summary")) or _tokens(k.get("summary")) <= t)
                 and _near_in_time(k, ev)),
                None)
            if absorber is None:
                kept.append(ev)
                continue
            ta, tb = start_time(absorber), start_time(ev)
            if ta and tb and ta != tb:
                # A parent already ruled on this series — apply it and stop re-asking.
                pref = (preferences or {}).get(normalise_title(absorber.get("summary"))) \
                    or (preferences or {}).get(normalise_title(ev.get("summary")))
                settled = _apply_time_preference(absorber, ev, pref) if pref else None
                if settled is not None:
                    kept[kept.index(absorber)] = settled
                    continue
            if conflicts is not None:
                if ta and tb and ta != tb:
                    conflicts.append({
                        # ref ties the conflict to the row that survives, so it can be
                        # stored on that row instead of printed and lost.
                        "ref": f"{absorber.get('_calendar_id')}:{absorber.get('id')}",
                        "date": d, "kept": absorber.get("summary"), "kept_time": ta,
                        "kept_cal": absorber.get("_calendar_name"),
                        "dropped": ev.get("summary"), "dropped_time": tb,
                        "dropped_cal": ev.get("_calendar_name")})
        out.extend(kept)
    return sorted(out, key=lambda e: (event_date(e) or "", e.get("summary") or ""))


def my_response(ev: dict) -> str | None:
    for a in ev.get("attendees") or []:
        if a.get("self") or (a.get("email", "").lower() == SELF_EMAIL.lower()):
            return a.get("responseStatus")
    return None


def organizer_of(ev: dict) -> str:
    return ((ev.get("organizer") or {}).get("email") or "").lower()


def _iso_range(ev: dict) -> tuple[str, str] | None:
    """Timed events only — an all-day event blocks nothing and can never conflict."""
    s = (ev.get("start") or {}).get("dateTime")
    e = (ev.get("end") or {}).get("dateTime")
    return (s, e) if s and e else None


def overlaps(a: dict, b: dict) -> bool:
    ra, rb = _iso_range(a), _iso_range(b)
    if not ra or not rb:
        return False
    return ra[0] < rb[1] and rb[0] < ra[1]


def rsvp_plan(events: list[dict]) -> list[dict]:
    """Decide, per pending invitation, whether the rules can answer it.

    The rules only ever ACCEPT. Declining on someone's behalf is a statement about their
    intent that no rule should make, so a conflict is surfaced for a human rather than
    resolved automatically.
    """
    accepted = [e for e in events if my_response(e) == "accepted"]
    plan = []
    for ev in events:
        if my_response(ev) != "needsAction":
            continue
        org = organizer_of(ev)
        row = {"event": ev, "summary": ev.get("summary"), "date": event_date(ev),
               "organizer": org, "action": None, "why": ""}
        if org not in TRUSTED_ORGANIZERS:
            row["action"], row["why"] = "skip", f"organizer {org or '(none)'} is not on the trusted list"
        else:
            clash = next((a for a in accepted if overlaps(ev, a)), None)
            if clash:
                row["action"] = "flag"
                row["why"] = f"overlaps something already accepted: {clash.get('summary')}"
            else:
                row["action"], row["why"] = "accept", "from a trusted organizer, no conflict"
        plan.append(row)
    return plan


# ---------------------------------------------------------------- network


def _creds():
    from google.auth.transport.requests import Request  # noqa: PLC0415
    from google.oauth2.credentials import Credentials  # noqa: PLC0415

    if not TOKEN_PATH.exists():
        raise SystemExit(
            f"No token at {TOKEN_PATH}. Run `python gcal.py --authorize` first "
            "(see docs/SETUP_CALENDAR.md)."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise SystemExit("Stored token is unusable — re-run --authorize.")
    return creds


def authorize(port: int = 0, open_browser: bool = True) -> int:
    """Get a fresh refresh token.

    `--authorize-port N --no-browser` is the HEADLESS path, and it exists because the
    machine that needs the token is often an always-on machine with no browser and no one
    sitting at it. A Testing-mode OAuth client gets its refresh token expired by Google
    every 7 days -- this is not an occasional chore, it is a standing one, and it once went
    unnoticed for six nights because the nightly job kept exiting 0 while the calendar
    step threw.

    Headless recipe, from a laptop that HAS a browser (SERVER = the always-on machine):
        ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 SERVER   # tunnel the redirect back
        ssh SERVER "... python gcal.py --authorize --authorize-port 8765 --no-browser"
        # open the printed URL in the laptop's browser, signed in as the test user,
        # approve, and the redirect to 127.0.0.1:8765 comes down the tunnel to SERVER.
    A fixed port is required for the tunnel; port=0 picks a random one and there would be
    nothing to forward. Loopback redirects on any port are allowed for a Desktop client.
    Keep it on 127.0.0.1 at both ends: the listener never needs to face the network.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415

    if not CLIENT_SECRET_PATH.exists():
        print(f"Missing OAuth client secret at {CLIENT_SECRET_PATH}.")
        print("Create one (Desktop app) and download the JSON — steps in docs/SETUP_CALENDAR.md.")
        return 2
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    if open_browser:
        creds = flow.run_local_server(port=port, prompt="consent")
    else:
        # Printed, flushed, and clearly delimited: this is read out of an SSH pipe by
        # somebody who then has to paste it into a browser somewhere else.
        print("OPEN THIS URL IN A BROWSER SIGNED IN AS THE TEST USER:", flush=True)
        creds = flow.run_local_server(
            port=port, prompt="consent", open_browser=False,
            authorization_prompt_message="{url}",
            success_message="Authorized. You can close this tab.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    print(f"Authorized. Refresh token stored at {TOKEN_PATH} (gitignored).")
    return 0


def token_health() -> dict:
    """Can we actually reach the calendar right now? Three states, never two.

    'ok' we made a real call, 'dead' the token is expired or revoked and only a human
    with a browser can fix it, 'unknown' something else went wrong. A dead token is not
    an empty calendar, and the nightly job treating them alike is what let six days of
    silence look like six days of nothing happening.
    """
    try:
        sess = session()
        r = sess.get(f"{API}/users/me/calendarList", params={"maxResults": 1}, timeout=20)
        if r.status_code == 200:
            return {"state": "ok", "detail": f"HTTP {r.status_code}"}
        return {"state": "unknown", "detail": f"HTTP {r.status_code} {r.text[:120]}"}
    except Exception as exc:                                  # noqa: BLE001
        msg = str(exc)
        if "invalid_grant" in msg or "expired or revoked" in msg:
            return {"state": "dead",
                    "detail": "refresh token expired or revoked -- re-run --authorize"}
        return {"state": "unknown", "detail": msg[:200]}


# Events this app puts ON the calendar must never come back IN as school events, or the
# app feeds itself: a created "Sam off — Leo (Little Oaks closed)" was re-ingested, typed
# as a closure because the title says closed, and — being longer than "Little Oaks
# Closed" — became the REASON shown for the day. The day was real; the explanation had
# become circular. _OURS_TITLE is built in bind_household() from the coverage vocabulary
# (days_off.self_title_regex): TBD, each parent, Both, the extra options, Sitter, Pickup.


def is_ours(ev: dict) -> bool:
    """Did this app create this event?

    The extended property is exact and covers everything created from the board. The title
    shapes are the fallback for the twelve events created by hand on 2026-08-14, before
    the marker existed.
    """
    priv = ((ev.get("extendedProperties") or {}).get("private") or {})
    if priv.get("fm_app") == "family_manager":
        return True
    return bool(_OURS_TITLE.match((ev.get("summary") or "").strip()))


def _get(session, url, **params):
    r = session.get(url, params=params, timeout=45)
    r.raise_for_status()
    return r.json()


def fetch(days_back: int, days_ahead: int) -> list[dict]:
    import requests  # noqa: PLC0415
    from google.auth.transport.requests import AuthorizedSession  # noqa: PLC0415

    session = AuthorizedSession(_creds())
    tmin = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    tmax = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).isoformat()

    cals = _get(session, f"{API}/users/me/calendarList").get("items", [])
    print(f"{len(cals)} calendar(s) on the account")

    out = []
    for cal in cals:
        cid = cal["id"]
        page = None
        n = 0
        while True:
            params = {"timeMin": tmin, "timeMax": tmax, "singleEvents": "true",
                      "orderBy": "startTime", "maxResults": 250}
            if page:
                params["pageToken"] = page
            try:
                data = _get(session, f"{API}/calendars/{requests.utils.quote(cid, safe='')}/events",
                            **params)
            except Exception as exc:
                print(f"  WARN {cal.get('summary')}: {exc}")
                break
            for ev in data.get("items", []):
                if ev.get("status") == "cancelled":
                    continue
                if is_ours(ev):
                    continue
                ev["_calendar_id"] = cid
                ev["_calendar_name"] = cal.get("summary")
                out.append(ev)
                n += 1
            page = data.get("nextPageToken")
            if not page:
                break
        print(f"  {n:4d}  {cal.get('summary')}")
    return out


def reconcile_window(days_back: int, days_ahead: int) -> tuple[str, str]:
    """The date range in which "not returned by Google" is safe to read as "deleted".

    Strictly INSIDE the fetch window, by a day at each end. fetch() asks Google for
    now-30d..now+365d — timestamps — while the events table holds dates. On the boundary
    DATE, anything earlier in the clock-day than the current time was never requested, so
    it comes back missing and looks deleted. That is how a perfectly healthy 8am "Weigh in"
    on the 30-days-ago date got stamped "no longer on the calendar upstream" — and it would
    have happened to a different event every single day the job ran.
    """
    return ((date.today() - timedelta(days=days_back - 1)).isoformat(),
            (date.today() + timedelta(days=days_ahead - 1)).isoformat())


def upsert(events: list[dict], dry: bool, conflicts: list | None = None,
           window: tuple[str, str] | None = None) -> dict:
    """Write the deduped events, then reconcile what upstream no longer has.

    Google gives no tombstone for a deleted event — it simply stops being returned. Without
    the reconcile pass a cancelled gymnastics class stays on the dashboard forever, and a
    ghost event is worse than a missing one because it gets trusted. So every row seen this
    run is stamped, and any gcal row inside the fetched window that went unstamped is marked
    status='cancelled' rather than deleted — a thing that vanished is itself information.
    """
    con = db.connect()
    stamp = db.now()
    by_ref: dict[str, dict] = {}
    for c in (conflicts or []):
        if c.get("ref"):
            by_ref[c["ref"]] = c

    added = updated = skipped = 0
    seen_refs: list[str] = []
    for ev in events:
        d = event_date(ev)
        title = (ev.get("summary") or "").strip()
        if not d or not title:
            skipped += 1
            continue
        kid = kid_for(f"{title} {ev.get('description','')} {ev.get('location','')}")
        ref = f"{ev.get('_calendar_id')}:{ev.get('id')}"
        seen_refs.append(ref)
        details = "\n".join(x for x in [ev.get("location"), ev.get("description")] if x)
        cat = classify_category(f"{title} {ev.get('description','')} {ev.get('location','')}")
        st, et = start_time(ev), end_time_of(ev)
        conf = by_ref.get(ref)
        conf_json = json.dumps(conf) if conf else None

        row = con.execute("SELECT id FROM events WHERE source='gcal' AND source_ref=?",
                          (ref,)).fetchone()
        if row:
            if not dry:
                try:
                    con.execute(
                        "UPDATE events SET kid=?, type=?, title=?, event_date=?, details=?, "
                        "start_time=?, end_time=?, category=?, conflict=?, last_seen_at=?, "
                        "status=CASE WHEN status='cancelled' THEN 'active' ELSE status END "
                        "WHERE id=?",
                        (kid, classify_type(title), title, d, details, st, et, cat,
                         conf_json, stamp, row["id"]))
                    updated += 1
                except sqlite3.IntegrityError:
                    # Upstream re-titled/rescheduled this event onto a (kid, title, date) that
                    # another row already holds — the UNIQUE index refuses the collision. This
                    # gcal row is now a duplicate of that other one, so retire it (mark seen so
                    # the reconcile pass doesn't also touch it) rather than crash the whole run.
                    con.execute(
                        "UPDATE events SET status='cancelled', last_seen_at=? WHERE id=?",
                        (stamp, row["id"]))
                    skipped += 1
            else:
                updated += 1
        else:
            if dry:
                added += 1
                continue
            cur = con.execute(
                "INSERT OR IGNORE INTO events (kid, school, type, title, event_date, "
                "details, start_time, end_time, category, conflict, source, source_ref, "
                "status, last_seen_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, 'gcal', ?, 'active', ?, ?)",
                (kid, ev.get("_calendar_name"), classify_type(title), title, d, details,
                 st, et, cat, conf_json, ref, stamp, stamp))
            if cur.rowcount:
                added += 1
            else:
                # UNIQUE(kid, title, event_date) already held this day from another
                # source. Counting it as added would overstate the run.
                skipped += 1

    cancelled = 0
    if window and not dry:
        rows = con.execute(
            "SELECT id, title, event_date FROM events WHERE source='gcal' "
            "AND status != 'cancelled' AND event_date BETWEEN ? AND ? "
            "AND COALESCE(last_seen_at,'') != ?", (window[0], window[1], stamp)).fetchall()
        for r in rows:
            con.execute("UPDATE events SET status='cancelled' WHERE id=?", (r["id"],))
            print(f"  GONE upstream, marked cancelled: {r['event_date']}  {r['title']}")
        cancelled = len(rows)

    if not dry:
        con.commit()
    con.close()
    return {"added": added, "updated": updated, "skipped": skipped, "cancelled": cancelled}


def do_rsvp(plan: list[dict], dry: bool) -> int:
    import requests  # noqa: PLC0415
    from google.auth.transport.requests import AuthorizedSession  # noqa: PLC0415

    todo = [p for p in plan if p["action"] == "accept"]
    if not todo:
        return 0
    if dry:
        return len(todo)
    session = AuthorizedSession(_creds())
    done = 0
    for p in todo:
        ev = p["event"]
        cid = requests.utils.quote(ev["_calendar_id"], safe="")
        eid = requests.utils.quote(ev["id"], safe="")
        attendees = []
        for a in ev.get("attendees") or []:
            a = dict(a)
            if a.get("self") or a.get("email", "").lower() == SELF_EMAIL.lower():
                a["responseStatus"] = "accepted"
            attendees.append(a)
        # sendUpdates=none: the calendar status is what the other parent reads. A mail
        # per acceptance is noise, and a batch of them is worse.
        r = session.patch(f"{API}/calendars/{cid}/events/{eid}",
                          params={"sendUpdates": "none"},
                          json={"attendees": attendees}, timeout=45)
        if r.ok:
            done += 1
        else:
            print(f"  FAILED {p['summary']}: {r.status_code} {r.text[:160]}")
    return done


def session():
    """An authorized REST session. The token already carries calendar.events, which is
    read AND write — no second OAuth flow is needed to create anything."""
    from google.auth.transport.requests import AuthorizedSession  # noqa: PLC0415

    return AuthorizedSession(_creds())


def find_by_key(sess, key: str) -> dict | None:
    """Look up an event this app created, by its own key.

    The stored event id is the fast path; this is the backstop for when the DB and the
    calendar disagree (a row restored from backup, an event created on another machine).
    Without it, "create" would silently mean "create a second copy".
    """
    data = _get(sess, f"{API}/calendars/primary/events",
                privateExtendedProperty=f"fm_key={key}", showDeleted="false", maxResults=5)
    for ev in data.get("items", []):
        if ev.get("status") != "cancelled":
            return ev
    return None


def create_event(sess, *, summary: str, description: str, start: str, end: str,
                 attendees: list[str], key: str, all_day: bool = True,
                 start_time_: str | None = None, end_time_: str | None = None,
                 notify: bool = True, tz: str | None = None) -> dict:
    """Create one event and invite the attendees.

    `end` is INCLUSIVE here — every date in this codebase is. Google's all-day end is
    exclusive, so the +1 happens once, at this boundary, and nowhere else. `tz` defaults
    to the household's timezone.
    """
    from datetime import date as _date  # noqa: PLC0415

    tz = tz or family.TIMEZONE
    if all_day:
        stop = (_date.fromisoformat(end) + timedelta(days=1)).isoformat()
        when = {"start": {"date": start}, "end": {"date": stop}}
    else:
        when = {"start": {"dateTime": f"{start}T{start_time_}:00", "timeZone": tz},
                "end": {"dateTime": f"{end}T{end_time_}:00", "timeZone": tz}}

    body = {
        "summary": summary,
        "description": description,
        "attendees": [{"email": e} for e in attendees],
        # The key travels WITH the event, so idempotency survives losing the local DB.
        "extendedProperties": {"private": {"fm_key": key, "fm_app": "family_manager"}},
        **when,
    }
    r = sess.post(f"{API}/calendars/primary/events",
                  params={"sendUpdates": "all" if notify else "none",
                          "conferenceDataVersion": 0},
                  json=body, timeout=45)
    if not r.ok:
        raise RuntimeError(f"{r.status_code} {r.text[:300]}")
    return r.json()


def delete_event(sess, event_id: str, notify: bool = True) -> bool:
    eid = _quote(event_id)
    r = sess.delete(f"{API}/calendars/primary/events/{eid}",
                    params={"sendUpdates": "all" if notify else "none"}, timeout=45)
    return r.ok or r.status_code in (404, 410)


def _quote(s: str) -> str:
    import requests  # noqa: PLC0415

    return requests.utils.quote(s, safe="")


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    fails = []

    def check(name, cond):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            fails.append(name)

    print("gcal self-test (offline)")

    # Every fixture is the example family (Sam + Jordan; Ava at Maple Street Elementary,
    # Leo at Little Oaks Preschool), whatever the user's own household says.
    family.use_example()
    days_off.bind_household()
    bind_household()
    me, partner = family.SELF_EMAIL, family.PARTNER_EMAIL

    a = {"summary": "First Day of Little Oaks", "start": {"date": "2026-09-01"}, "description": ""}
    b = {"summary": "First Day of Little Oaks Preschool",
         "start": {"date": "2026-09-01"},
         "description": "Remember: labeled water bottle. NUT FREE."}
    c = {"summary": "First Day of Little Oaks 2026", "start": {"date": "2026-09-01"}, "description": "x"}
    d = {"summary": "Ava first day of school", "start": {"date": "2026-09-02"}}
    check("the three school-name variants share one key",
          normalise_title(a["summary"]) == normalise_title(b["summary"]) == normalise_title(c["summary"]))
    out = dedupe([a, b, c, d])
    check("collapses 3 duplicates + keeps the distinct day", len(out) == 2)
    check("keeps the RICHEST copy, not the first",
          any(o.get("description", "").startswith("Remember") for o in out))

    # The duplicates as they actually appear in the account: a suffix, not a rewording.
    # An exact-key dedupe kept both copies of the swim lesson for 109 rows.
    swim_cc = {"summary": "Leo Swim Lessons", "start": {"dateTime": "2026-08-09T10:00:00-04:00"},
               "description": "Instructor: Coach Lee"}
    swim_pri = {"summary": "Leo Swim Lessons - private lessons",
                "start": {"dateTime": "2026-08-09T09:30:00-04:00"}, "description": ""}
    conf: list = []
    out2 = dedupe([swim_cc, swim_pri], conf)
    check("collapses a suffix variant (the 109-row swim bug)", len(out2) == 1)
    check("keeps the copy carrying the instructor name",
          "Coach Lee" in (out2[0].get("description") or ""))
    check("records the 10:00 vs 9:30 disagreement instead of hiding it",
          len(conf) == 1 and {conf[0]["kept_time"], conf[0]["dropped_time"]} == {"10:00", "09:30"})

    # Once a parent rules on a series, the ruling holds and the flag stops. The ruling here:
    # the parent's own calendar's 09:30 is the real swim time, CC's 10:00 is wrong.
    swim_cc2 = dict(swim_cc, _calendar_name="CC")
    swim_pri2 = dict(swim_pri, _calendar_name=me)
    conf_pref: list = []
    out_pref = dedupe([swim_cc2, swim_pri2], conf_pref,
                      {normalise_title("Leo Swim Lessons"): me})
    check("a decided series takes the preferred calendar's time",
          len(out_pref) == 1 and start_time(out_pref[0]) == "09:30")
    check("a decided series keeps the richer copy's detail",
          "Coach Lee" in (out_pref[0].get("description") or ""))
    check("a decided series stops flagging a conflict", conf_pref == [])
    # The preference must not silently apply to a DIFFERENT event that also disagrees.
    conf_other: list = []
    dedupe([dict(swim_cc2, summary="Ava Piano Lesson"),
            dict(swim_pri2, summary="Ava Piano Lesson - with Ms. Park")], conf_other,
           {normalise_title("Leo Swim Lessons"): me})
    check("a preference for one series does not settle another", len(conf_other) == 1)

    # The reconcile window must never reach the fetch window's boundary dates, or an event
    # that was simply out of range gets marked deleted. Cost one real row before it was found.
    w = reconcile_window(30, 365)
    check("reconcile window starts inside the fetched range",
          w[0] > (date.today() - timedelta(days=30)).isoformat())
    check("reconcile window ends inside the fetched range",
          w[1] < (date.today() + timedelta(days=365)).isoformat())

    # CC stores UTC, the primary stores -04:00. Same instant, different string.
    check("UTC and -04:00 forms of one instant read as the same clock time",
          start_time({"start": {"dateTime": "2026-08-09T14:00:00Z"}}) ==
          start_time({"start": {"dateTime": "2026-08-09T10:00:00-04:00"}}) == "10:00")
    utc_copy = {"summary": "Leo Swim Lessons", "description": "d",
                "start": {"dateTime": "2026-08-09T14:00:00Z"}}
    local_copy = {"summary": "Leo Swim Lessons - private lessons", "description": "",
                  "start": {"dateTime": "2026-08-09T10:00:00-04:00"}}
    conf2: list = []
    dedupe([utc_copy, local_copy], conf2)
    check("agreeing times across timezones raise NO false conflict", conf2 == [])
    check("all-day events have no start time", start_time({"start": {"date": "2026-09-02"}}) is None)

    # The subset rule alone merged Ava's 9am haircut into someone's 9pm haircut.
    hair = [{"summary": "Ava Haircut", "start": {"dateTime": "2026-07-18T09:00:00-04:00"}},
            {"summary": "Haircut", "start": {"dateTime": "2026-07-18T21:00:00-04:00"}}]
    check("a 12-hour gap means two events, not one copy", len(dedupe(hair)) == 2)
    check("a 30-minute gap still merges (one lesson, two calendars)",
          len(dedupe([swim_cc, swim_pri])) == 1)
    check("an all-day copy never blocks a merge on the clock",
          len(dedupe([{"summary": "Last Day of Camp", "start": {"date": "2026-08-21"}},
                      {"summary": "Little Oaks Last Day of Camp",
                       "start": {"dateTime": "2026-08-21T15:00:00-04:00"}}])) == 1)

    camp = [{"summary": "Last Day of Camp 2026", "start": {"date": "2026-08-21"}, "description": ""},
            {"summary": "Last Day of Little Oaks Summer Camp", "start": {"date": "2026-08-21"},
             "description": "pickup 3pm"},
            {"summary": "Little Oaks Last Day of Summer Camp", "start": {"date": "2026-08-21"},
             "description": ""}]
    check("collapses the three last-day-of-camp variants", len(dedupe(camp)) == 1)
    check("word order alone does not defeat it",
          len(dedupe(camp[1:])) == 1)

    distinct = [{"summary": "Ava Gymnastics", "start": {"date": "2026-09-14"}, "description": ""},
                {"summary": "Leo Dentist", "start": {"date": "2026-09-14"}, "description": ""}]
    check("two unrelated same-day events are NOT collapsed", len(dedupe(distinct)) == 2)
    check("same title on different days stays separate",
          len(dedupe([{"summary": "Weigh in", "start": {"date": "2026-09-01"}},
                      {"summary": "Weigh in", "start": {"date": "2026-09-08"}}])) == 2)

    check("attributes Ava", kid_for("Ava first day of school at Maple Street") == "Ava")
    check("attributes Leo", kid_for("First Day of Little Oaks") == "Leo")
    check("both kids -> Both", kid_for("Ava and Leo 6 month dentist") == "Both")
    check("ambiguous -> Both, not a guess", kid_for("Mattress delivery") == "Both")

    check("a kid's name beats the money words", classify_category("Leo swim payment due") == "kids_school")
    check("money categorized", classify_category("Make Payment Student Loan $400!") == "money")
    check("office day categorized", classify_category(f"{family.PARENTS[0]} in the office") == "work")
    check("errand categorized", classify_category("Weigh in") == "errand")
    check("travel categorized", classify_category("Flight to Chicago (UA 1439)") == "travel_home")
    check("unknown falls through to other, not a guess",
          classify_category("Zzz thing") == "other")
    # Live-data regressions. Each of these was on a kid's dashboard page before it was fixed.
    check("a statement CLOSURE date is not a school closure",
          classify_category("CHECK STATEMENT BALANCE Visa Statement closure date is the "
                            "20th of every month") == "money")
    check("a real school closure still lands in kids_school",
          classify_category("Little Oaks Closed - Labor Day") == "kids_school")
    check("a hotel stay is travel, not other",
          classify_category("Stay at the Grand Hotel downtown") == "travel_home")
    check("end time converts like start time",
          end_time_of({"end": {"dateTime": "2026-08-09T14:55:00Z"}}) == "10:55")
    check("all-day has no end time", end_time_of({"end": {"date": "2026-08-10"}}) is None)

    check("closure typed", classify_type("Little Oaks Closed - Labor Day") == "closure")
    check("meeting typed", classify_type("Back to school night at Maple Street") == "meeting")
    check("deadline typed", classify_type("2021 Subaru Car Inspection") == "deadline")

    def ev(summary, s, e, org, resp):
        return {"summary": summary, "id": summary,
                "start": {"dateTime": s}, "end": {"dateTime": e},
                "organizer": {"email": org},
                "attendees": [{"email": SELF_EMAIL, "self": True, "responseStatus": resp}]}

    booked = ev("Gymnastics", "2026-08-31T16:00:00-04:00", "2026-08-31T16:55:00-04:00",
                partner, "accepted")
    clash = ev("Meet and Greet", "2026-08-31T16:30:00-04:00", "2026-08-31T17:00:00-04:00",
               partner, "needsAction")
    clear = ev("Dentist", "2026-09-28T14:30:00-04:00", "2026-09-28T15:30:00-04:00",
               partner, "needsAction")
    stranger = ev("Webinar", "2026-09-29T14:30:00-04:00", "2026-09-29T15:30:00-04:00",
                  "sales@vendor.com", "needsAction")
    allday = {"summary": "Little Oaks Closed", "id": "x", "start": {"date": "2026-08-31"},
              "end": {"date": "2026-09-01"}, "organizer": {"email": partner},
              "attendees": [{"email": SELF_EMAIL, "self": True, "responseStatus": "needsAction"}]}

    plan = {p["summary"]: p for p in rsvp_plan([booked, clash, clear, stranger, allday])}
    check("accepts a clear invite from the partner", plan["Dentist"]["action"] == "accept")
    check("FLAGS a conflict instead of guessing", plan["Meet and Greet"]["action"] == "flag")
    check("never answers for an untrusted organizer", plan["Webinar"]["action"] == "skip")
    check("an all-day event cannot conflict", plan["Little Oaks Closed"]["action"] == "accept")
    check("already-accepted events are not re-planned", "Gymnastics" not in plan)
    check("the rules never decline", all(p["action"] != "decline" for p in plan.values()))

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--authorize", action="store_true")
    ap.add_argument("--authorize-port", type=int, default=0,
                    help="fixed redirect port, so the flow can be tunnelled")
    ap.add_argument("--no-browser", action="store_true",
                    help="print the consent URL instead of opening one")
    ap.add_argument("--token-health", action="store_true",
                    help="say whether the stored token can still reach the calendar")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rsvp", action="store_true", help="answer invitations by rule")
    ap.add_argument("--days-back", type=int, default=30)
    ap.add_argument("--days-ahead", type=int, default=365)
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if args.authorize:
        return authorize(port=args.authorize_port, open_browser=not args.no_browser)
    if args.token_health:
        h = token_health()
        print(f"token: {h['state'].upper()} -- {h['detail']}")
        # 0 healthy, 1 definitely dead, 2 could not tell. A checker that cannot say
        # "could not tell" reports a network blip as a revoked token.
        return {"ok": 0, "dead": 1}.get(h["state"], 2)

    raw = fetch(args.days_back, args.days_ahead)
    print(f"\n{len(raw)} event(s) fetched")
    conflicts: list = []
    prefs = load_time_preferences()
    if prefs:
        print(f"{len(prefs)} time disagreement(s) already settled by a parent — applying")
    clean = dedupe(raw, conflicts, prefs)
    print(f"{len(clean)} after cross-calendar dedupe ({len(raw) - len(clean)} duplicate(s) collapsed)")
    if conflicts:
        print(f"\n{len(conflicts)} time disagreement(s) between copies — the calendar the "
              f"detail came from is kept, both are shown here:")
        for c in conflicts[:20]:
            print(f"  {c['date']}  {c['kept']} @{c['kept_time']} [{c['kept_cal']}]"
                  f"  vs  {c['dropped']} @{c['dropped_time']} [{c['dropped_cal']}]")
        if len(conflicts) > 20:
            print(f"  ... and {len(conflicts) - 20} more")

    win = reconcile_window(args.days_back, args.days_ahead)
    r = upsert(clean, args.dry_run, conflicts, win)
    print(f"\nevents table: {r['added']} new, {r['updated']} updated, "
          f"{r['cancelled']} marked cancelled (gone upstream), {r['skipped']} skipped"
          f"{' (dry-run)' if args.dry_run else ''}")

    con = db.connect()
    print("\nby category:")
    for row in con.execute("SELECT COALESCE(category,'(none)') c, COUNT(*) n FROM events "
                           "WHERE source='gcal' AND status='active' GROUP BY c ORDER BY n DESC"):
        print(f"  {row['n']:5d}  {row['c']}")
    con.close()

    if args.rsvp:
        plan = rsvp_plan(raw)
        acc = [p for p in plan if p["action"] == "accept"]
        flg = [p for p in plan if p["action"] == "flag"]
        skp = [p for p in plan if p["action"] == "skip"]
        print(f"\nRSVP: {len(acc)} to accept, {len(flg)} flagged for you, {len(skp)} skipped")
        for p in flg:
            print(f"  FLAG  {p['date']}  {p['summary']} — {p['why']}")
        for p in skp:
            print(f"  skip  {p['date']}  {p['summary']} — {p['why']}")
        n = do_rsvp(plan, args.dry_run)
        print(f"{'would accept' if args.dry_run else 'accepted'} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
