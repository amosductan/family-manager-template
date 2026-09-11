"""Family Manager — School Hub dashboard (Flask).

Local: python app.py -> http://127.0.0.1:5088 (AUTH_MODE=dev, no login).
Shared: AUTH_MODE=google + FM_ALLOWLIST, or keep it on a private network (see AGENTS.md).
"""
import io
import json
import os
import _env  # noqa: F401  -- loads this project's .env (personal secrets) before anything reads os.environ
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

import auth
import db
import ask
import family
import geo
import weather
import household
from ingest import STATUS_FILE

ROOT = Path(__file__).resolve().parent
app = Flask(__name__)
auth.install(app)

# Who the household is comes from family.py (data/household.json, or the example family).
# The name stays because other modules and templates read app.KIDS.
KIDS = family.KIDS
TYPE_LABELS = {
    "closure": "Closure", "early_dismissal": "Early dismissal", "deadline": "Deadline",
    "meeting": "Meeting", "camp": "Camp", "event": "Event", "info": "Info",
}


def fetch_events(con, kid=None, start=None, end=None, include_dismissed=False, limit=None,
                 categories=None):
    q = "SELECT * FROM events WHERE 1=1"
    args = []
    if not include_dismissed:
        # 'cancelled' means it vanished from the calendar upstream. Hiding it by default is
        # the point — showing it would be the ghost event this was built to prevent.
        q += " AND status = 'active'"
    if kid:
        # 'Both' carries two different meanings now. From the school feeds it means "affects
        # both kids" (a closure). From the calendar it is what kid_for() returns when a title
        # names no kid at all — and once the whole household was ingested that was 347 rows,
        # so Leo's page filled up with pay days, work days and Jordan's nail appointment.
        # A kid's page shows that kid's own events plus the shared KID ones, nothing else.
        q += " AND (kid = ? OR (kid = 'Both' AND COALESCE(category,'kids_school') "
        q += "= 'kids_school'))"
        args.append(kid)
    if start:
        q += " AND event_date >= ?"
        args.append(start)
    if end:
        q += " AND event_date <= ?"
        args.append(end)
    if categories:
        q += f" AND COALESCE(category,'other') IN ({','.join('?' * len(categories))})"
        args.extend(categories)
    # All-day entries first — a closure or a first-day frames the whole day — then the
    # timed ones in clock order. Sorting by kid put 4pm gymnastics above a 9am lesson.
    q += " ORDER BY event_date ASC, start_time IS NOT NULL, start_time ASC, kid"
    if limit:
        q += f" LIMIT {int(limit)}"
    return dedupe_rows(con.execute(q, args).fetchall())


def _row_richness(r) -> tuple:
    """Which copy of a duplicated happening to keep: the one with the most to say. A timed
    copy beats an all-day one, then detail length, then the calendar over a school feed
    (the calendar copy is the one a parent can open and edit)."""
    src = r["source"] or ""
    return (1 if r["start_time"] else 0, len(r["details"] or ""),
            1 if src.startswith("gcal") or src == "calendar" else 0)


def dedupe_rows(rows):
    """One row per real-world happening on a page, across SOURCES.

    gcal.dedupe() already collapses the four Google calendars into one copy per event, but
    a school closure also arrives from the preschool's email, the district feed and Jordan's
    invite, and each of those is its own `events` row (the table's UNIQUE key includes the
    title, which every feed words differently). Labor Day was four rows on the home page.

    Storage is untouched -- this is a display decision, the same shape as the days-off
    board's merge. Two rows collapse only when they are the SAME DAY, the SAME KID, and
    one normalised title equals or is a word-subset of the other. The subset rule needs
    at least two words on the shorter side, so "Swim" cannot swallow "Swim party".
    """
    import gcal

    # The feeds inflect differently: "Little Oaks Closure - Labor Day" (CC) vs "Little Oaks
    # Preschool Closed - Labor Day" (primary), "Swim Lessons session begins" vs "Swim Lessons
    # begin". Fold the inflections before comparing word sets.
    _STEM = {"closure": "closed", "closures": "closed", "closing": "closed", "close": "closed",
             "begins": "begin", "beginning": "begin", "starts": "start", "starting": "start",
             "ends": "end", "ending": "end", "sessions": "session", "lessons": "lesson",
             "classes": "class", "days": "day", "nights": "night"}

    def toks(title):
        return frozenset(_STEM.get(w, w) for w in gcal._tokens(title or ""))

    by_day: dict = {}
    for r in rows:
        by_day.setdefault((r["event_date"], r["kid"]), []).append(r)
    keep_ids = set()
    for (_, _), group in by_day.items():
        if len(group) == 1:
            keep_ids.add(group[0]["id"])
            continue
        group = sorted(group, key=_row_richness, reverse=True)
        kept: list = []
        for r in group:
            t = toks(r["title"])
            if not t:
                kept.append(r)
                continue
            absorber = None
            for k in kept:
                kt = toks(k["title"])
                if t == kt or (min(len(t), len(kt)) >= 2 and (t <= kt or kt <= t)):
                    absorber = k
                    break
            if absorber is None:
                kept.append(r)
        keep_ids.update(k["id"] for k in kept)
    return [r for r in rows if r["id"] in keep_ids]


# Shown in this order wherever categories are offered. Labels are what a human calls them.
CATEGORIES = [
    ("kids_school", "Kids & school"),
    ("money", "Money"),
    ("work", "Work"),
    ("travel_home", "Travel & home"),
    ("errand", "Errands"),
    ("other", "Other"),
]


def selected_categories(req):
    """Which categories the viewer wants. No selection means everything — the app is
    comprehensive by default and narrowing is the deliberate act, not the reverse."""
    picked = req.args.getlist("cat")
    valid = {k for k, _ in CATEGORIES}
    picked = [c for c in picked if c in valid]
    return picked or None


def category_chips(req, counts, active):
    """Build the filter row's links. In Python, not Jinja, because each href has to carry
    the page's OTHER query args forward — /events?show=past loses its tab otherwise."""
    keys = [k for k, _ in CATEGORIES]
    all_on = set(active) == set(keys)

    def href(cats):
        args = {k: v for k, v in req.args.lists() if k != "cat"}
        if cats and set(cats) != set(keys):
            args["cat"] = cats
        return url_for(req.endpoint, **args, **req.view_args)

    chips = [{"key": None, "label": "All", "n": sum(counts.values()),
              "on": all_on, "href": href([])}]
    for key, label in CATEGORIES:
        # From all-on, one click means "only this" — the intent nine times in ten.
        # Otherwise the chip adds or removes itself from the current selection.
        if all_on:
            nxt = [key]
        elif key in active:
            nxt = [c for c in active if c != key]
        else:
            nxt = [c for c in keys if c in active or c == key]
        chips.append({"key": key, "label": label, "n": counts.get(key, 0),
                      "on": key in active and not all_on, "href": href(nxt)})
    return chips


def group_by_date(rows):
    groups = []
    for r in rows:
        d = r["event_date"]
        if not groups or groups[-1]["date"] != d:
            dt = datetime.strptime(d, "%Y-%m-%d")
            groups.append({"date": d, "display": dt.strftime("%A, %B %-d")
                           if sys.platform != "win32" else dt.strftime("%A, %B %d"),
                           "is_today": d == date.today().isoformat(), "events": []})
        groups[-1]["events"].append(r)
    return groups


@app.template_filter("clock")
def clock(hhmm):
    """24h 'HH:MM' -> '9:30a'. An all-day row renders as a blank column, not the word
    'all day' — the empty slot next to timed neighbours already says it, without noise."""
    if not hhmm:
        return ""
    try:
        h, m = (int(x) for x in str(hhmm).split(":")[:2])
    except ValueError:
        return hhmm
    suffix = "a" if h < 12 else "p"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{suffix}" if m else f"{h12}{suffix}"


@app.template_filter("endclock")
def endclock(hhmm, minutes):
    """When a timed item finishes. Clamped inside the day rather than wrapping past
    midnight, which would render a 10pm start as ending at 12:30a the previous line."""
    if not hhmm or not minutes:
        return ""
    m = _mins(hhmm)
    if m is None:
        return ""
    return clock(_hhmm(min(m + int(minutes), 23 * 60 + 59)))


@app.template_filter("howlong")
def howlong(minutes):
    """90 -> '1 h 30'. Empty string for nothing, so `| howlong or '—'` reads right."""
    if not minutes:
        return ""
    h, r = divmod(int(minutes), 60)
    if h and r:
        return f"{h} h {r} m"
    return f"{h} h" if h else f"{r} m"


@app.template_filter("money")
def money(n, currency="CAD"):
    """Money carries its currency, always. This trip is quoted in two of them and a
    bare number is how a price in one currency gets planned against as another."""
    if n is None:
        return "not priced"
    sym = {"CAD": "CA$", "USD": "$"}.get(currency or "CAD", (currency or "") + " ")
    n = float(n)
    return f"{sym}{n:,.0f}" if abs(n - round(n)) < 0.005 else f"{sym}{n:,.2f}"


@app.template_filter("nice_date")
def nice_date(iso):
    """'2026-11-05' -> 'Thu Nov 5, 2026'. %-d is not a Windows strftime flag."""
    try:
        d = datetime.strptime((iso or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return iso or ""
    return f"{d.strftime('%a %b')} {d.day}, {d.year}"


@app.template_filter("nice_date_noyear")
def nice_date_noyear(iso):
    try:
        d = datetime.strptime((iso or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return iso or ""
    return f"{d.strftime('%b')} {d.day}"


@app.template_filter("fromjson")
def fromjson(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return None


@app.context_processor
def inject_globals():
    # Read from family at render time, not import time, so a household installed after
    # boot (family.reload) shows up without a restart.
    return {"TYPE_LABELS": TYPE_LABELS, "today": date.today().isoformat(),
            "CAT_LABELS": dict(CATEGORIES),
            "PARENTS": family.PARENTS, "KIDS": family.KIDS,
            "FAMILY_NAME": family.FAMILY_NAME, "PARTNER_NAME": family.PARTNER_NAME,
            "IS_EXAMPLE": family.IS_EXAMPLE}


# A kid's chip color by position (kid-0, kid-1, ... ; 'Both' is kid-both) and "Ava + Leo".
app.jinja_env.filters["kid_class"] = family.kid_class
app.jinja_env.filters["kids_label"] = family.kids_label
app.jinja_env.globals["kid_class"] = family.kid_class
app.jinja_env.globals["kids_label"] = family.kids_label


@app.route("/")
def index():
    con = db.connect()
    today = date.today()
    horizon = (today + timedelta(days=21)).isoformat()
    cats = selected_categories(request)
    upcoming = fetch_events(con, start=today.isoformat(), end=horizon, categories=cats)
    cat_counts = {r["c"]: r["n"] for r in con.execute(
        "SELECT COALESCE(category,'other') c, COUNT(*) n FROM events WHERE status='active' "
        "AND event_date BETWEEN ? AND ? GROUP BY c", (today.isoformat(), horizon))}
    sources = [{"row": r, **db.source_status(r)}
               for r in con.execute("SELECT * FROM sources ORDER BY kid, name")]
    payments = con.execute(
        "SELECT * FROM payments WHERE active = 1 ORDER BY next_due IS NULL, next_due"
    ).fetchall()
    due_soon = [p for p in payments if p["next_due"]
                and p["next_due"] <= (today + timedelta(days=30)).isoformat()]
    tbd = [p for p in payments if p["amount"] is None]
    latest_nl = {}
    for kid in KIDS:
        latest_nl[kid] = con.execute(
            "SELECT * FROM newsletters WHERE kid = ? ORDER BY nl_date DESC LIMIT 1", (kid,)
        ).fetchone()
    # A trip is the one thing where the open items matter more the closer it gets, and
    # the home page is where both parents actually land. Only surfaced inside a 30-day
    # window -- a permanent banner for a trip in March is furniture.
    trip = con.execute(
        "SELECT * FROM trips WHERE end_date >= ? AND start_date <= ? "
        "ORDER BY start_date LIMIT 1",
        (today.isoformat(), (today + timedelta(days=30)).isoformat())).fetchone()
    trip_open = 0
    if trip:
        # What the banner counts is what somebody still OWES: a booking nobody has
        # made. An idea on the shelf is not an obligation, and counting it here is how a
        # banner stops meaning anything.
        trip_open = con.execute(
            "SELECT COUNT(*) c FROM trip_items WHERE trip=? AND booking='todo' "
            "AND plan<>'dropped'", (trip["slug"],)).fetchone()["c"]
    counts = {
        "events": con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"],
        "emails": con.execute("SELECT COUNT(*) c FROM emails").fetchone()["c"],
        "newsletters": con.execute("SELECT COUNT(*) c FROM newsletters").fetchone()["c"],
    }
    # The next gathering being planned, inside its own window (60 days) -- the same rule as
    # the trip: a card for a party in March is furniture.
    try:
        import gatherings
        gathering = gatherings.next_for_home(con, today)
    except Exception:
        gathering = None
    # What school mail is still asking of a parent — the whole point of reading the mail deeply.
    shared_tasks = household.collect(con, today)
    needs_you = [t for t in shared_tasks if t['state'] == 'open' and not t['snoozed'] and not t['review']][:6]
    action_counts = {
        'open': sum(t['state'] == 'open' and not t['snoozed'] and not t['review'] for t in shared_tasks),
        'review': sum(t['review'] for t in shared_tasks),
        'coverage': sum(t['key'].startswith('coverage:') and t['state'] == 'open'
                        and t['due'] <= (today + timedelta(days=14)).isoformat() for t in shared_tasks),
    }
    payment_changes = [p for p in household.plans(con, today) if p['upcoming']]
    today_events = fetch_events(con, start=today.isoformat(), end=today.isoformat())
    school_today = school_today_lines(con)
    con.close()
    return render_template("index.html", groups=group_by_date(upcoming), needs_you=needs_you,
                           school_today=school_today,
                           action_counts=action_counts, today_events=today_events,
                           payment_changes=payment_changes,
                           trip=trip, trip_open=trip_open, gathering=gathering,
                           trip_media=_trip_media(trip["slug"]) if trip else None,
                           trip_days=(datetime.strptime(trip["start_date"], "%Y-%m-%d")
                                      .date() - today).days if trip else None,
                           sources=sources, payments=payments, due_soon=due_soon,
                           tbd=tbd, latest_nl=latest_nl, counts=counts, kids=KIDS,
                           chips=category_chips(request, cat_counts,
                                                cats or [k for k, _ in CATEGORIES]))


@app.route("/kid/<kid>")
def kid_page(kid):
    if kid not in KIDS:
        return redirect(url_for("index"))
    con = db.connect()
    today = date.today().isoformat()
    upcoming = fetch_events(con, kid=kid, start=today)
    past = con.execute(
        "SELECT * FROM events WHERE (kid = ? OR (kid = 'Both' AND "
        "COALESCE(category,'kids_school') = 'kids_school')) AND status='active' "
        "AND event_date < ? ORDER BY event_date DESC, start_time DESC LIMIT 25",
        (kid, today)).fetchall()
    newsletters = con.execute(
        "SELECT * FROM newsletters WHERE kid = ? ORDER BY nl_date DESC LIMIT 12",
        (kid,)).fetchall()
    nl_items = []
    for nl in newsletters:
        content = json.loads(nl["content"] or "{}")
        highlights = []
        if "summary" in content and content["summary"]:
            highlights.append(content["summary"])
        for sec, items in content.items():
            if isinstance(items, list):
                for it in items[:3]:
                    if isinstance(it, dict) and it.get("title"):
                        highlights.append(it["title"])
        nl_items.append({"row": nl, "highlights": highlights[:5]})
    routines = _routines(con, kid)
    con.close()
    return render_template("kid.html", kid=kid, groups=group_by_date(upcoming),
                           past=past, newsletters=nl_items, routines=routines)


PAYMENT_CATEGORIES = ["tuition", "camp", "aftercare", "activity", "other"]
# Cadence -> what the "when due" box asks for. A weekly swim class is due "Mondays", not on a
# date; the date box stays for the next SPECIFIC instance or a one-time charge.
PAYMENT_CADENCES = [
    ("weekly", "Weekly", "e.g. Mondays"),
    ("biweekly", "Every 2 weeks", "e.g. every other Friday"),
    ("monthly", "Monthly", "e.g. the 1st"),
    ("per-session", "Per session", "e.g. each class, at the desk"),
    ("semester", "Per semester / term", "e.g. start of each term"),
    ("annual", "Annual", "e.g. every August"),
    ("one-time", "One-time", "use the date box"),
]


def when_due(p) -> str:
    """One human phrase for a payment's timing: the rule when there is one, the cadence
    otherwise, and the next specific date appended when it is known."""
    cadence = dict((k, lbl) for k, lbl, _ in PAYMENT_CADENCES).get(p["cadence"], p["cadence"] or "")
    rule = (p["due_rule"] or "").strip()
    nxt = p["next_due"]
    if p["cadence"] == "one-time":
        return nxt or rule or "date TBD"
    head = f"{cadence} · {rule}" if rule else cadence
    return f"{head} · next {nxt}" if nxt else head


def _payment_fields(f) -> tuple:
    amount = (f.get("amount") or "").strip()
    return (f["name"].strip()[:120], (f.get("activity") or "").strip()[:80] or None,
            (f.get("organization") or "").strip()[:120] or None,
            f["kid"] if f["kid"] in KIDS + ["Both"] else "Both",
            f["category"] if f["category"] in PAYMENT_CATEGORIES else "other",
            float(amount) if amount else None,
            f["cadence"] if f["cadence"] in dict((k, l) for k, l, _ in PAYMENT_CADENCES) else "monthly",
            (f.get("due_rule") or "").strip()[:80] or None,
            f.get("next_due") or None,
            1 if f.get("autopay") else 0, (f.get("notes") or "").strip())


@app.route("/payments", methods=["GET", "POST"])
def payments_page():
    con = db.connect()
    if request.method == "POST":
        f = request.form
        vals = _payment_fields(f)
        if f.get("id"):  # edit
            con.execute(
                "UPDATE payments SET name=?, activity=?, organization=?, kid=?, category=?, amount=?, "
                "cadence=?, due_rule=?, next_due=?, autopay=?, notes=?, active=? WHERE id=?",
                (*vals, 1 if f.get("active") else 0, f["id"]))
        else:
            con.execute(
                "INSERT INTO payments (name, activity, organization, kid, category, amount, cadence, "
                "due_rule, next_due, autopay, notes, active) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)", vals)
        con.commit()
        con.close()
        return redirect(url_for("payments_page"))
    rows = con.execute(
        "SELECT * FROM payments ORDER BY active DESC, next_due IS NULL, next_due, name").fetchall()
    con.close()
    return render_template("payments.html", payments=rows, kids=KIDS + ["Both"],
                           categories=PAYMENT_CATEGORIES, cadences=PAYMENT_CADENCES, when_due=when_due)


@app.route("/payments.xlsx")
def payments_xlsx():
    """Every table a human looks at is downloadable."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    con = db.connect()
    rows = con.execute("SELECT * FROM payments ORDER BY active DESC, kid, name").fetchall()
    con.close()
    cols = ["Name", "Activity", "Organization", "Kid", "Category", "Amount", "Cadence", "Due rule",
            "Next date", "When due", "Autopay", "Active", "Notes"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Payments"
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="C4974C")
        cell.alignment = Alignment(vertical="center")
    for r in rows:
        ws.append([r["name"], r["activity"], r["organization"], r["kid"], r["category"], r["amount"],
                   r["cadence"], r["due_rule"], r["next_due"], when_due(r),
                   "yes" if r["autopay"] else "no", "yes" if r["active"] else "no", r["notes"]])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, c in enumerate(cols, 1):
        width = max([len(str(c))] + [len(str(ws.cell(row=j, column=i).value or "")) for j in range(2, ws.max_row + 1)])
        ws.column_dimensions[get_column_letter(i)].width = min(60, max(10, width + 2))
    buf = io.BytesIO()
    wb.save(buf)
    from flask import Response
    return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=payments-{date.today().isoformat()}.xlsx"})


@app.route("/events")
def events_page():
    con = db.connect()
    show = request.args.get("show", "upcoming")
    today = date.today().isoformat()
    cats = selected_categories(request)
    if show == "past":
        q = ("SELECT * FROM events WHERE status='active' AND event_date < ?")
        args = [today]
        if cats:
            q += f" AND COALESCE(category,'other') IN ({','.join('?' * len(cats))})"
            args += cats
        q += " ORDER BY event_date DESC, start_time IS NOT NULL, start_time ASC LIMIT 200"
        rows = con.execute(q, args).fetchall()
        counted = ("event_date < ?", (today,))
    else:
        rows = fetch_events(con, start=today, categories=cats)
        counted = ("event_date >= ?", (today,))
    cat_counts = {r["c"]: r["n"] for r in con.execute(
        "SELECT COALESCE(category,'other') c, COUNT(*) n FROM events WHERE status='active' "
        f"AND {counted[0]} GROUP BY c", counted[1])}
    con.close()
    return render_template("events.html", groups=group_by_date(rows), show=show,
                           chips=category_chips(request, cat_counts,
                                                cats or [k for k, _ in CATEGORIES]))


@app.route("/events/<int:event_id>/dismiss", methods=["POST"])
def dismiss_event(event_id):
    con = db.connect()
    con.execute("UPDATE events SET status='dismissed' WHERE id=?", (event_id,))
    con.commit()
    con.close()
    return redirect(request.referrer or url_for("index"))


def shift_time(end, old_start, new_start):
    """Move `end` by however far the start moved, preserving the duration. None-safe."""
    if not end or not old_start or not new_start:
        return end
    try:
        mins = lambda t: int(t[:2]) * 60 + int(t[3:5])
        m = mins(end) + (mins(new_start) - mins(old_start))
    except (ValueError, IndexError):
        return end
    m %= 24 * 60
    return f"{m // 60:02d}:{m % 60:02d}"


@app.post("/events/<int:event_id>/resolve-time")
def resolve_time(event_id):
    """A parent settles a two-calendar time disagreement, for the whole recurring series.

    Nothing in the code can know whether a kid's swim lesson is at 9:30 or 10:00 — the
    calendars disagree and both belong to the same parent. Before this, resolving it meant editing a
    dict in gcal.py. Now it is a button, the ruling covers every future instance, and the
    next ingest applies it instead of re-flagging 52 weeks.
    """
    con = db.connect()
    row = con.execute("SELECT title, conflict FROM events WHERE id=?", (event_id,)).fetchone()
    conf = json.loads(row["conflict"]) if row and row["conflict"] else None
    if not conf:
        con.close()
        return jsonify({"error": "no recorded disagreement on this event"}), 404

    prefer = request.form.get("calendar") or (request.json or {}).get("calendar")
    if prefer not in (conf.get("kept_cal"), conf.get("dropped_cal")):
        con.close()
        return jsonify({"error": "that calendar is not one of the two in dispute"}), 400

    import gcal
    key = gcal.normalise_title(row["title"])
    con.execute("INSERT OR REPLACE INTO time_preferences (title_key, prefer_calendar, "
                "decided_by, decided_at) VALUES (?,?,?,?)",
                (key, prefer, request.form.get("who") or "a parent", db.now()))
    # Apply it to every instance of the series NOW. Clearing the flag alone would leave
    # the board showing the rejected time with nothing to say it was disputed — worse than
    # before the click. The nightly ingest re-derives both times from Google regardless.
    cleared = 0
    for r in con.execute("SELECT id, start_time, end_time, conflict FROM events "
                         "WHERE source='gcal' AND conflict IS NOT NULL AND title=?",
                         (row["title"],)).fetchall():
        c = json.loads(r["conflict"])
        want = c["kept_time"] if prefer == c.get("kept_cal") else c["dropped_time"]
        # Shift the end by the same amount so the duration survives; only the start times
        # were ever in dispute, and the next ingest replaces both with Google's own.
        end = shift_time(r["end_time"], r["start_time"], want)
        con.execute("UPDATE events SET start_time=?, end_time=?, conflict=NULL WHERE id=?",
                    (want, end, r["id"]))
        cleared += 1
    con.commit()
    con.close()
    return redirect(request.referrer or url_for("index")) if request.form else \
        jsonify({"ok": True, "series": row["title"], "prefer": prefer, "cleared": cleared})



# ---------------------------------------------------------------------------
# Ask -- a question in plain words, answered from the family's own records
# ---------------------------------------------------------------------------

ASK_EXAMPLES = [
    "What time is back to school night?",
    "When is the next day off school?",
    "What still needs a parent this week?",
    "Add swim lessons, $45 a week on Mondays",
    "Add 'return the permission slip' to the checklist, due Tuesday",
    "What's on the calendar this weekend?",
]


@app.route("/ask")
def ask_page():
    con = db.connect()
    hist = ask.history(con, limit=20)
    con.close()
    latest = None
    if hist and hist[0]["status"] == "working":
        con = db.connect(); latest = ask.row(con, hist[0]["id"]); con.close()
        hist = hist[1:]
    q = (request.args.get("q") or "").strip()
    return render_template("ask.html", latest=latest, history=hist, examples=ASK_EXAMPLES,
                           prefill=q, autostart=bool(q), describe=ask.describe)


@app.post("/ask")
def ask_start():
    q = (request.form.get("q") or "").strip()
    up = request.files.get("file")
    upload = None
    if up and up.filename:
        data = up.read()
        if len(data) > ask.MAX_UPLOAD:
            return jsonify({"error": "That file is over 15 MB."}), 413
        if not data:
            return jsonify({"error": "That file is empty."}), 400
        upload = (up.filename, data)
        if len(q) < 3:
            q = f"What in this document matters for the family, and what should be recorded? ({up.filename})"
    if len(q) < 3:
        return jsonify({"error": "Ask a question first."}), 400
    who = (request.form.get("who") or "").strip()[:20] or None
    con = db.connect()
    hist = ask.history(con, limit=1)
    con.close()
    if hist and hist[0]["status"] == "working" and hist[0]["question"] == q and not upload:
        return jsonify({"id": hist[0]["id"], "status": "working", "stage": hist[0]["stage"]}), 202
    aid = ask.start(q, who, upload)
    return jsonify({"id": aid, "status": "working",
                    "stage": "Reading the document" if upload else "Finding matching entries"}), 202


@app.route("/ask/<int:ask_id>.json")
def ask_status(ask_id):
    con = db.connect()
    r = ask.row(con, ask_id)
    con.close()
    if not r:
        return jsonify({"error": "no such question"}), 404
    return jsonify({k: r[k] for k in ("id", "status", "stage", "error", "seconds")} | {"n_actions": len(r["actions"])})


@app.post("/ask/<int:ask_id>/apply")
def ask_apply(ask_id):
    """Apply one proposed change (idx) or all of them. The ONLY path by which anything Ask
    proposed reaches the database -- a parent tapped it."""
    data = request.get_json(silent=True) or {}
    idx = data.get("idx")
    who = (data.get("who") or "").strip()[:20] or None
    r = ask.apply(ask_id, None if idx in (None, "all") else int(idx), who)
    if not r:
        return jsonify({"error": "no proposed changes on that question"}), 404
    return jsonify({"id": ask_id, "actions": r["actions"]})


@app.route("/ask/<int:ask_id>")
def ask_view(ask_id):
    con = db.connect()
    r = ask.row(con, ask_id)
    hist = [h for h in ask.history(con, limit=21) if h["id"] != ask_id][:20]
    con.close()
    if not r:
        return redirect(url_for("ask_page"))
    return render_template("ask.html", latest=r, history=hist, examples=ASK_EXAMPLES, prefill="",
                           autostart=False, describe=ask.describe)

@app.route("/checklist")
def checklist_page():
    """The shared to-do board. Both parents see the same list on the home network, so
    the point of the page is not the list — it is who ticked what, and when."""
    con = db.connect()
    show_done = request.args.get("done") == "1"
    q = "SELECT * FROM checklist"
    if not show_done:
        q += " WHERE status != 'na'"
    q += " ORDER BY kid, category, sort, id"
    rows = con.execute(q).fetchall()
    con.close()

    groups = []
    for r in rows:
        key = (r["kid"], r["category"])
        if not groups or groups[-1]["key"] != key:
            # NOT "items" — Jinja resolves g.items to the dict method, not the key.
            groups.append({"key": key, "kid": r["kid"], "category": r["category"],
                           "entries": []})
        groups[-1]["entries"].append(r)
    for g in groups:
        g["open"] = sum(1 for i in g["entries"] if i["status"] == "open")
        g["blocked"] = sum(1 for i in g["entries"] if i["status"] == "blocked")
        g["total"] = len(g["entries"])
        g["done"] = sum(1 for i in g["entries"] if i["status"] == "done")

    totals = {
        "open": sum(1 for r in rows if r["status"] == "open"),
        "done": sum(1 for r in rows if r["status"] == "done"),
        "blocked": sum(1 for r in rows if r["status"] == "blocked"),
    }
    return render_template("checklist.html", groups=groups, totals=totals,
                           show_done=show_done, kids=KIDS + ["Both"],
                           categories=db.CHECKLIST_CATEGORIES)


@app.route("/checklist/toggle", methods=["POST"])
def checklist_toggle():
    """Tick or untick one item. `who` comes from the browser's parent picker —
    with AUTH_MODE=dev on the home network there is no signed-in identity, and an
    unattributed tick is the thing that makes a shared list useless."""
    data = request.get_json(silent=True) or {}
    item_id = data.get("id")
    who = (data.get("who") or "").strip()[:20] or None
    if not item_id:
        return jsonify({"error": "missing id"}), 400

    con = db.connect()
    row = con.execute("SELECT * FROM checklist WHERE id=?", (item_id,)).fetchone()
    if not row:
        con.close()
        return jsonify({"error": "no such item"}), 404

    # Blocked items stay blocked until the blocker clears — ticking one would
    # hide a dependency both parents need to see.
    if row["status"] == "blocked" and not data.get("force"):
        con.close()
        return jsonify({"error": "blocked", "blocked_on": row["blocked_on"]}), 409

    if row["status"] == "done":
        con.execute("UPDATE checklist SET status='open', done_at=NULL, done_by=NULL "
                    "WHERE id=?", (item_id,))
        new = {"status": "open", "done_at": None, "done_by": None}
    else:
        stamp = db.now()
        con.execute("UPDATE checklist SET status='done', done_at=?, done_by=? WHERE id=?",
                    (stamp, who, item_id))
        new = {"status": "done", "done_at": stamp, "done_by": who}
    con.commit()
    con.close()
    return jsonify({"id": item_id, **new})


@app.route("/checklist/add", methods=["POST"])
def checklist_add():
    f = request.form
    con = db.connect()
    con.execute(
        "INSERT OR IGNORE INTO checklist (title, detail, kid, category, due_date, "
        "status, source, sort) VALUES (?,?,?,?,?,'open',?,?)",
        (f["title"].strip(), f.get("detail", "").strip(), f["kid"], f["category"],
         f.get("due_date") or None, "added by hand", 200))
    con.commit()
    con.close()
    return redirect(url_for("checklist_page"))


@app.route("/checklist.xlsx")
def checklist_xlsx():
    """Every table a human looks at is downloadable. Real .xlsx, not a CSV floor —
    openpyxl is already a dependency here."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    con = db.connect()
    rows = con.execute("SELECT * FROM checklist ORDER BY kid, category, sort, id").fetchall()
    con.close()

    cols = ["Kid", "Category", "Item", "Detail", "Due", "Status", "Blocked on",
            "Done by", "Done at", "Source"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Checklist"
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="C4974C")
        cell.alignment = Alignment(vertical="center")
    for r in rows:
        ws.append([r["kid"], r["category"], r["title"], r["detail"], r["due_date"],
                   r["status"], r["blocked_on"], r["done_by"],
                   (r["done_at"] or "")[:16].replace("T", " "), r["source"]])
    widths = [8, 10, 44, 60, 11, 9, 34, 9, 17, 34]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{ws.max_row}"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from flask import send_file

    return send_file(buf, as_attachment=True,
                     download_name=f"family-checklist-{date.today().isoformat()}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument."
                              "spreadsheetml.sheet")


# ---------------------------------------------------------------- days off / coverage

PARTNER_EMAIL = os.environ.get("FM_PARTNER_EMAIL", family.PARTNER_EMAIL)
# What a button says. "invite jordan" (an address) is not a person; the name is.
PARTNER_NAME = os.environ.get("FM_PARTNER_NAME", family.PARTNER_NAME)
HUB_URL = os.environ.get("FM_HUB_URL", family.HUB_URL)


def _out_day_rows(con):
    """Every closure / early-dismissal row from EVERY feed, unfiltered.

    Deliberately not deduped here — days_off.merge_rows does that across sources, which
    nothing in the ingest can do (Labor Day arrives as four rows from four feeds).
    """
    return con.execute(
        "SELECT event_date, end_date, kid, type, title, details FROM events "
        "WHERE status='active' AND type IN ('closure','early_dismissal') "
        "AND category='kids_school' AND event_date IS NOT NULL "
        "ORDER BY event_date").fetchall()


def _plans(con) -> dict:
    import sitters

    return sitters.plans_with_sitters(con)


def _holidays(con) -> dict:
    return {r["day"]: r["label"] for r in con.execute("SELECT * FROM company_holidays")}


def _board(con, today=None, rejected=None):
    import days_off

    return days_off.board(_out_day_rows(con), _plans(con), _holidays(con),
                          today=today or date.today().isoformat(),
                          rejected_out=rejected)


@app.route("/days-off")
def days_off_page():
    import days_off

    con = db.connect()
    rejected = []
    rows = _board(con, rejected=rejected)
    holidays = sorted(_holidays(con).items())
    con.close()
    # Deduped for display: one bad feed row can be rejected once but explains many days.
    seen, rejects = set(), []
    for day, title, why in rejected:
        if (title, why) in seen:
            continue
        seen.add((title, why))
        rejects.append({"day": day, "title": title, "why": why})

    counts = {
        "total": len(rows),
        "full": sum(1 for r in rows if r["kind"] == days_off.FULL),
        "early": sum(1 for r in rows if r["kind"] == days_off.EARLY),
        # The number that matters: days nobody has claimed yet, company holidays excluded
        # because those are already covered by both parents being off.
        "tbd": sum(1 for r in rows
                   if r["coverage"] == days_off.COVERAGE_TBD and not r["company_holiday"]),
        "on_calendar": sum(1 for r in rows if r["gcal_event_id"]),
    }
    import sitters

    con = db.connect()
    roster = sitters.roster(con, include_inactive=False)
    con.close()
    return render_template("days_off.html", rows=rows, counts=counts, holidays=holidays,
                           months=days_off.display_months(rows), rejects=rejects,
                           coverage_options=days_off.COVERAGE_OPTIONS, sitters=roster,
                           pto_options=days_off.PTO_OPTIONS,
                           partner=PARTNER_EMAIL, FULL=days_off.FULL, EARLY=days_off.EARLY)


@app.route("/days-off/plan", methods=["POST"])
def days_off_plan():
    """Record a human decision about one day. Never touches the derived school data."""
    import days_off

    d = request.get_json(silent=True) or {}
    day = (d.get("day") or "").strip()
    if not day:
        return jsonify({"error": "missing day"}), 400
    try:
        day = household.iso(day, False)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    fields, vals = [], []
    if 'confirmation' in d:
        if d['confirmation'] not in ('proposed', 'asked', 'confirmed'):
            return jsonify(error='Choose Proposed, Asked, or Confirmed.'), 400
        if d.get('who') not in family.PARENTS:
            return jsonify(error='Choose your name in the parent picker first.'), 400
        fields.append('confirmation')
        vals.append(d['confirmation'])
    if "coverage" in d:
        if d["coverage"] not in days_off.COVERAGE_OPTIONS:
            return jsonify({"error": "bad coverage"}), 400
        fields.append("coverage")
        vals.append(d["coverage"])
    if "pto" in d:
        if d["pto"] not in days_off.PTO_OPTIONS:
            return jsonify({"error": "bad pto"}), 400
        fields.append("pto")
        vals.append(d["pto"])
    if "note" in d:
        fields.append("note")
        vals.append((d.get("note") or "").strip()[:400])
    if "sitter_id" in d:
        sid = d.get("sitter_id")
        if sid in ("", None):
            sid = None
        else:
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                return jsonify({"error": "bad sitter"}), 400
        fields.append("sitter_id")
        vals.append(sid)
    if "coverage" in d and d["coverage"] != days_off.COVERAGE_BABYSITTER and "sitter_id" not in d:
        # Moving the day to a parent clears the sitter; a stale sitter id on a parent's
        # day would come back the moment coverage flips again.
        fields.append("sitter_id")
        vals.append(None)
    if not fields:
        return jsonify({"error": "nothing to set"}), 400

    con = db.connect()
    if "sitter_id" in fields:
        sid = vals[fields.index("sitter_id")]
        if sid is not None and not con.execute("SELECT 1 FROM sitters WHERE id=?", (sid,)).fetchone():
            con.close()
            return jsonify({"error": "no such sitter"}), 400
    con.execute("INSERT OR IGNORE INTO coverage_plan (day) VALUES (?)", (day,))
    previous = dict(con.execute('SELECT * FROM coverage_plan WHERE day=?', (day,)).fetchone())
    proposed = {**previous, **dict(zip(fields, vals))}
    changed_person = any(k in d and proposed.get(k) != previous.get(k) for k in ('coverage', 'sitter_id'))
    if changed_person:
        if 'confirmation' in fields:
            vals[fields.index('confirmation')] = 'proposed'
        else:
            fields.append('confirmation')
            vals.append('proposed')
    elif proposed.get('confirmation') == 'confirmed':
        if proposed.get('coverage') in (None, '', 'TBD') or (proposed.get('coverage') == 'Babysitter' and not proposed.get('sitter_id')):
            con.rollback()
            con.close()
            return jsonify(error='Choose who is covering the day before confirming.'), 400
    con.execute(f"UPDATE coverage_plan SET {', '.join(f + '=?' for f in fields)}, "
                f"updated_at=?, updated_by=? WHERE day=?",
                (*vals, db.now(), (d.get("who") or "").strip()[:20] or None, day))
    con.commit()
    row = dict(con.execute(
        "SELECT p.*, s.name AS sitter FROM coverage_plan p LEFT JOIN sitters s ON s.id=p.sitter_id "
        "WHERE p.day=?", (day,)).fetchone())
    con.close()
    return jsonify(row)


def _blocks_for(con, days: list[str], kind: str):
    """Turn selected dates into the calendar events that would be created.

    Refuses rather than guesses: a day already on the calendar is skipped (idempotent),
    and a day-off event with nobody assigned is refused, because "TBD off" is not a thing
    a calendar can mean.
    """
    import days_off

    wanted = set(days)
    rows = [r for r in _board(con) if r["day"] in wanted]
    skipped = []
    usable = []
    for r in rows:
        if r["kind"] != kind:
            skipped.append((r["day"], f"is a {r['kind']} day, not {kind}"))
        elif r["gcal_event_id"]:
            skipped.append((r["day"], "already on the calendar"))
        elif r.get('confirmation') != 'confirmed':
            skipped.append((r['day'], 'coverage is tentative — confirm the arrangement first'))
        elif kind == days_off.FULL and r["coverage"] == days_off.COVERAGE_TBD:
            skipped.append((r["day"], "nobody assigned yet — set coverage first"))
        else:
            usable.append(r)
    missing = wanted - {r["day"] for r in rows}
    skipped.extend((d, "not a school-out day") for d in sorted(missing))
    return days_off.collapse_runs(usable), skipped


@app.route("/days-off/preview", methods=["POST"])
def days_off_preview():
    import days_off

    d = request.get_json(silent=True) or {}
    kind = d.get("kind")
    if kind not in (days_off.FULL, days_off.EARLY):
        return jsonify({"error": "bad kind"}), 400
    con = db.connect()
    blocks, skipped = _blocks_for(con, d.get("days") or [], kind)
    con.close()
    return jsonify({
        "blocks": [{
            "start": b["start"], "end": b["end"], "days": [x["day"] for x in b["days"]],
            "summary": days_off.event_summary(b),
            "key": days_off.fm_key(b),
        } for b in blocks],
        "skipped": [{"day": d_, "why": w} for d_, w in skipped],
        "partner": PARTNER_EMAIL,
    })


@app.route("/days-off/create-one", methods=["POST"])
def days_off_create_one():
    """Create ONE block. The browser walks the list so it can show real i/N progress
    instead of a spinner that says nothing while 12 round trips happen."""
    import days_off
    import gcal

    d = request.get_json(silent=True) or {}
    kind = d.get("kind")
    if kind not in (days_off.FULL, days_off.EARLY):
        return jsonify({"error": "bad kind"}), 400

    con = db.connect()
    blocks, _ = _blocks_for(con, d.get("days") or [], kind)
    if not blocks:
        con.close()
        return jsonify({"error": "nothing to create"}), 400
    block = blocks[0]
    key = days_off.fm_key(block)

    try:
        sess = gcal.session()
        # The stored id is the fast path; this catches a calendar that already has the
        # event when the DB does not (restored backup, created elsewhere).
        existing = gcal.find_by_key(sess, key)
        if existing:
            ev = existing
            created = False
        else:
            if kind == days_off.FULL:
                ev = gcal.create_event(
                    sess, summary=days_off.event_summary(block),
                    description=days_off.event_description(block),
                    start=block["start"], end=block["end"],
                    attendees=[PARTNER_EMAIL], key=key, all_day=True)
            else:
                # A pickup is a timed afternoon block, never an all-day "off" event.
                ev = gcal.create_event(
                    sess, summary=days_off.event_summary(block),
                    description=days_off.event_description(block),
                    start=block["start"], end=block["start"],
                    attendees=[PARTNER_EMAIL], key=key, all_day=False,
                    start_time_="12:00", end_time_="17:00")
            created = True
    except Exception as exc:  # noqa: BLE001 — surfaced to the user, not swallowed
        con.close()
        return jsonify({"error": str(exc)[:300]}), 502

    gkind = days_off.GCAL_DAYOFF if kind == days_off.FULL else days_off.GCAL_PICKUP
    for x in block["days"]:
        con.execute("INSERT OR IGNORE INTO coverage_plan (day) VALUES (?)", (x["day"],))
        con.execute("UPDATE coverage_plan SET gcal_event_id=?, gcal_kind=?, updated_at=? "
                    "WHERE day=?", (ev["id"], gkind, db.now(), x["day"]))
    con.commit()
    con.close()
    return jsonify({"created": created, "id": ev["id"], "link": ev.get("htmlLink"),
                    "summary": block and days_off.event_summary(block),
                    "days": [x["day"] for x in block["days"]]})


@app.route("/days-off/remove", methods=["POST"])
def days_off_remove():
    """Undo. Deletes the calendar event and clears the days that pointed at it — all of
    them, since one event can cover a whole week."""
    import gcal

    d = request.get_json(silent=True) or {}
    event_id = (d.get("event_id") or "").strip()
    if not event_id:
        return jsonify({"error": "missing event_id"}), 400
    try:
        ok = gcal.delete_event(gcal.session(), event_id)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)[:300]}), 502
    if not ok:
        return jsonify({"error": "calendar refused the delete"}), 502
    con = db.connect()
    n = con.execute("UPDATE coverage_plan SET gcal_event_id=NULL, gcal_kind=NULL, "
                    "updated_at=? WHERE gcal_event_id=?", (db.now(), event_id)).rowcount
    con.commit()
    con.close()
    return jsonify({"removed": n})


@app.route("/days-off/holidays", methods=["POST"])
def days_off_holidays():
    """A parent's employer holiday list. Not derivable from anything here, changes yearly,
    and it is the input that decides whether a closure costs a PTO day."""
    raw = request.form.get("holidays", "")
    con = db.connect()
    con.execute("DELETE FROM company_holidays")
    kept = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        day = parts[0].strip().rstrip(",")
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        con.execute("INSERT OR REPLACE INTO company_holidays (day, label) VALUES (?,?)",
                    (day, (parts[1].strip() if len(parts) > 1 else "Company holiday")))
        kept += 1
    con.commit()
    con.close()
    return redirect(url_for("days_off_page"))


@app.route("/days-off.json")
def days_off_json():
    """The board as data. Exists so checks can derive their own fixtures instead of
    hardcoding dates — a hardcoded pair of days that happen to share a kid-set on one
    machine stops sharing it on another, and the check fails for the wrong reason."""
    con = db.connect()
    rejected = []
    rows = _board(con, rejected=rejected)
    con.close()
    return jsonify({"days": rows,
                    "rejected": [{"day": d, "title": t, "why": w} for d, t, w in rejected]})


@app.route("/days-off.xlsx")
def days_off_xlsx():
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    con = db.connect()
    rows = _board(con)
    con.close()

    cols = ["Date", "Day", "Type", "Who is out", "Company holiday", "Coverage",
            "Arrangement", "Time off", "On calendar", "Note", "Why school is out"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Days off"
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="C4974C")
        cell.alignment = Alignment(vertical="center")
    for r in rows:
        ws.append([
            r["day"], r["weekday"],
            "School closed" if r["kind"] == "full" else "Early dismissal",
            r["who"], r["holiday_label"] or "", r["coverage"], r['confirmation'], r["pto"],
            "yes" if r["gcal_event_id"] else "", r["note"],
            "; ".join(f"{k}: {v}" for k, v in sorted(r["titles"].items())),
        ])
    for i, w in enumerate([12, 6, 15, 14, 20, 14, 14, 11, 12, 30, 60], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{ws.max_row}"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from flask import send_file

    return send_file(buf, as_attachment=True,
                     download_name=f"family-days-off-{date.today().isoformat()}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument."
                              "spreadsheetml.sheet")


@app.route("/ingest/run", methods=["POST"])
def ingest_run():
    status = _read_status()
    if status.get("running"):
        return jsonify(status), 409
    subprocess.Popen([sys.executable, str(ROOT / "ingest.py")], cwd=str(ROOT),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return jsonify({"running": True, "stage": "starting"})


@app.route("/ingest/status")
def ingest_status():
    return jsonify(_read_status())


# ---- Mail: the deep read of school/kid email (summary + every document, for review) ----
try:
    import mailsweep
except ImportError:  # the Mail deep-read is in flight in another session; the hub must still boot without it
    mailsweep = None


def _summary_of(row):
    try:
        return json.loads(row["summary"]) if row["summary"] else {}
    except (ValueError, TypeError):
        return {}


@app.route("/mail")
def mail_list():
    con = db.connect()
    kid = request.args.get("kid")
    src = request.args.get("source")
    q = ("SELECT e.*, "
         "(SELECT COUNT(*) FROM mail_documents d WHERE d.msg_id=e.msg_id) n_docs, "
         "(SELECT COUNT(*) FROM mail_actions a WHERE a.msg_id=e.msg_id AND a.state='open') n_open "
         "FROM emails e WHERE 1=1")
    args = []
    if kid in KIDS or kid == "Both":
        q += " AND (e.kid=? OR e.kid='Both')"
        args.append(kid)
    if src:
        q += " AND e.source=?"
        args.append(src)
    q += " ORDER BY e.sent_date DESC, e.created_at DESC LIMIT 200"
    rows = con.execute(q, args).fetchall()
    items = []
    for r in rows:
        sm = _summary_of(r)
        items.append({"row": r, "summary": sm,
                      "headline": sm.get("headline") or r["subject"],
                      "n_docs": r["n_docs"], "n_open": r["n_open"]})
    sources = con.execute("SELECT DISTINCT source FROM emails ORDER BY source").fetchall()
    open_actions = db.open_actions(con, limit=100)
    suggestions = con.execute(
        "SELECT * FROM mail_suggestions WHERE status='new' ORDER BY score DESC, hits DESC "
        "LIMIT 30").fetchall()
    con.close()
    return render_template("mail.html", items=items, sources=sources, kid=kid, src=src,
                           kids=KIDS, open_actions=open_actions, suggestions=suggestions,
                           status=_read_status())


@app.route("/mail/<int:eid>")
def mail_detail(eid):
    con = db.connect()
    row = con.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    if not row:
        con.close()
        return redirect(url_for("mail_list"))
    sm = _summary_of(row)
    docs = con.execute("SELECT * FROM mail_documents WHERE msg_id=? ORDER BY id",
                       (row["msg_id"],)).fetchall()
    actions = con.execute("SELECT * FROM mail_actions WHERE msg_id=? ORDER BY idx",
                          (row["msg_id"],)).fetchall()
    # Which of the summary's dates already exist as events (so "Add to calendar" is honest).
    ev_dates = {(e["event_date"], e["title"]) for e in con.execute(
        "SELECT event_date, title FROM events WHERE source_ref=?", (row["msg_id"],))}
    con.close()
    return render_template("mail_detail.html", e=row, summary=sm, docs=docs,
                           actions=actions, ev_dates=ev_dates)


@app.route("/mail/doc/<int:did>")
def mail_doc(did):
    """Serve a stored original so a parent can open the actual notice, not just the summary."""
    con = db.connect()
    d = con.execute("SELECT * FROM mail_documents WHERE id=?", (did,)).fetchone()
    con.close()
    if not d or not d["saved_path"] or not Path(d["saved_path"]).exists():
        return "Document not available", 404
    from flask import send_file
    return send_file(d["saved_path"], as_attachment=False, download_name=d["name"])


@app.route("/mail/action", methods=["POST"])
def mail_action():
    """Toggle one action item's state. Records WHO, the shared-board contract."""
    body = request.get_json(silent=True) or {}
    aid = body.get("id")
    action = body.get("action")
    who = (body.get("who") or "").strip()[:40]
    if action not in ("done", "reopen", "dismiss"):
        return jsonify({"error": "bad action"}), 400
    con = db.connect()
    if action == "reopen":
        con.execute("UPDATE mail_actions SET state='open', done_by=NULL, done_at=NULL WHERE id=?",
                    (aid,))
    else:
        con.execute("UPDATE mail_actions SET state=?, done_by=?, done_at=? WHERE id=?",
                    ("done" if action == "done" else "dismissed", who, db.now(), aid))
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.route("/mail/date/add", methods=["POST"])
def mail_date_add():
    """Put one of a summary's dates onto the family board as an event."""
    body = request.get_json(silent=True) or {}
    eid = body.get("mail_id")
    d = (body.get("date") or "").strip()
    title = (body.get("title") or "").strip()
    kind = (body.get("kind") or "info").strip()
    if not (d and title):
        return jsonify({"error": "date and title required"}), 400
    con = db.connect()
    row = con.execute("SELECT msg_id, kid, source FROM emails WHERE id=?", (eid,)).fetchone()
    if not row:
        con.close()
        return jsonify({"error": "unknown email"}), 404
    cur = con.execute(
        "INSERT OR IGNORE INTO events (kid, school, type, title, event_date, category, "
        "source, source_ref, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (row["kid"] or "Both", "", kind, title, d, db.category_for_source(row["source"]),
         row["source"], row["msg_id"], db.now()))
    con.commit()
    con.close()
    return jsonify({"ok": True, "added": bool(cur.rowcount)})


@app.route("/mail/sources", methods=["GET", "POST"])
def mail_sources():
    con = db.connect()
    if request.method == "POST":
        act = request.form.get("action")
        if act == "toggle":
            con.execute("UPDATE sources SET enabled=1-COALESCE(enabled,1) WHERE key=?",
                        (request.form.get("key"),))
        elif act == "approve":
            # Turn a discovered sender into a real, enabled source.
            sender = request.form.get("sender", "")
            kid = request.form.get("kid", "Both")
            key = re.sub(r"[^a-z0-9]+", "_", sender.split("@")[-1].lower())[:40] or "src"
            key = "disc_" + key
            con.execute(
                "INSERT OR IGNORE INTO sources (key, name, kid, sender_match, cadence, "
                "active_months, enabled, keywords, origin) VALUES (?,?,?,?,?,?,1,'[]','discovered')",
                (key, sender, kid, sender, "adhoc", json.dumps(list(range(1, 13)))))
            con.execute("UPDATE mail_suggestions SET status='approved' WHERE sender=?", (sender,))
        elif act == "dismiss":
            con.execute("UPDATE mail_suggestions SET status='dismissed' WHERE sender=?",
                        (request.form.get("sender"),))
        con.commit()
        con.close()
        return redirect(url_for("mail_sources"))
    sources = con.execute("SELECT * FROM sources ORDER BY COALESCE(enabled,1) DESC, name").fetchall()
    suggestions = con.execute(
        "SELECT * FROM mail_suggestions WHERE status='new' ORDER BY score DESC, hits DESC").fetchall()
    con.close()
    return render_template("mail_sources.html", sources=sources, suggestions=suggestions,
                           kids=KIDS)


@app.route("/mail/reprocess", methods=["POST"])
def mail_reprocess():
    """Re-summarize stored mail whose summary is missing/failed (model was down at ingest)."""
    status = _read_status()
    if status.get("running"):
        return jsonify(status), 409
    subprocess.Popen([sys.executable, str(ROOT / "ingest.py"), "--reprocess", "--days", "45"],
                     cwd=str(ROOT), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return jsonify({"running": True, "stage": "re-summarizing stored mail"})


# ---- Notes that never arrive as mail: a class-app post, a paper note, a screenshot ----
# A school messaging app's email is often only a POINTER ("Ms. Park shared a post") -- the
# post itself, with the action items and the specials schedule, exists only inside the
# app, and there's no parent API. So a parent pastes the text, or uploads the screenshot
# they already took, and it goes through exactly the pipeline an email does: stored,
# briefed by the model, action items onto "Needs you", dates onto the board.
NOTE_SOURCE = "pasted"
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
NOTE_STALE_S = 6 * 60   # a briefing "pending" longer than this belongs to a thread that died


def _note_msg_id(kid: str, sent_date: str, text: str) -> str:
    import hashlib
    h = hashlib.sha1(f"{kid}|{sent_date}|{text.strip()}".encode("utf-8")).hexdigest()[:16]
    return f"note:{h}"


def _brief_note_async(msg_id: str):
    """Run the model briefing off the request thread. The page polls until it lands; if the
    model can't run, the deterministic fallback is stored so the note is never left blank."""
    import mailsweep

    def work():
        # Read, then RELEASE the connection before the model call. db.connect() runs the
        # migration UPDATEs, which open a write transaction that stays open until commit or
        # close -- holding it across a 60s model call locked every other request out
        # ("database is locked" on status.json, found in the first local test 2026-09-02).
        con = db.connect()
        try:
            row = con.execute("SELECT * FROM emails WHERE msg_id=?", (msg_id,)).fetchone()
            docs = [dict(d) for d in con.execute(
                "SELECT * FROM mail_documents WHERE msg_id=? ORDER BY id", (msg_id,))]
        finally:
            con.close()
        if not row:
            return
        try:
            summary = mailsweep.summarize(row["subject"] or "", row["body_text"] or "",
                                          docs, row["sent_date"],
                                          date.fromisoformat(row["sent_date"]))
        except Exception as e:  # noqa: BLE001 -- must never leave the row pending
            summary = mailsweep._fallback_summary(row["subject"] or "", row["body_text"] or "",
                                                  docs, date.fromisoformat(row["sent_date"]),
                                                  why=f"briefing raised {type(e).__name__}: {e}")
        con = db.connect()
        try:
            db.save_summary(con, msg_id, summary, row["kid"])
            # The dates the briefing found land on the board the way an email's do (INSERT OR
            # IGNORE against the UNIQUE(kid,title,event_date) key), keyed back to this note.
            for d in summary.get("dates") or []:
                if not (d.get("date") and d.get("title")):
                    continue
                # Same day, another source's wording -> already on the board, don't double it.
                if db.already_on_board(con, row["kid"] or "Both", d["date"], d.get("kind") or "info",
                                       d["title"]):
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO events (kid, school, type, title, event_date, "
                    "category, source, source_ref, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (row["kid"] or "Both", "", d.get("kind") or "info", d["title"], d["date"],
                     db.category_for_source(NOTE_SOURCE), NOTE_SOURCE, msg_id, db.now()))
            con.commit()
        finally:
            con.close()

    threading.Thread(target=work, daemon=True).start()


@app.route("/mail/add", methods=["GET", "POST"])
def mail_add():
    if request.method == "GET":
        return render_template("mail_add.html", kids=KIDS, today=date.today().isoformat(),
                               error=None, form={})
    f = request.form
    kid = f.get("kid") if f.get("kid") in KIDS + ["Both"] else "Both"
    sender = (f.get("sender") or "Pasted note").strip()[:120]
    sent_date = (f.get("sent_date") or "").strip() or date.today().isoformat()
    try:
        date.fromisoformat(sent_date)
    except ValueError:
        sent_date = date.today().isoformat()
    title = (f.get("title") or "").strip()[:200]
    text = (f.get("text") or "").replace("\r\n", "\n").strip()
    image = request.files.get("image")
    docs = []
    problem = None
    if image and image.filename:
        import mailsweep
        data = image.read()
        if len(data) > 12 * 1024 * 1024:
            problem = "That image is over 12 MB - a phone screenshot is usually under 2 MB."
        else:
            key = mailsweep.msg_key("note:" + db.now())
            folder = db.DATA_DIR / "mail" / key
            folder.mkdir(parents=True, exist_ok=True)
            ext = mailsweep._img_ext(data[:16]) or Path(image.filename).suffix or ".png"
            saved = folder / ("01_screenshot" + ext)
            saved.write_bytes(data)
            # Only READ the image when it is the only source of text. A class-app photo
            # arriving with its post's body is an attachment, not a note to transcribe --
            # a 10s model call per photo for nothing.
            if text:
                ocr, why = "", "not read: the note's text was provided"
            else:
                ocr, why = mailsweep.transcribe_image(str(saved))
            docs.append({"origin": "attachment", "name": image.filename or "screenshot",
                         "kind": "image", "url": None, "final_url": None,
                         "saved_path": str(saved), "size": len(data), "text": ocr,
                         "status": "ok" if (ocr or text) else f"failed:{why}"})
            if ocr and not text:
                text = ocr
            elif not ocr and not text:
                problem = f"Couldn't read the screenshot ({why}). Paste the text instead."
    if not text and not problem:
        problem = "Paste the note's text or add a screenshot."
    if problem:
        return render_template("mail_add.html", kids=KIDS, today=date.today().isoformat(),
                               error=problem, form=f), 400
    if not title:
        first = next((ln.strip() for ln in text.split("\n") if ln.strip()), "Note")
        title = first[:120]
    msg_id = _note_msg_id(kid, sent_date, text)
    con = db.connect()
    con.execute(
        "INSERT INTO emails (msg_id, source, sender, subject, sent_date, kid, body_text, "
        "created_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(msg_id) DO UPDATE SET sender=excluded.sender, subject=excluded.subject",
        (msg_id, NOTE_SOURCE, sender, title, sent_date, kid, text[:200000], db.now()))
    if docs:
        db.save_documents(con, msg_id, docs)
    else:
        con.execute("UPDATE emails SET swept_at=? WHERE msg_id=?", (db.now(), msg_id))
    con.execute("UPDATE emails SET summary=?, summarized_at=? WHERE msg_id=?",
                (json.dumps({"_engine": "pending", "headline": title}), db.now(), msg_id))
    con.commit()
    eid = con.execute("SELECT id FROM emails WHERE msg_id=?", (msg_id,)).fetchone()["id"]
    con.close()
    _brief_note_async(msg_id)
    return redirect(url_for("mail_detail", eid=eid))


@app.route("/mail/<int:eid>/status.json")
def mail_status(eid):
    con = db.connect()
    row = con.execute("SELECT summary, summarized_at FROM emails WHERE id=?", (eid,)).fetchone()
    con.close()
    if not row:
        return jsonify({"error": "unknown"}), 404
    sm = _summary_of(row)
    pending = sm.get("_engine") == "pending"
    stale = False
    if pending and row["summarized_at"]:
        try:
            age = (datetime.now() - datetime.fromisoformat(row["summarized_at"])).total_seconds()
            stale = age > NOTE_STALE_S
        except ValueError:
            stale = True
    return jsonify({"ready": not pending, "engine": sm.get("_engine"), "stale": stale})


@app.route("/mail/<int:eid>/rebrief", methods=["POST"])
def mail_rebrief(eid):
    """Run the briefing again for a note whose thread died, or whose model pass fell back."""
    con = db.connect()
    row = con.execute("SELECT msg_id, subject FROM emails WHERE id=?", (eid,)).fetchone()
    if row:
        con.execute("UPDATE emails SET summary=?, summarized_at=? WHERE msg_id=?",
                    (json.dumps({"_engine": "pending", "headline": row["subject"]}), db.now(),
                     row["msg_id"]))
        con.commit()
    con.close()
    if not row:
        return jsonify({"error": "unknown"}), 404
    _brief_note_async(row["msg_id"])
    return jsonify({"ok": True})


# ---- A kid's week at school: specials by weekday + the standing class rules ----

def _routines(con, kid: str) -> dict:
    rows = con.execute("SELECT * FROM kid_routines WHERE kid=? "
                       "ORDER BY CASE WHEN weekday<0 THEN 9 ELSE weekday END, sort, id",
                       (kid,)).fetchall()
    today_wd = date.today().weekday()
    week = [{"weekday": i, "name": WEEKDAYS[i], "is_today": i == today_wd,
             "entries": [r for r in rows if r["weekday"] == i]} for i in range(5)]
    # A weekend entry is unusual but not forbidden -- show the day only when it has one.
    for i in (5, 6):
        ents = [r for r in rows if r["weekday"] == i]
        if ents:
            week.append({"weekday": i, "name": WEEKDAYS[i], "is_today": i == today_wd,
                         "entries": ents})
    standing = [r for r in rows if r["weekday"] == -1]
    return {"week": week, "standing": standing, "n": len(rows)}


def school_today_lines(con, today: date | None = None) -> list[dict]:
    """For the home page: what each kid has at school today and tomorrow -- the sneakers get
    packed the night before, which is why tomorrow is on the line too."""
    today = today or date.today()
    import days_off
    closures = days_off.merge_rows(_out_day_rows(con))
    out = []
    for kid in KIDS:
        rows = con.execute("SELECT * FROM kid_routines WHERE kid=? AND weekday>=0 "
                           "ORDER BY weekday, sort, id", (kid,)).fetchall()
        if not rows:
            continue
        line = {"kid": kid, "today": [], "tomorrow": []}
        tomorrow = today + timedelta(days=1)
        for r in rows:
            if r["weekday"] == today.weekday() and kid not in closures.get(today.isoformat(), {}).get(days_off.FULL, {}):
                line["today"].append(r)
            if r["weekday"] == tomorrow.weekday() and kid not in closures.get(tomorrow.isoformat(), {}).get(days_off.FULL, {}):
                line["tomorrow"].append(r)
        if line["today"] or line["tomorrow"]:
            line["tomorrow_name"] = WEEKDAYS[tomorrow.weekday()]
            out.append(line)
    return out


@app.route("/kid/<kid>/routine", methods=["POST"])
def kid_routine(kid):
    if kid not in KIDS:
        return redirect(url_for("index"))
    f = request.form
    act = f.get("action")
    con = db.connect()
    if act == "delete" and f.get("id"):
        con.execute("DELETE FROM kid_routines WHERE id=? AND kid=?", (f.get("id"), kid))
    else:
        label = (f.get("label") or "").strip()[:80]
        note = (f.get("note") or "").strip()[:300]
        try:
            weekday = int(f.get("weekday", -1))
        except ValueError:
            weekday = -1
        weekday = weekday if -1 <= weekday <= 6 else -1
        who = (f.get("who") or "").strip()[:40]
        source = f"typed on the page{(' by ' + who) if who else ''} {date.today().isoformat()}"
        if label:
            if act == "edit" and f.get("id"):
                con.execute("UPDATE kid_routines SET weekday=?, label=?, note=? WHERE id=? AND kid=?",
                            (weekday, label, note, f.get("id"), kid))
            else:
                con.execute(
                    "INSERT INTO kid_routines (kid, weekday, label, note, source, created_at) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(kid, weekday, label) DO UPDATE SET "
                    "note=excluded.note, source=excluded.source",
                    (kid, weekday, label, note, source, db.now()))
    con.commit()
    con.close()
    return redirect(url_for("kid_page", kid=kid) + "#week")


@app.route("/kid/<kid>/week.xlsx")
def kid_week_xlsx(kid):
    if kid not in KIDS:
        return redirect(url_for("index"))
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    con = db.connect()
    rows = con.execute("SELECT * FROM kid_routines WHERE kid=? "
                       "ORDER BY CASE WHEN weekday<0 THEN 9 ELSE weekday END, sort, id",
                       (kid,)).fetchall()
    con.close()
    cols = ["Kid", "Day", "What", "Note", "Source"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Week at school"
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="C4974C")
        cell.alignment = Alignment(vertical="center")
    for r in rows:
        day = WEEKDAYS[r["weekday"]] if 0 <= (r["weekday"] or -1) <= 6 else "Every day"
        ws.append([r["kid"], day, r["label"], r["note"], r["source"]])
    for i, w in enumerate([8, 12, 28, 60, 44], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{ws.max_row}"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from flask import send_file

    return send_file(buf, as_attachment=True,
                     download_name=f"{kid.lower()}-week-at-school-{date.today().isoformat()}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument."
                              "spreadsheetml.sheet")


@app.route("/checklist/blocked.json")
def checklist_blocked():
    """Every checklist item that is waiting on somebody outside the house.

    A blocked item names who it is waiting on in `blocked_on` — and then nothing ever
    re-checks whether that party answered. Ava's fall swim lessons sat blocked on
    "Waiting on the rec center to reply" while its enrollment-open mail had already
    arrived. The family sweep reads this and matches the awaited party against who has
    since written.
    """
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT id, title, kid, category, due_date, blocked_on, source "
            "FROM checklist WHERE status = 'blocked' ORDER BY due_date IS NULL, due_date"
        ).fetchall()
    finally:
        con.close()
    return jsonify({"blocked": [dict(r) for r in rows], "count": len(rows)})


@app.route("/ingest/captured.json")
def ingest_captured():
    """Which school-mail messages this app already holds, keyed by Message-ID.

    The family sweep scans the same mailbox looking for asks that fell through. Without
    this it re-reports every message Family Manager already ingested and turned into an
    event — 84 rows in a 45-day window, which is a list nobody reads. A check that fires
    on nearly all of its input is worse than no check.

    Message-IDs are normalised to bare lowercase here so both sides agree on the key;
    an id that travels with angle brackets on one side and without on the other matches
    nothing and silently suppresses nothing.
    """
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT e.msg_id, e.source, e.sent_date, "
            "       (SELECT COUNT(*) FROM events v WHERE v.source_ref = e.msg_id) AS n_ev "
            "FROM emails e"
        ).fetchall()
        captured = {}
        for r in rows:
            key = (r["msg_id"] or "").strip().strip("<>").strip().lower()
            if key:
                captured[key] = {"source": r["source"], "events": r["n_ev"],
                                 "sent_date": r["sent_date"]}
    finally:
        con.close()
    status = _read_status()
    return jsonify({
        "captured": captured,
        "count": len(captured),
        "last_ingest": status.get("finished") or status.get("updated"),
        "last_ingest_ok": status.get("ok"),
    })


def _read_status():
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"running": False, "stage": None, "summary": None}


# ---- Sitters: who we call, and the sheet they need -------------------------------------
@app.route("/sitters")
def sitters_page():
    import sitters

    con = db.connect()
    sitters.seed_info(con)
    roster = sitters.roster(con)
    sections = sitters.info_sections(con)
    booked = sitters.booked_days(con, date.today().isoformat())
    con.close()
    filled = sum(s["filled"] for s in sections)
    total = sum(len(s["rows"]) for s in sections)
    return render_template("sitters.html", roster=roster, sections=sections, booked=booked,
                           kids_ok=sitters.KIDS_OK, filled=filled, total=total,
                           section_names=[s for s, _ in sitters.SECTIONS],
                           active_n=sum(1 for r in roster if r["active"]))


@app.route("/sitters/sheet")
def sitters_sheet():
    """The printable version: what you leave on the counter. Blank rows are left OUT --
    a printed sheet that says 'Allergies: ' with nothing after it reads as 'none'."""
    import sitters

    con = db.connect()
    sitters.seed_info(con)
    sections = sitters.info_sections(con)
    con.close()
    for sec in sections:
        sec["rows"] = [r for r in sec["rows"] if (r["value"] or "").strip()]
    sections = [s for s in sections if s["rows"]]
    return render_template("sitters_sheet.html", sections=sections)


@app.route("/sitters/save", methods=["POST"])
def sitters_save():
    import sitters

    d = request.get_json(silent=True) or request.form.to_dict()
    who = (d.get("who") or "").strip()[:20] or None
    sid = d.get("id")
    con = db.connect()
    try:
        row = sitters.save_sitter(con, d, who, sitter_id=int(sid) if sid else None)
    except ValueError as exc:
        con.close()
        if request.is_json:
            return jsonify({"error": str(exc)}), 400
        return redirect("/sitters?error=" + str(exc))
    con.close()
    if request.is_json:
        return jsonify(row)
    return redirect("/sitters#s-" + str(row["id"]))


@app.route("/sitters/<int:sid>/move", methods=["POST"])
def sitters_move(sid):
    import sitters

    d = request.get_json(silent=True) or {}
    con = db.connect()
    sitters.move_sitter(con, sid, -1 if d.get("direction") == "up" else 1)
    con.close()
    return jsonify({"ok": True})


@app.route("/sitters/<int:sid>/delete", methods=["POST"])
def sitters_delete(sid):
    import sitters

    con = db.connect()
    n = sitters.delete_sitter(con, sid)
    con.close()
    return jsonify({"deleted": n})


@app.route("/sitters/info", methods=["POST"])
def sitters_info():
    import sitters

    d = request.get_json(silent=True) or {}
    who = (d.get("who") or "").strip()[:20] or None
    con = db.connect()
    try:
        if d.get("id"):
            row = sitters.set_info(con, int(d["id"]), d.get("value") or "", who)
            if not row:
                return jsonify({"error": "no such row"}), 404
        else:
            row = sitters.add_info_row(con, d.get("section") or "", d.get("label") or "", who)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        con.close()
    return jsonify(row)


@app.route("/sitters.xlsx")
def sitters_xlsx():
    import sitters
    from flask import send_file

    con = db.connect()
    data = sitters.workbook_bytes(con, date.today().isoformat())
    con.close()
    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name=f"sitters-{date.today().isoformat()}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---- Scan everything: the five feeds, on demand, with what came in ----------------------
@app.route("/scan")
def scan_page():
    import scan_all

    return render_template("scan.html", status=scan_all.read_status(),
                           steps=[(n, l) for n, l, _, _ in scan_all.STEPS])


@app.route("/scan/run", methods=["POST"])
def scan_run():
    import scan_all

    d = request.get_json(silent=True) or {}
    only = d.get("only") or request.form.getlist("only") or None
    if only:
        bad = [x for x in only if x not in scan_all.STEP_NAMES]
        if bad:
            return jsonify({"error": "unknown feed: " + ", ".join(bad)}), 400
    busy = scan_all.already_busy()
    if busy:
        if request.is_json:
            return jsonify({"error": busy}), 409
        return redirect("/scan")
    args = [sys.executable, str(ROOT / "scan_all.py")]
    if only:
        args += ["--only", ",".join(only)]
    subprocess.Popen(args, cwd=str(ROOT), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if request.is_json:
        return jsonify({"running": True, "only": only})
    return redirect("/scan")


@app.route("/scan/status.json")
def scan_status():
    import scan_all

    return jsonify(scan_all.read_status())


# ---- Trips: every trip, the next one first ------------------------------------------------
def _slugify(name: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "trip"


@app.route("/trips")
def trips_page():
    con = db.connect()
    today = date.today()
    rows = con.execute("SELECT * FROM trips ORDER BY start_date").fetchall()
    out = []
    for t in rows:
        d = dict(t)
        d["n_items"] = con.execute("SELECT COUNT(*) FROM trip_items WHERE trip=? AND COALESCE(plan,'') != 'dropped'",
                                   (t["slug"],)).fetchone()[0]
        d["n_todo"] = con.execute("SELECT COUNT(*) FROM trip_items WHERE trip=? AND booking='todo' "
                                  "AND COALESCE(plan,'') != 'dropped'", (t["slug"],)).fetchone()[0]
        d["n_expenses"] = con.execute("SELECT COUNT(*) FROM trip_expenses WHERE trip=?",
                                      (t["slug"],)).fetchone()[0]
        try:
            sd = datetime.strptime(t["start_date"], "%Y-%m-%d").date()
            ed = datetime.strptime(t["end_date"], "%Y-%m-%d").date()
            # %-d is not a Windows strftime flag; strip the zero by hand.
            d["range"] = f"{sd.strftime('%b')} {sd.day} – {ed.strftime('%b')} {ed.day}, {ed.year}"
            d["days_until"] = (sd - today).days if ed >= today else None
        except (TypeError, ValueError):
            d["range"] = f"{t['start_date']} – {t['end_date']}"
            d["days_until"] = None
        out.append(d)
    con.close()
    upcoming = [t for t in out if (t["end_date"] or "") >= today.isoformat()]
    past = [t for t in out if (t["end_date"] or "") < today.isoformat()][::-1]
    return render_template("trips.html", upcoming=upcoming, past=past)


@app.route("/trips/new", methods=["POST"])
def trips_new():
    f = request.form
    name = (f.get("name") or "").strip()[:80]
    start, end = (f.get("start_date") or "").strip(), (f.get("end_date") or "").strip()
    if not name or not start or not end:
        return redirect("/trips?error=name, first day and last day are required")
    if end < start:
        return redirect("/trips?error=the last day is before the first day")
    slug = f"{_slugify(name)}-{start[:7]}"
    con = db.connect()
    if con.execute("SELECT 1 FROM trips WHERE slug=?", (slug,)).fetchone():
        con.close()
        return redirect(f"/trip/{slug}")
    con.execute("INSERT INTO trips (slug, name, destination, start_date, end_date, party, notes) "
                "VALUES (?,?,?,?,?,?,?)",
                (slug, name, (f.get("destination") or "").strip()[:120], start, end,
                 (f.get("party") or "").strip()[:120], (f.get("notes") or "").strip()[:600]))
    con.commit()
    con.close()
    return redirect(f"/trip/{slug}")


# ---------------------------------------------------------------- gatherings (parties, visits)
#
# One shared plan for anything the house hosts. The kid birthdays are the recurring case;
# a family visit or a New Year's thing uses the same page with a different starter plan.
# Who is editing = the fm_who picker, as everywhere else.

def _g_who():
    who = (request.values.get("who") or "").strip()[:20]
    if not who and request.is_json:
        who = ((request.get_json(silent=True) or {}).get("who") or "").strip()[:20]
    return who or None


@app.route("/gatherings")
def gatherings_page():
    import gatherings
    con = db.connect()
    lst = gatherings.listing(con, date.today())
    con.close()
    return render_template("gatherings.html", kinds=gatherings.KINDS, kids=KIDS,
                           birthdays=gatherings.KID_BIRTHDAYS, this_year=date.today().year, **lst)


@app.route("/gatherings/new", methods=["POST"])
def gatherings_new():
    import gatherings
    f = request.form
    kind = f.get("kind") or "party"
    data = {k: f.get(k) for k in gatherings.G_FIELDS}
    data["budget"] = f.get("budget")
    # A birthday party is anchored on the birthday; the form only asks which kid and year.
    if kind == "kid_birthday" and data.get("honoree") in gatherings.KID_BIRTHDAYS and not data.get("occasion_date"):
        yr = (f.get("year") or str(date.today().year)).strip()[:4]
        data["occasion_date"] = f"{yr}-{gatherings.KID_BIRTHDAYS[data['honoree']]}"
        if not data.get("name"):
            data["name"] = data["honoree"] + "'s birthday party"
    if kind != "kid_birthday":
        data["honoree"] = ""
    con = db.connect()
    try:
        g = gatherings.create(con, data, _g_who())
    except ValueError as exc:
        con.close()
        return redirect("/gatherings?error=" + str(exc))
    con.close()
    return redirect(f"/gathering/{g['slug']}")


@app.route("/gathering/<slug>")
def gathering_page(slug):
    import gatherings
    con = db.connect()
    g = gatherings.get(con, slug)
    if not g:
        con.close()
        return redirect("/gatherings?error=no such gathering")
    today = date.today()
    s = gatherings.summary(con, g, today)
    groups = gatherings.plan(con, slug, g["kind"])
    guests = gatherings.guests(con, slug)
    workstreams = [x["workstream"] for x in groups]
    for w in gatherings.WORKSTREAM_ORDER.get(g["kind"], []):
        if w not in workstreams:
            workstreams.append(w)
    origin = gatherings.get(con, g["cloned_from"]) if g.get("cloned_from") else None
    con.close()
    return render_template("gathering.html", g=g, s=s, groups=groups, guests=guests, workstreams=workstreams,
                           kinds=gatherings.KINDS, statuses=gatherings.STATUSES,
                           status_labels=gatherings.STATUS_LABELS, rsvps=gatherings.RSVPS,
                           rsvp_labels=gatherings.RSVP_LABELS, kids=KIDS, origin=origin, today=today.isoformat())


@app.route("/gathering/<slug>/update", methods=["POST"])
def gathering_update(slug):
    """Header, the post-mortem, status. Form or JSON; only the keys sent change."""
    import gatherings
    data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    data = {k: v for k, v in (data or {}).items() if k != "who"}
    con = db.connect()
    try:
        g = gatherings.update(con, slug, data, _g_who())
    except ValueError as exc:
        con.close()
        if request.is_json:
            return jsonify({"error": str(exc)}), 400
        return redirect(f"/gathering/{slug}?error={exc}")
    con.close()
    if request.is_json:
        return jsonify({"ok": True, "gathering": g})
    return redirect(f"/gathering/{slug}" + ("#after" if "went_well" in data or "status" in data else ""))


@app.route("/gathering/<slug>/delete", methods=["POST"])
def gathering_delete(slug):
    import gatherings
    con = db.connect()
    gatherings.delete(con, slug)
    con.close()
    return jsonify({"ok": True}) if request.is_json else redirect("/gatherings")


@app.route("/gathering/<slug>/clone", methods=["POST"])
def gathering_clone(slug):
    import gatherings
    con = db.connect()
    try:
        g = gatherings.clone(con, slug, _g_who())
    except ValueError as exc:
        con.close()
        return redirect(f"/gathering/{slug}?error={exc}")
    con.close()
    return redirect(f"/gathering/{g['slug']}")


@app.route("/gathering/<slug>/item", methods=["POST"])
def gathering_item(slug):
    """JSON. {id, ...fields} edits; {workstream, title} adds; {id, delete:true} removes."""
    import gatherings
    d = request.get_json(silent=True) or {}
    who = _g_who()
    con = db.connect()
    try:
        if d.get("id") and d.get("delete"):
            gatherings.delete_item(con, int(d["id"]))
            row = None
        elif d.get("id"):
            row = gatherings.set_item(con, int(d["id"]), d, who)
        else:
            row = gatherings.add_item(con, slug, d.get("workstream") or "", d.get("title") or "", who,
                                      owner=d.get("owner") or None, due=d.get("due") or None)
        g = gatherings.get(con, slug)
        s = gatherings.summary(con, g, date.today()) if g else {}
    except ValueError as exc:
        con.close()
        return jsonify({"error": str(exc)}), 400
    con.close()
    return jsonify({"ok": True, "item": row, "summary": s})


@app.route("/gathering/<slug>/guest", methods=["POST"])
def gathering_guest(slug):
    """JSON. {id, ...fields} edits; {name, ...} adds; {paste: text} adds many;
    {id, delete:true} removes; {invited_all:true} flips every not-asked row."""
    import gatherings
    d = request.get_json(silent=True) or {}
    who = _g_who()
    con = db.connect()
    try:
        out = {}
        if d.get("id") and d.get("delete"):
            gatherings.delete_guest(con, int(d["id"]))
        elif d.get("id"):
            out["guest"] = gatherings.set_guest(con, int(d["id"]), d, who)
        elif d.get("paste") is not None:
            out["added"] = gatherings.add_guests_from_text(con, slug, d.get("paste") or "", who,
                                                           adults=int(d.get("adults") or 0), kids=int(d.get("kids") or 1))
        elif d.get("invited_all"):
            out["flipped"] = gatherings.mark_all_invited(con, slug, who)
        else:
            out["guest"] = gatherings.add_guest(con, slug, d, who)
        g = gatherings.get(con, slug)
        out["summary"] = gatherings.summary(con, g, date.today()) if g else {}
    except (ValueError, TypeError) as exc:
        con.close()
        return jsonify({"error": str(exc)}), 400
    con.close()
    return jsonify({"ok": True, **out})


@app.route("/gathering/<slug>.xlsx")
def gathering_xlsx(slug):
    import gatherings
    from flask import send_file
    con = db.connect()
    if not gatherings.get(con, slug):
        con.close()
        return redirect("/gatherings")
    data = gatherings.workbook_bytes(con, slug)
    con.close()
    return send_file(io.BytesIO(data), as_attachment=True, download_name=f"{slug}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/gatherings.xlsx")
def gatherings_xlsx():
    import gatherings
    from flask import send_file
    con = db.connect()
    data = gatherings.index_workbook_bytes(con, date.today())
    con.close()
    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name=f"gatherings-{date.today().isoformat()}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- trips
#
# The trip page is a WORKSPACE, not a report. Three things it has to do that a list of
# confirmations cannot: hold candidates nobody has committed to, let a plan carry a time
# (or deliberately not carry one), and end with a day sheet that says only what you are
# actually doing.

TRIP_KIND_LABELS = {
    "flight": "Flight", "stay": "Stay", "transport": "Transport",
    "activity": "Activity", "food": "Food", "admin": "Admin", "money": "Money",
}
TRIP_PLAN_LABELS = {"idea": "Idea", "penciled": "Penciled in",
                    "doing": "Doing it", "dropped": "Dropped"}
TRIP_BOOKING_LABELS = {"none": "Nothing to book", "todo": "To book", "booked": "Booked"}

# Minutes between two coarse areas. Only ever asked one question -- "did you leave time
# for the drive" -- and a few buckets answer it as well as a street address would. A
# household that plans by area lists the minutes in household.json as
# "trip_travel_minutes": [["a", "b", 20], ...]; a pair it doesn't list assumes 20.
_TRAVEL = {(a, b): int(m) for a, b, m in (family.CONFIG.get("trip_travel_minutes") or [])}
# A flight or a drive IS the getting-there. Charging travel time on either side of one
# double-counts it and reports the airport run as a missed connection.
_TRANSIT_KINDS = ("flight", "transport")


def _travel_min(a, b):
    if not a or not b or a == b:
        return 0
    return _TRAVEL.get((a, b)) or _TRAVEL.get((b, a)) or 20


def _mins(t):
    """HH:MM -> minutes past midnight. None for anything unparseable, never 0 -- a time
    nobody set and midnight are different facts."""
    if not t or ":" not in t:
        return None
    try:
        h, m = t.split(":")[:2]
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def _hhmm(m):
    return "%02d:%02d" % ((m // 60) % 24, m % 60)


def _fmt_clock(t):
    """4:30pm, not 16:30. Same rule as the |clock filter, callable from python."""
    m = _mins(t)
    if m is None:
        return ""
    h, mm = divmod(m, 60)
    ap = "pm" if h >= 12 else "am"
    h = h % 12 or 12
    return f"{h}{ap}" if mm == 0 else f"{h}:{mm:02d}{ap}"


def _sort_key(r):
    """Where a row sits in its day. An untimed row is not last -- it sorts to its part of
    the day, half a step after anything genuinely clocked at that hour."""
    t = _mins(r["start_time"])
    if t is not None:
        return t
    anchor = db.PART_ANCHOR.get(r["part"])
    return (anchor + 0.5) if anchor is not None else 24 * 60


def _item_when_sig(r):
    """What was sent to the calendar. Lets a day tell 'on Google' from 'on Google, but
    this has moved since'."""
    return "~".join(str(r[k] or "") for k in ("day", "start_time", "part",
                                              "duration_min", "title"))


def _analyze_day(entries):
    """The part that makes it a workspace: what the day costs in hours and dollars, what
    overlaps, and where nobody left time for the drive."""
    timed = [r for r in entries if _mins(r["start_time"]) is not None]
    flags, gaps = [], {}
    first = last = None
    out = cost = 0.0
    unpriced = 0

    loose_min = 0
    for r in entries:
        if r["cost_est"] is None:
            unpriced += 1
        else:
            cost += float(r["cost_est"])
        # Timed and untimed work are different claims. Summing them made a day report
        # more hours "doing" than it was out of the house, which reads as a bug.
        if _mins(r["start_time"]) is not None:
            out += (r["duration_min"] or 0)
        else:
            loose_min += (r["duration_min"] or 0)

    for n, r in enumerate(timed):
        st = _mins(r["start_time"])
        en = st + (r["duration_min"] or 60)
        first = st if first is None else min(first, st)
        last = en if last is None else max(last, en)
        if n + 1 >= len(timed):
            continue
        nx = timed[n + 1]
        gap = _mins(nx["start_time"]) - en
        need = 0 if (r["kind"] in _TRANSIT_KINDS or nx["kind"] in _TRANSIT_KINDS) \
            else _travel_min(r["area"], nx["area"])
        if gap < 0:
            flags.append({"kind": "err", "text":
                          f"{nx['title']} starts at {_fmt_clock(nx['start_time'])}, "
                          f"before {r['title']} is done at {_fmt_clock(_hhmm(en))}."})
        elif gap < need:
            flags.append({"kind": "warn", "text":
                          f"Only {gap} min between {r['title']} and {nx['title']} — "
                          f"{db.TRIP_AREAS.get(r['area'], 'there')} to "
                          f"{db.TRIP_AREAS.get(nx['area'], 'there')} is about {need} min "
                          f"of driving."})
        elif gap > 0:
            gaps[nx["id"]] = {"m": gap, "need": need}

    # A day that starts before 9:30 and ends after 21:30 is a long one at three years old.
    # Stated, not enforced -- it is his call, and the page's job is to make it visible.
    if last is not None and first is not None and last > 21 * 60 + 30 and first < 9 * 60 + 30:
        flags.append({"kind": "warn", "text":
                      f"Out from {_fmt_clock(_hhmm(first))} to {_fmt_clock(_hhmm(last))} "
                      f"— a long day to end that late with a three-year-old."})
    if not entries:
        flags.append({"kind": "calm", "text":
                      "Nothing planned. Add something, pencil an idea in, or leave it as "
                      "the day you don't plan."})

    doing = [r for r in entries if r["plan"] == "doing"]
    if not doing:
        gstate = "empty"
    elif all(r["gcal_event_id"] for r in doing):
        gstate = "stale" if any((r["gcal_sig"] or "") != _item_when_sig(r)
                                for r in doing) else "sent"
    elif any(r["gcal_event_id"] for r in doing):
        gstate = "part"
    else:
        gstate = "none"

    return {"flags": flags, "gaps": gaps, "out": int(out), "loose_min": int(loose_min),
            "cost": cost,
            "unpriced": unpriced, "gcal": gstate,
            "span": (last - first) if (first is not None and last is not None) else 0}


def _current_trip(con, slug=None):
    """The trip to show. Default is the next one that has not ended yet -- a page that
    defaults to whatever was seeded first would show last year's trip in a year."""
    if slug:
        return con.execute("SELECT * FROM trips WHERE slug=?", (slug,)).fetchone()
    today = date.today().isoformat()
    row = con.execute("SELECT * FROM trips WHERE end_date >= ? ORDER BY start_date "
                      "LIMIT 1", (today,)).fetchone()
    return row or con.execute("SELECT * FROM trips ORDER BY start_date DESC "
                              "LIMIT 1").fetchone()


def _trip_days(trip, rows):
    """One block per calendar day, each already analyzed. A stay that spans nights shows
    on its first day only -- repeating it on all four turns the timeline into wallpaper."""
    start = datetime.strptime(trip["start_date"], "%Y-%m-%d").date()
    end = datetime.strptime(trip["end_date"], "%Y-%m-%d").date()
    days = []
    d = start
    while d <= end:
        key = d.isoformat()
        entries = sorted([r for r in rows if r["day"] == key], key=_sort_key)
        days.append({"day": key,
                     "label": d.strftime("%A"),
                     "date_label": d.strftime("%b %-d") if os.name != "nt"
                                   else d.strftime("%b %#d"),
                     "entries": entries,
                     "a": _analyze_day(entries)})
        d += timedelta(days=1)
    return days


def _trip_media(slug):
    """Hero media resolved by convention, never by code: drop files into
    static/trips/<slug>/ and the pages pick them up. hero.mp4 is a quiet muted loop,
    hero.jpg is its poster and the no-motion fallback, banner.jpg dresses the home-page
    banner. Any of them may be missing -- the pages degrade to the plain layout -- so
    the NEXT trip needs a folder of pictures, not a template change."""
    if not slug:
        return None
    base = os.path.join(app.static_folder, "trips", slug)
    media = {}
    for key, fname in (("video", "hero.mp4"), ("poster", "hero.jpg"),
                       ("banner", "banner.jpg")):
        if os.path.exists(os.path.join(base, fname)):
            media[key] = url_for("static", filename=f"trips/{slug}/{fname}")
    return media or None


def _trip_date_range(trip):
    """'Mon, Aug 24 - Fri, Aug 28' -- weekday-first, because 'is that the Monday or
    the Tuesday' is the question a parent actually asks of a date."""
    a = datetime.strptime(trip["start_date"], "%Y-%m-%d").date()
    b = datetime.strptime(trip["end_date"], "%Y-%m-%d").date()
    day = "%a, %b %-d" if os.name != "nt" else "%a, %b %#d"
    return f"{a.strftime(day)} – {b.strftime(day)}"


def _base_row(rows):
    """The one row everything is measured FROM. Explicit -- seed_trip.mark_base sets it.

    Returns None rather than falling back to "the first row that has coordinates": a
    distance measured from an arbitrary origin is wrong in a way nobody reading the page
    could ever detect, which makes it worse than no distance at all.
    """
    for r in rows:
        if r["is_base"] and r["lat"] is not None and r["geo_status"] == geo.OK:
            return r
    return None


def _located(rows):
    return [r for r in rows if r["lat"] is not None and r["geo_status"] == geo.OK]


def _trip_country(trip):
    """The country the driving happens in, taken from the trip's own destination.

    Not a constant and not a distance threshold -- a threshold here would be exactly the
    "plausible default" that hides the thing it is meant to catch.
    """
    dest = (trip["destination"] or "").strip()
    return dest.split(",")[-1].strip() if "," in dest else None


def _drivable(row, country):
    """Is this somewhere you get to BY CAR on this trip?

    Two things are not, and both were being routed as drives until the rendered page
    gave it away: a flight is not a drive, and neither is anything on the far side of
    one. Monday's total read 19 hr 25 min of driving -- the home airport and back by road --
    which is not merely wrong, it is the kind of wrong that makes a reader stop
    believing the other numbers on the page.

    Decided on the geocoder's OWN answer for which country a place is in, rather than a
    distance cutoff: the destination airport is 121 km away and genuinely driven, the home airport is
    624 km away and genuinely flown, and no threshold expresses that difference honestly.
    """
    if row["kind"] == "flight":
        return False
    if country and row["geo_label"]:
        return row["geo_label"].strip().rstrip(".").endswith(country)
    return True


def _distance_note(row):
    """Why a row has no distance, in words. Never a silent blank, and never a zero.

    Each of these is a genuinely different state and the page has to say which: an
    address the geocoder could not find is a typo to fix, an address that was never
    written down is a row to fill in, and a lookup that failed is one to retry.
    """
    st = row["geo_status"]
    if st == geo.OK:
        return None
    if st == geo.NOADDRESS or not row["location"]:
        return "no address on this item"
    if st == geo.NOHIT:
        return "address not found on the map"
    if st == geo.ERROR:
        return "could not look it up"
    return "not looked up yet"


def _trip_distances(con, rows, country=None):
    """Drive time from base for every row, and the per-day leg-by-leg run.

    Cache-only (live=False) on purpose: a page render must never wait on somebody
    else's server. Anything not yet cached comes back None and renders as "not
    measured" -- the honest answer, and the prompt to press Refresh.
    """
    base = _base_row(rows)
    from_base, legs_by_day = {}, {}
    if base is None:
        return None, from_base, legs_by_day
    bpt = (base["lat"], base["lng"])
    for r in _located(rows):
        if r["id"] == base["id"]:
            from_base[r["id"]] = {"meters": 0.0, "seconds": 0.0, "source": "same"}
            continue
        if not _drivable(r, country):
            continue
        from_base[r["id"]] = geo.drive(con, bpt, (r["lat"], r["lng"]), live=False)

    by_day = {}
    for r in rows:
        if r["day"] and r["plan"] in ("doing", "penciled"):
            by_day.setdefault(r["day"], []).append(r)
    for day, items in by_day.items():
        stops = [r for r in items
                 if r["lat"] is not None and r["geo_status"] == geo.OK
                 and r["id"] != base["id"] and _drivable(r, country)]
        flown = [r for r in items if not _drivable(r, country)
                 and r["lat"] is not None and r["geo_status"] == geo.OK]
        legs, total, unmeasured = [], 0.0, 0
        prev, prev_name = bpt, "the base"
        for r in stops:
            d = geo.drive(con, prev, (r["lat"], r["lng"]), live=False)
            legs.append({"from": prev_name, "to": r["title"], "drive": d})
            if d:
                total += d["seconds"]
            else:
                unmeasured += 1
            prev, prev_name = (r["lat"], r["lng"]), r["title"]
        if stops:
            d = geo.drive(con, prev, bpt, live=False)
            legs.append({"from": prev_name, "to": "the base", "drive": d})
            if d:
                total += d["seconds"]
            else:
                unmeasured += 1
        legs_by_day[day] = {"legs": legs,
                            "total_seconds": total if legs else None,
                            "unmeasured": unmeasured,
                            # Named on the page, never silently dropped: a day that
                            # leaves out its flights has to say that it did.
                            "flown": [r["title"] for r in flown]}
    return base, from_base, legs_by_day


# The templates have to be able to say WHY a row has no distance, in the same words the
# logistics page uses. Registered once rather than passed to every render, so the two
# pages cannot drift apart on it.
app.jinja_env.globals["distance_note"] = _distance_note


@app.route("/trip/<slug>/logistics")
def trip_logistics(slug):
    """How far everything is from the base, and from each other.

    The question the itinerary could never answer: two items half an hour apart on the
    page can be four minutes apart on the ground or thirty-five, and that is what
    actually decides whether a day works.
    """
    con = db.connect()
    trip = _current_trip(con, slug)
    if not trip:
        con.close()
        return redirect(url_for("trip_page"))
    rows = con.execute("SELECT * FROM trip_items WHERE trip=? AND plan<>'dropped' "
                       "ORDER BY day IS NULL, day, sort, id",
                       (trip["slug"],)).fetchall()
    country = _trip_country(trip)
    base, from_base, legs_by_day = _trip_distances(con, rows, country)

    # One entry per PLACE, not per row: the hotel appears three times on this trip and
    # a matrix with three identical hotel columns is a matrix nobody reads.
    seen, places = set(), []
    for r in _located(rows):
        if not _drivable(r, country):
            continue
        k = geo.coord_key(r["lat"], r["lng"])
        if k in seen:
            continue
        seen.add(k)
        places.append(r)
    matrix = []
    for a in places:
        cells = []
        for b in places:
            # A place to itself is a KNOWN zero, not a failed lookup. Passing None here
            # made the whole diagonal render "not measured", which reads as 21 broken
            # cells down the middle of an otherwise complete table.
            cells.append({"meters": 0.0, "seconds": 0.0, "source": "same"}
                         if a["id"] == b["id"] else
                         geo.drive(con, (a["lat"], a["lng"]), (b["lat"], b["lng"]),
                                   live=False))
        matrix.append({"row": a, "cells": cells})

    # Named, never hidden: a matrix that quietly omits the rows it could not place
    # reads as a complete picture of the trip.
    unplaced = [{"row": r, "why": _distance_note(r)} for r in rows
                if r["geo_status"] != geo.OK]
    # Placed fine, deliberately excluded from every driving number on the page.
    not_driven = [r for r in _located(rows) if not _drivable(r, country)]
    con.close()
    return render_template("trip_logistics.html", trip=trip, base=base,
                           from_base=from_base, legs_by_day=legs_by_day,
                           places=places, matrix=matrix, unplaced=unplaced,
                           not_driven=not_driven, country=country,
                           rows=rows, geo=geo,
                           date_range=_trip_date_range(trip))


@app.route("/trip/<slug>/geo", methods=["POST"])
def trip_geo_refresh(slug):
    """Look up anything not yet located, then warm every driving leg.

    Threaded because Nominatim's published rate limit is one request a second, and
    thirty rows is half a minute of a page appearing to do nothing.
    """
    def work():
        try:
            geo.geocode_trip(slug, live=True, verbose=True)
            geo.warm_routes(slug, live=True, verbose=True)
        except Exception as exc:                              # noqa: BLE001
            print("geo refresh failed: %s" % exc, file=sys.stderr)
    threading.Thread(target=work, daemon=True).start()
    return redirect(url_for("trip_logistics", slug=slug))


def _weather_for_days(con, slug, days, shelf):
    """Attach the forecast to each day, and say what it MEANS for that day's plan.

    The forecast on its own changes nothing. What changes a decision is the pairing:
    "Monday is 54% rain and 61F, and the only thing on it is outdoors". That sentence
    needs the forecast, the shelter classification, and the day's actual contents, which
    is why this is computed here rather than left to three separate template loops.

    Cache-only: the render never waits on the network. A day with nothing stored says
    so -- it is never drawn as a fine day.
    """
    # The wet-weather bench: indoor candidates still sitting on the shelf. Offered only
    # when a day actually needs one, so it does not become permanent furniture.
    bench = [r for r in shelf if r["shelter"] == "indoor" and r["plan"] != "dropped"]
    issued = None
    for d in days:
        f = weather.day_forecast(con, slug, d["day"])
        d["wx"] = f
        if f and f["fetched_at"] and (issued is None or f["fetched_at"] > issued):
            issued = f["fetched_at"]
        entries = d["entries"]
        outdoor = [r for r in entries if r["shelter"] == "outdoor"]
        d["outdoor"] = outdoor
        d["wx_note"] = None
        d["wx_bench"] = []
        if not f:
            continue
        # Per-item chance at the hour it actually happens. The DAY figure cannot tell
        # you whether the 8pm walk is in the wet part of a 54% day.
        d["wx_hourly"] = {r["id"]: weather.hour_pop(con, slug, d["day"], r["start_time"])
                          for r in entries if r["start_time"]}
        if f["wet"] and outdoor:
            d["wx_note"] = (f"{f['rain_pct']}% chance of rain and "
                            f"{len(outdoor)} outdoor thing{'' if len(outdoor) == 1 else 's'} "
                            f"on this day.")
            d["wx_bench"] = bench[:4]
        elif f["wet"]:
            d["wx_note"] = (f"{f['rain_pct']}% chance of rain, but nothing outdoors is "
                            "planned -- this day is already weatherproof.")
        elif f["iffy"] and outdoor:
            d["wx_note"] = (f"{f['rain_pct']}% chance of rain. Worth having the indoor "
                            "fallback in your pocket.")
        if f["cold"]:
            cold = f"High of only {round(f['temp_max'])}\u00b0F -- jackets, not August clothes."
            d["wx_note"] = (d["wx_note"] + " " + cold) if d["wx_note"] else cold
    return {"issued": issued, "age": weather.fmt_age(issued) if issued else None,
            "bench": bench}


@app.route("/trip/<slug>/weather", methods=["POST"])
def trip_weather_refresh(slug):
    """Re-pull the forecast now. Cheap, no key, and it is meant to change -- unlike the
    geocode refresh this is a button somebody may press twice in a day."""
    try:
        weather.refresh_trip(slug, force=True, verbose=False)
    except Exception as exc:                                  # noqa: BLE001
        print("weather refresh failed: %s" % exc, file=sys.stderr)
    return _back(slug)


@app.route("/trip")
@app.route("/trip/<slug>")
def trip_page(slug=None):
    """Everything about one trip on one page, in two views: the workspace where you build
    the days, and the day sheet that says only what you are actually doing."""
    con = db.connect()
    trip = _current_trip(con, slug)
    if not trip:
        con.close()
        return render_template("trip.html", trip=None, days=[], shelf=[], loose=[],
                               dropped=[], owed=[], totals={}, countdown=None,
                               view="plan", media=None, date_range=None,
                               base_row=None, from_base={}, legs_by_day={}, geo=geo,
                               wx={"issued": None, "age": None, "bench": []},
                               weather=weather, shelters=db.TRIP_SHELTERS)
    rows = con.execute("SELECT * FROM trip_items WHERE trip=? ORDER BY day IS NULL, "
                       "day, sort, id", (trip["slug"],)).fetchall()
    con.close()

    view = "sheet" if request.args.get("view") == "sheet" else "plan"
    # The shelf holds CANDIDATES -- plan='idea' -- wherever they came from, and whether or
    # not the seed suggested a day for them. Splitting on "has no day" instead would put
    # "bring both car seats" on a shelf of things you might do, which empties the shelf of
    # meaning. A seeded idea keeps its suggested time so pencilling it in pre-fills it.
    shelf = [r for r in rows if r["plan"] == "idea"]
    # Settled, but not tied to one day: the car seats, the card to pay with.
    loose = [r for r in rows if not r["day"] and r["plan"] in ("penciled", "doing")]
    dropped = [r for r in rows if r["plan"] == "dropped"]
    days = _trip_days(trip, [r for r in rows
                             if r["plan"] not in ("dropped", "idea")])
    # Above the timeline: anything owed a booking, soonest deadline first. Two of these
    # fall due BEFORE the trip starts, so a reader who only scrolls the days meets them
    # too late.
    owed = sorted([r for r in rows if r["booking"] == "todo" and r["plan"] != "dropped"],
                  key=lambda r: (r["deadline"] or "9999", r["day"] or "9999"))

    live = [r for r in rows if r["plan"] in ("doing", "penciled")]
    totals = {
        # NOT "items" -- Jinja resolves totals.items to the dict method and renders
        # "<built-in method items of dict object at 0x...>" on the page.
        "count": len(live),
        "doing": sum(1 for r in rows if r["plan"] == "doing"),
        "penciled": sum(1 for r in rows if r["plan"] == "penciled"),
        "shelf": len(shelf),
        "owed": len(owed),
        "cost": sum(float(r["cost_est"]) for r in live if r["cost_est"] is not None),
        # Named, never folded into the number above. A total that quietly absorbs the
        # things nobody priced is a total that lies in the flattering direction.
        "unpriced": sum(1 for r in live if r["cost_est"] is None),
    }

    today = date.today()
    start = datetime.strptime(trip["start_date"], "%Y-%m-%d").date()
    end = datetime.strptime(trip["end_date"], "%Y-%m-%d").date()
    if today < start:
        countdown = {"n": (start - today).days, "word": "days to go"}
    elif today <= end:
        countdown = {"n": (today - start).days + 1, "word": "day of the trip"}
    else:
        countdown = None

    # The date the option prices were researched. Attraction rates move by date, so a price
    # with no as-of on it is a price nobody can act on with any confidence.
    try:
        from seed_trip import OPTIONS_CHECKED
    except Exception:
        OPTIONS_CHECKED = None
    con2 = db.connect()
    base_row, from_base, legs_by_day = _trip_distances(con2, rows, _trip_country(trip))
    wx = _weather_for_days(con2, trip["slug"], days, shelf)
    exp_rows = _expense_rows(con2, trip["slug"])
    con2.close()
    spent = _expense_summary(exp_rows) if exp_rows else None
    return render_template("trip.html", trip=trip, days=days, shelf=shelf,
                           loose=loose, base_row=base_row, from_base=from_base,
                           legs_by_day=legs_by_day, geo=geo,
                           wx=wx, weather=weather, shelters=db.TRIP_SHELTERS,
                           dropped=dropped, owed=owed, options_checked=OPTIONS_CHECKED,
                           totals=totals, countdown=countdown, view=view, spent=spent,
                           media=_trip_media(trip["slug"]),
                           date_range=_trip_date_range(trip),
                           kinds=db.TRIP_KINDS, plans=db.TRIP_PLANS,
                           bookings=db.TRIP_BOOKINGS, parts=db.TRIP_PARTS,
                           areas=db.TRIP_AREAS, part_labels=db.PART_LABELS,
                           plan_labels=TRIP_PLAN_LABELS,
                           booking_labels=TRIP_BOOKING_LABELS,
                           partner=PARTNER_EMAIL, partner_name=PARTNER_NAME)


def _touch(con, item_id, who):
    con.execute("UPDATE trip_items SET updated_at=?, updated_by=? WHERE id=?",
                (db.now(), (who or "a parent").strip()[:20], item_id))


def _resync_status(con, item_id):
    """Keep the legacy single column truthful. Nothing reads it, but a column that
    silently stops updating is a trap for whoever opens the DB next."""
    r = con.execute("SELECT plan, booking FROM trip_items WHERE id=?",
                    (item_id,)).fetchone()
    if r:
        con.execute("UPDATE trip_items SET status=? WHERE id=?",
                    (db.legacy_status(r["plan"], r["booking"]), item_id))


def _back(slug, item_id=None):
    view = request.form.get("view") or request.args.get("view")
    url = url_for("trip_page", slug=slug)
    if view == "sheet":
        url += "?view=sheet"
    return redirect(url + (f"#item-{item_id}" if item_id else ""))


@app.route("/trip/<slug>/item/<int:item_id>/when", methods=["POST"])
def trip_item_when(slug, item_id):
    """Move an item: which day, what time, or deliberately no time at all.

    This route is the whole difference between a report and a workspace. Until it
    existed the ONLY way an item ever got a time was to be created with one, so nothing
    already on the page could be scheduled, re-timed or moved.
    """
    f = request.form
    con = db.connect()
    row = con.execute("SELECT * FROM trip_items WHERE id=? AND trip=?",
                      (item_id, slug)).fetchone()
    if not row:
        con.close()
        return _back(slug)

    day = (f.get("day") or "").strip() or None
    time_ = (f.get("start_time") or "").strip() or None
    if time_ and _mins(time_) is None:
        time_ = None
    part = (f.get("part") or "").strip() or None
    if part not in db.TRIP_PARTS:
        part = None
    # A clock beats a part of day; keeping both would render one and store the other.
    if time_:
        part = None
    raw_dur = (f.get("duration_min") or "").strip()
    dur = int(raw_dur) if raw_dur.isdigit() else None

    plan = row["plan"]
    # Putting something on a day is an act of planning, so an idea becomes penciled --
    # and taking it back off returns it to the shelf. It never silently promotes anything
    # to "doing": that is a decision, and it has its own button.
    if day and plan == "idea":
        plan = "penciled"
    if not day and plan == "penciled":
        plan = "idea"

    con.execute("UPDATE trip_items SET day=?, start_time=?, part=?, duration_min=?, "
                "plan=? WHERE id=?", (day, time_, part, dur, plan, item_id))
    _touch(con, item_id, f.get("who"))
    _resync_status(con, item_id)
    con.commit()
    con.close()
    return _back(slug, item_id)


@app.route("/trip/<slug>/item/<int:item_id>/plan", methods=["POST"])
def trip_item_plan(slug, item_id):
    """Is it happening? idea -> penciled -> doing, or dropped."""
    new = (request.form.get("plan") or "").strip()
    if new not in db.TRIP_PLANS:
        return _back(slug)
    con = db.connect()
    row = con.execute("SELECT * FROM trip_items WHERE id=? AND trip=?",
                      (item_id, slug)).fetchone()
    if not row:
        con.close()
        return _back(slug)
    # Dropping something, or sending it back to the shelf, takes it off the day. Leaving
    # a dropped row sitting on Tuesday is how a day comes to disagree with itself.
    day = None if new in ("dropped", "idea") else row["day"]
    con.execute("UPDATE trip_items SET plan=?, day=? WHERE id=?", (new, day, item_id))
    _touch(con, item_id, request.form.get("who"))
    _resync_status(con, item_id)
    con.commit()
    con.close()
    return _back(slug, item_id)


@app.route("/trip/<slug>/item/<int:item_id>/booking", methods=["POST"])
def trip_item_booking(slug, item_id):
    """Does anyone owe a booking? Separate from whether it is happening -- the fireworks
    are free and unreservable, the rental car is happening and unbooked."""
    new = (request.form.get("booking") or "").strip()
    if new not in db.TRIP_BOOKINGS:
        return _back(slug)
    who = (request.form.get("who") or "").strip()
    con = db.connect()
    row = con.execute("SELECT * FROM trip_items WHERE id=? AND trip=?",
                      (item_id, slug)).fetchone()
    if not row:
        con.close()
        return _back(slug)
    # Never discard the reason it was outstanding -- keep it as the record of the call.
    note = row["open_reason"]
    if new == "booked" and note and who:
        note = note + "\n\nSettled by " + who + " on " + date.today().isoformat() + "."
    con.execute("UPDATE trip_items SET booking=?, open_reason=? WHERE id=?",
                (new, note, item_id))
    _touch(con, item_id, who)
    _resync_status(con, item_id)
    con.commit()
    con.close()
    return _back(slug, item_id)


@app.route("/trip/<slug>/item/<int:item_id>/move", methods=["POST"])
def trip_item_move(slug, item_id):
    """Where drag-and-drop lands: change WHERE a row lives and nothing else.

    /when re-states the whole schedule -- an absent field is a cleared field -- which
    is right for a form and wrong for a drag: Fireworks pulled from Wednesday onto
    Thursday must keep its 10pm. So this touches day (and the plan promotions that
    follow from it) and leaves clock, part, duration and booking exactly as they were.

    Targets: day=YYYY-MM-DD (an idea or dropped row is promoted to penciled, the same
    promotion /when makes); to=shelf (off the day, back to being a candidate);
    to=loose (settled but tied to no single day -- plan is kept)."""
    f = request.form
    con = db.connect()
    row = con.execute("SELECT * FROM trip_items WHERE id=? AND trip=?",
                      (item_id, slug)).fetchone()
    if not row:
        con.close()
        return jsonify({"error": "no such row on this trip"}), 404
    day = (f.get("day") or "").strip() or None
    to = (f.get("to") or "").strip()
    if day:
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            con.close()
            return jsonify({"error": f"not a date: {day}"}), 400
        plan = "doing" if row["plan"] == "doing" else "penciled"
    elif to == "shelf":
        plan = "idea"
    elif to == "loose":
        plan = row["plan"] if row["plan"] in ("penciled", "doing") else "penciled"
    else:
        con.close()
        return jsonify({"error": "say where: day=YYYY-MM-DD, to=shelf or to=loose"}), 400
    con.execute("UPDATE trip_items SET day=?, plan=? WHERE id=?", (day, plan, item_id))
    _touch(con, item_id, f.get("who"))
    _resync_status(con, item_id)
    con.commit()
    con.close()
    return jsonify({"ok": True, "day": day, "plan": plan})


@app.route("/trip/<slug>/item/<int:item_id>/delete", methods=["POST"])
def trip_item_delete(slug, item_id):
    """Gone means gone -- and only for a row already DROPPED. A real decision stays on
    the dropped list ('kept so nobody rediscovers it at the door'); delete exists for
    the fat-fingered add, which until now was immortal. Drop first, then delete: two
    steps is what keeps a slip of the thumb from erasing a booked hotel. Also the
    teardown path check_trip.py uses, so the gate cleans up over HTTP and works
    against a REMOTE hub -- its old direct-DB delete hit whatever machine the gate
    ran on, and every probe row it leaked here broke the next run's assertions."""
    con = db.connect()
    row = con.execute("SELECT plan, gcal_event_id FROM trip_items WHERE id=? AND trip=?",
                      (item_id, slug)).fetchone()
    if not row:
        con.close()
        return _back(slug)
    if row["plan"] != "dropped":
        con.close()
        return ("Only a row already marked 'Not this trip' can be deleted -- "
                "drop it first.", 409)
    if row["gcal_event_id"]:
        con.close()
        return ("This row still has a Google Calendar event -- take its day off "
                "the calendar first, or the event would be orphaned.", 409)
    con.execute("DELETE FROM trip_items WHERE id=? AND trip=?", (item_id, slug))
    con.commit()
    con.close()
    return _back(slug)


@app.route("/trip/<slug>/add", methods=["POST"])
def trip_item_add(slug):
    """Add anything, from anywhere on the page, with nothing but a title if that is all
    you have. The whole point: nobody should ever have to ask for a row to be added."""
    f = request.form
    title = (f.get("title") or "").strip()
    if not title:
        return _back(slug)
    who = (f.get("who") or "a parent").strip()[:20]

    day = (f.get("day") or "").strip() or None
    time_ = (f.get("start_time") or "").strip() or None
    if time_ and _mins(time_) is None:
        time_ = None
    part = (f.get("part") or "").strip()
    part = part if (part in db.TRIP_PARTS and not time_) else None
    raw_dur = (f.get("duration_min") or "").strip()
    dur = int(raw_dur) if raw_dur.isdigit() else None
    area = f.get("area") if f.get("area") in db.TRIP_AREAS else None

    plan = f.get("plan") if f.get("plan") in db.TRIP_PLANS else "idea"
    if day and plan == "idea":
        plan = "penciled"
    if not day and plan == "penciled":
        plan = "idea"
    booking = f.get("booking") if f.get("booking") in db.TRIP_BOOKINGS else "none"

    # A cost nobody can read is NOT zero and never a plausible guess: it stays NULL, shows
    # as "not priced", and is counted out of every total by name.
    raw_cost = (f.get("cost") or "").strip()
    cost_est = None
    cost_note = raw_cost or None
    if raw_cost:
        cleaned = "".join(c for c in raw_cost if c.isdigit() or c == ".")
        try:
            cost_est = float(cleaned) if cleaned else None
        except ValueError:
            cost_est = None
        if cost_est is None:
            cost_note = raw_cost + " — not a number, so it stays out of the totals"

    con = db.connect()
    cur = con.execute(
        "INSERT OR IGNORE INTO trip_items (trip, day, start_time, part, duration_min, "
        "kind, title, detail, location, confirmation, cost, cost_est, currency, area, "
        "plan, booking, status, source, sort, updated_at, updated_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (slug, day, time_, part, dur,
         f.get("kind") if f.get("kind") in db.TRIP_KINDS else "activity",
         title, (f.get("detail") or "").strip() or None,
         (f.get("location") or "").strip() or None,
         (f.get("confirmation") or "").strip() or None,
         cost_note, cost_est, "CAD", area, plan, booking,
         db.legacy_status(plan, booking),
         "Added on the page by " + who, 500, db.now(), who))
    con.commit()
    new_id = cur.lastrowid if cur.rowcount else None
    con.close()
    return _back(slug, new_id)


# ---------------------------------------------------------------- trip -> Google
#
# Reuses exactly what the days-off board already does: gcal.session(), find_by_key,
# create_event with the fm_key extended property -- which is also what stops the nightly
# ingest reading our own events back in as school events. One item per request so the
# button can show real i/N progress instead of a spinner during N round trips.

def _gcal_key(slug, item_id):
    return f"trip:{slug}:{item_id}"


def _gcal_description(r):
    bits = [r["glance"], r["detail"]]
    if r["location"]:
        bits.append("Where: " + r["location"])
    if r["confirmation"]:
        bits.append("Confirmation: " + r["confirmation"])
    if r["cost"]:
        bits.append("Cost: " + r["cost"])
    bits.append("From the Family Manager trip page.")
    return "\n\n".join(b for b in bits if b)


@app.route("/trip/<slug>/day/<day>/gcal.json")
def trip_day_gcal_plan(slug, day):
    """What WOULD go to the calendar for this day, so the browser can walk it and count.

    Only plan='doing' rows. Penciled means nobody has agreed to it yet, and putting a
    maybe on both parents' calendars is how a calendar stops being trusted.
    """
    con = db.connect()
    rows = con.execute("SELECT * FROM trip_items WHERE trip=? AND day=? AND plan='doing' "
                       "ORDER BY sort, id", (slug, day)).fetchall()
    con.close()
    rows = sorted(rows, key=_sort_key)
    return jsonify({
        "day": day,
        "partner": PARTNER_EMAIL,
        "items": [{
            "id": r["id"], "title": r["title"],
            "when": _fmt_clock(r["start_time"]) if r["start_time"]
                    else (db.PART_LABELS.get(r["part"]) or "Any time"),
            "on_calendar": bool(r["gcal_event_id"]),
            "changed": bool(r["gcal_event_id"])
                       and (r["gcal_sig"] or "") != _item_when_sig(r),
        } for r in rows],
    })


@app.route("/trip/<slug>/item/<int:item_id>/gcal", methods=["POST"])
def trip_item_gcal(slug, item_id):
    """Put ONE item on the calendar and invite the other parent. Idempotent: an item that already
    has an event is replaced in place, never duplicated."""
    import gcal

    con = db.connect()
    r = con.execute("SELECT * FROM trip_items WHERE id=? AND trip=?",
                    (item_id, slug)).fetchone()
    if not r or not r["day"]:
        con.close()
        return jsonify({"error": "that item is not on a day"}), 400
    if r["plan"] != "doing":
        con.close()
        return jsonify({"error": "only settled items go on the calendar"}), 400

    key = _gcal_key(slug, item_id)
    start = _mins(r["start_time"])
    try:
        sess = gcal.session()
        existing = r["gcal_event_id"] or (gcal.find_by_key(sess, key) or {}).get("id")
        if existing:
            gcal.delete_event(sess, existing, notify=False)
        if start is not None:
            end = _hhmm(min(start + (r["duration_min"] or 60), 23 * 60 + 59))
            ev = gcal.create_event(
                sess, summary=r["title"], description=_gcal_description(r),
                start=r["day"], end=r["day"], attendees=[PARTNER_EMAIL], key=key,
                all_day=False, start_time_=r["start_time"][:5], end_time_=end)
        else:
            # No clock is a real answer, so it becomes an all-day event: it shows at the
            # top of the day without claiming a slot nobody agreed to.
            ev = gcal.create_event(
                sess, summary=r["title"], description=_gcal_description(r),
                start=r["day"], end=r["day"], attendees=[PARTNER_EMAIL], key=key,
                all_day=True)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the user, never swallowed
        con.close()
        return jsonify({"error": str(exc)[:300]}), 502

    con.execute("UPDATE trip_items SET gcal_event_id=?, gcal_sig=? WHERE id=?",
                (ev["id"], _item_when_sig(r), item_id))
    con.commit()
    con.close()
    return jsonify({"id": ev["id"], "link": ev.get("htmlLink"), "title": r["title"]})


@app.route("/trip/<slug>/day/<day>/gcal-remove", methods=["POST"])
def trip_day_gcal_remove(slug, day):
    """Undo the whole day. Deletes the events and clears the ids that pointed at them."""
    import gcal

    con = db.connect()
    rows = con.execute("SELECT id, gcal_event_id FROM trip_items WHERE trip=? AND day=? "
                       "AND gcal_event_id IS NOT NULL", (slug, day)).fetchall()
    removed, failed = 0, []
    try:
        sess = gcal.session()
    except Exception as exc:  # noqa: BLE001
        con.close()
        return jsonify({"error": str(exc)[:300]}), 502
    for r in rows:
        try:
            if gcal.delete_event(sess, r["gcal_event_id"]):
                con.execute("UPDATE trip_items SET gcal_event_id=NULL, gcal_sig=NULL "
                            "WHERE id=?", (r["id"],))
                removed += 1
            else:
                failed.append(r["id"])
        except Exception:  # noqa: BLE001, PERF203
            failed.append(r["id"])
    con.commit()
    con.close()
    return jsonify({"removed": removed, "failed": failed})


@app.route("/trip/<slug>.xlsx")
def trip_xlsx(slug):
    """Every table a human looks at is downloadable -- including the one you want on a
    phone with no signal at a border crossing."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    con = db.connect()
    trip = con.execute("SELECT * FROM trips WHERE slug=?", (slug,)).fetchone()
    rows = con.execute("SELECT * FROM trip_items WHERE trip=? ORDER BY day IS NULL, "
                       "day, sort, id", (slug,)).fetchall()
    con.close()
    if not trip:
        return redirect(url_for("trip_page"))

    cols = ["Day", "Ends", "When", "How long", "Type", "Item", "At a glance", "Details",
            "Where", "Area", "Confirmation", "Cost", "Est. cost", "Currency", "Paid",
            "Happening?", "Booking", "Still open / deadline", "On the calendar", "Source"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Itinerary"
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="C4974C")
        cell.alignment = Alignment(vertical="center")
    for r in rows:
        # An untimed row is not a blank cell -- "Afternoon" is what was actually decided.
        when = r["start_time"] or db.PART_LABELS.get(r["part"], "")
        if r["start_time"] and r["end_time"]:
            when = f"{when}-{r['end_time']}"
        openish = r["open_reason"] or ""
        if r["deadline"]:
            openish = (openish + f" (by {r['deadline']})").strip()
        ws.append([r["day"] or "no day yet", r["end_day"], when,
                   r["duration_min"] and f"{r['duration_min']} min",
                   TRIP_KIND_LABELS.get(r["kind"], r["kind"]), r["title"], r["glance"],
                   r["detail"], r["location"], db.TRIP_AREAS.get(r["area"], ""),
                   r["confirmation"], r["cost"], r["cost_est"],
                   r["currency"] if r["cost_est"] is not None else "",
                   r["paid"],
                   TRIP_PLAN_LABELS.get(r["plan"], r["plan"]),
                   TRIP_BOOKING_LABELS.get(r["booking"], r["booking"]), openish,
                   "yes" if r["gcal_event_id"] else "", r["source"]])
    widths = [12, 11, 12, 10, 11, 42, 46, 70, 40, 14, 16, 34, 10, 9, 40, 13, 16, 60, 15, 40]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row_cells in ws.iter_rows(min_row=2):
        for cell in row_cells:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{ws.max_row}"
    # The ledger rides along as a second sheet, so one download is the whole trip.
    con = db.connect()
    exp = _expense_rows(con, slug)
    con.close()
    if exp:
        _expenses_sheet(wb.create_sheet("Expenses"), exp)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from flask import send_file

    return send_file(buf, as_attachment=True,
                     download_name=f"{slug}-itinerary.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument."
                              "spreadsheetml.sheet")



# ---- the expense ledger ------------------------------------------------------------
# What the trip actually cost, read off the card statements, put against what the plan
# said it would. Amounts are USD as BILLED; CAD is derived from a labeled Bank of Canada
# rate. Every total names what it left out (home charges, bills, pending copies).

def _expense_rows(con, slug):
    return con.execute(
        "SELECT e.*, i.title AS item_title FROM trip_expenses e "
        "LEFT JOIN trip_items i ON i.id = e.item_id "
        "WHERE e.trip=? ORDER BY e.phase='before' DESC, e.posted IS NULL, e.posted, e.id",
        (slug,)).fetchall()


def _expense_summary(rows):
    """Totals a reader can trust: each one says what it counts. Charges only (no fee
    rows, no pending copies) unless the block is ABOUT fees or pending."""
    charges = [r for r in rows if r["dup_of"] is None and r["fee_for"] is None]
    trip = [r for r in charges if r["scope"] == "trip"]
    during = [r for r in trip if r["phase"] == "during"]
    before = [r for r in trip if r["phase"] == "before"]
    fees = [r for r in rows if r["fee_for"] is not None and r["scope"] == "trip"
            and r["dup_of"] is None]
    pending = [r for r in during if r["status"] == "pending"]
    home = [r for r in charges if r["scope"] == "home"]
    excluded = [r for r in charges if r["scope"] == "excluded"]
    dups = [r for r in rows if r["dup_of"] is not None]

    def usd(rs):
        return round(sum(float(r["amount"]) for r in rs), 2)

    def cad(rs):
        """Approximate: only the rows that carry a rate, and says how many did not."""
        with_fx = [r for r in rs if r["fx"]]
        return (round(sum(float(r["amount"]) * float(r["fx"]) for r in with_fx), 2),
                len(rs) - len(with_fx))

    by_cat = {}
    for r in during:
        d = by_cat.setdefault(r["category"] or "other", {"n": 0, "usd": 0.0})
        d["n"] += 1
        d["usd"] = round(d["usd"] + float(r["amount"]), 2)
    fee_usd = usd(fees)
    if fee_usd:
        by_cat["fee"] = {"n": len(fees), "usd": fee_usd}
    cats = sorted(by_cat.items(), key=lambda kv: -kv[1]["usd"])

    by_card = {}
    for r in during + fees:
        d = by_card.setdefault(r["card"] or "unknown", {"n": 0, "usd": 0.0, "fees": 0.0})
        if r["fee_for"] is not None:
            d["fees"] = round(d["fees"] + float(r["amount"]), 2)
        else:
            d["n"] += 1
            d["usd"] = round(d["usd"] + float(r["amount"]), 2)
    cards = sorted(by_card.items(), key=lambda kv: -kv[1]["usd"])

    # By the day it happened where somebody said so, else the posting date. Those are
    # different columns on the row, so the table never claims a precision it lacks.
    by_day = {}
    for r in during:
        k = r["day"] or r["posted"] or "pending"
        d = by_day.setdefault(k, {"n": 0, "usd": 0.0, "dated": 0})
        d["n"] += 1
        d["dated"] += 1 if r["day"] else 0
        d["usd"] = round(d["usd"] + float(r["amount"]), 2)
    days = sorted(by_day.items(), key=lambda kv: (kv[0] == "pending", kv[0]))

    during_cad, during_nofx = cad(during)
    fees_cad, _ = cad(fees)
    return {
        "during": usd(during), "during_n": len(during),
        "during_cad": during_cad, "during_nofx": during_nofx,
        "fees": fee_usd, "fees_n": len(fees), "fees_cad": fees_cad,
        "pending": usd(pending), "pending_n": len(pending),
        "before": usd(before), "before_n": len(before),
        "whole": round(usd(during) + fee_usd + usd(before), 2),
        "home": usd(home), "home_n": len(home),
        "excluded": usd(excluded), "excluded_n": len(excluded),
        "dups": usd(dups), "dups_n": len(dups),
        "cats": cats, "cards": cards, "days": days,
        "fx_source": next((r["fx_source"] for r in during if r["fx_source"]), None),
    }


def _planned_vs_actual(con, slug, rows):
    """Only rows that point at a priced plan item, never a guess at which charge was
    which attraction. Estimates are CAD, charges USD; converted at the charge's own rate."""
    out = []
    linked = {}
    for r in rows:
        if (r["item_id"] and r["scope"] == "trip" and r["dup_of"] is None
                and r["fee_for"] is None):
            linked.setdefault(r["item_id"], []).append(r)
    for item_id, rs in linked.items():
        it = con.execute("SELECT id, title, cost, cost_est, currency FROM trip_items WHERE id=?",
                         (item_id,)).fetchone()
        if not it:
            continue
        usd = round(sum(float(r["amount"]) for r in rs), 2)
        fx = next((float(r["fx"]) for r in rs if r["fx"]), None)
        est = it["cost_est"]
        est_cur = it["currency"] or "CAD"
        actual = None
        if est is not None and float(est) > 0:
            if est_cur == "USD":
                actual = usd
            elif fx is not None:
                actual = round(usd * fx, 2)
        delta = round(actual - float(est), 2) if actual is not None else None
        # Checked and accepted by a human? Then the difference is reported, not flagged.
        settled = all(r["settled"] for r in rs)
        out.append({"item": it, "rows": rs, "usd": usd, "est": est, "est_cur": est_cur,
                    "actual": actual, "delta": delta, "fx": fx, "settled": settled})
    out.sort(key=lambda d: -(abs(d["delta"]) if d["delta"] is not None else 0))
    return out


@app.route("/trip/<slug>/expenses")
def trip_expenses(slug):
    con = db.connect()
    trip = con.execute("SELECT * FROM trips WHERE slug=?", (slug,)).fetchone()
    if not trip:
        con.close()
        return redirect(url_for("trip_page"))
    rows = _expense_rows(con, slug)
    summary = _expense_summary(rows)
    pva = _planned_vs_actual(con, slug, rows)
    items = con.execute("SELECT id, title FROM trip_items WHERE trip=? AND plan != 'dropped' "
                        "ORDER BY day IS NULL, day, sort, id", (slug,)).fetchall()
    con.close()
    fees_by_parent = {r["fee_for"]: r for r in rows if r["fee_for"]}
    fee_cards = [(c, d) for c, d in summary["cards"] if d["fees"]]
    return render_template("trip_expenses.html", trip=trip, rows=rows, s=summary, pva=pva,
                           items=items, fees_by_parent=fees_by_parent, fee_cards=fee_cards,
                           err=request.args.get("err"),
                           categories=db.TRIP_EXPENSE_CATEGORIES,
                           scopes=db.TRIP_EXPENSE_SCOPES,
                           date_range=_trip_date_range(trip))


@app.route("/trip/<slug>/expense/<int:eid>", methods=["POST"])
def trip_expense_edit(slug, eid):
    f = request.form
    con = db.connect()
    row = con.execute("SELECT * FROM trip_expenses WHERE id=? AND trip=?", (eid, slug)).fetchone()
    if not row:
        con.close()
        return redirect(url_for("trip_expenses", slug=slug))
    cat = f.get("category") if f.get("category") in db.TRIP_EXPENSE_CATEGORIES else row["category"]
    scope = f.get("scope") if f.get("scope") in db.TRIP_EXPENSE_SCOPES else row["scope"]
    item_id = (f.get("item_id") or "").strip()
    item_id = int(item_id) if item_id.isdigit() else None
    day = (f.get("day") or "").strip() or None
    con.execute(
        "UPDATE trip_expenses SET label=?, category=?, scope=?, day=?, item_id=?, note=?, "
        "settled=?, updated_at=?, updated_by=? WHERE id=?",
        ((f.get("label") or "").strip() or row["label"], cat, scope, day, item_id,
         (f.get("note") or "").strip() or None, 1 if f.get("settled") else 0, db.now(),
         (f.get("who") or "a parent").strip()[:20], eid))
    # A fee follows its charge: same scope, same day.
    con.execute("UPDATE trip_expenses SET scope=?, day=? WHERE fee_for=?", (scope, day, eid))
    con.commit()
    con.close()
    return redirect(url_for("trip_expenses", slug=slug) + f"#exp-{eid}")


@app.route("/trip/<slug>/expense/add", methods=["POST"])
def trip_expense_add(slug):
    f = request.form
    try:
        amount = round(float((f.get("amount") or "").replace(",", "").replace("$", "")), 2)
    except ValueError:
        return redirect(url_for("trip_expenses", slug=slug) + "?err=amount#add")
    merchant = (f.get("merchant") or "").strip()
    if not merchant:
        return redirect(url_for("trip_expenses", slug=slug) + "?err=merchant#add")
    posted = (f.get("posted") or "").strip() or None
    status = "pending" if not posted else "posted"
    card = (f.get("card") or "").strip() or None
    cat = f.get("category") if f.get("category") in db.TRIP_EXPENSE_CATEGORIES else "other"
    scope = f.get("scope") if f.get("scope") in db.TRIP_EXPENSE_SCOPES else "trip"
    phase = "before" if f.get("phase") == "before" else "during"
    who = (f.get("who") or "a parent").strip()[:20]
    fx = fx_src = None
    if phase == "during":
        try:
            from seed_expenses import fx_for
            fx, fx_src = fx_for(posted)
        except Exception:
            pass
    con = db.connect()
    n = con.execute("SELECT COUNT(*) FROM trip_expenses WHERE trip=? AND card IS ? AND posted IS ? "
                    "AND merchant=? AND amount=?", (slug, card, posted, merchant, amount)).fetchone()[0]
    key = f"{card}|{posted or 'pending'}|{merchant}|{amount:.2f}|{n + 1}"
    cur = con.execute(
        "INSERT INTO trip_expenses (trip, key, posted, day, merchant, label, category, amount, "
        "card_currency, card, status, phase, scope, fx, fx_source, note, source, ordinal, "
        "updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (slug, key, posted, (f.get("day") or "").strip() or None, merchant,
         (f.get("label") or "").strip() or merchant.title(), cat, amount, "USD", card, status,
         phase, scope, fx, fx_src, (f.get("note") or "").strip() or None,
         f"added on the page by {who}", n + 1, db.now(), who))
    eid = cur.lastrowid
    fee = (f.get("fee") or "").strip().replace("$", "")
    if fee:
        try:
            fee_amt = round(float(fee), 2)
        except ValueError:
            fee_amt = None
        if fee_amt:
            con.execute(
                "INSERT INTO trip_expenses (trip, key, posted, merchant, label, category, amount, "
                "card_currency, card, status, phase, scope, fee_for, fx, fx_source, source, "
                "ordinal, updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (slug, f"{card}|{posted or 'pending'}|FOREIGN TRANSACTION FEE|{fee_amt:.2f}|{eid}",
                 posted, "FOREIGN TRANSACTION FEE", "fee", "fee", fee_amt, "USD", card, status,
                 phase, scope, eid, fx, fx_src, f"added on the page by {who}", eid, db.now(), who))
    con.commit()
    con.close()
    return redirect(url_for("trip_expenses", slug=slug) + f"#exp-{eid}")


@app.route("/trip/<slug>/expense/<int:eid>/delete", methods=["POST"])
def trip_expense_delete(slug, eid):
    con = db.connect()
    con.execute("DELETE FROM trip_expenses WHERE trip=? AND (id=? OR fee_for=?)", (slug, eid, eid))
    con.execute("UPDATE trip_expenses SET dup_of=NULL WHERE dup_of=?", (eid,))
    con.commit()
    con.close()
    return redirect(url_for("trip_expenses", slug=slug))


def _expenses_sheet(ws, rows):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    cols = ["Posted", "Happened", "Status", "Card", "Statement line", "What it was", "Category",
            "Amount (USD, as billed)", "CAD approx", "Rate basis", "Counts toward", "Fee on row",
            "Copy of row", "Plan item", "Note", "Source"]
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="C4974C")
    for r in rows:
        cad = round(float(r["amount"]) * float(r["fx"]), 2) if r["fx"] else None
        counts = ("before the trip" if r["phase"] == "before" and r["scope"] == "trip"
                  else db.TRIP_EXPENSE_SCOPES.get(r["scope"], r["scope"]))
        if r["dup_of"]:
            counts = "not counted (copy of a posted charge)"
        ws.append([r["posted"] or "pending", r["day"], r["status"], r["card"], r["merchant"],
                   r["label"], db.TRIP_EXPENSE_CATEGORIES.get(r["category"], r["category"]),
                   float(r["amount"]), cad, r["fx_source"], counts,
                   r["fee_for"], r["dup_of"], r["item_title"], r["note"], r["source"]])
    for i, w in enumerate([12, 12, 9, 26, 40, 34, 20, 12, 12, 40, 26, 8, 8, 40, 60, 30], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row_cells in ws.iter_rows(min_row=2):
        for cell in row_cells:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        row_cells[7].number_format = "#,##0.00"
        row_cells[8].number_format = "#,##0.00"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{ws.max_row}"


@app.route("/trip/<slug>/expenses.xlsx")
def trip_expenses_xlsx(slug):
    import io

    from flask import send_file
    from openpyxl import Workbook

    con = db.connect()
    rows = _expense_rows(con, slug)
    con.close()
    wb = Workbook()
    ws = wb.active
    ws.title = "Expenses"
    _expenses_sheet(ws, rows)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{slug}-expenses.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument."
                              "spreadsheetml.sheet")


@app.route("/privacy")
def privacy():
    """What the Google OAuth consent screen links to. The app was pushed to production on
    2026-09-04 so its refresh token stops dying every 7 days, and production needs a
    privacy-policy URL on the Branding page; this is that page, and it is true."""
    return render_template("privacy.html")


@app.get("/healthz")
def healthz():
    """Post-deploy sanity check, and — since 2026-08-21 — an actual verdict.

    `ok` used to be the literal `True`. A health endpoint that cannot return false is a
    green light wired to the switch, not to the bulb: the nightly job could fail every
    night and this still answered ok. It now judges what it already had in front of it —
    `ingest_status.json` has recorded ok/failed and a timestamp since the app was built,
    and nothing ever read either.

    The two failures it is here to catch look identical from the outside and are not:
    a step that FAILED, and a job that stopped RUNNING. The board renders yesterday's
    data perfectly well in both cases. Freshness is the only thing that separates them.
    Still no secrets echoed.
    """
    con = db.connect()
    events = con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    con.close()

    status = _read_status()
    stamp = status.get("finished") or status.get("updated")
    age_h = None
    problems = []
    if not stamp:
        problems.append("the ingest has never recorded a run")
    else:
        try:
            age_h = round((datetime.now() - datetime.fromisoformat(stamp))
                          .total_seconds() / 3600, 1)
        except Exception:
            problems.append(f"the last ingest stamp is unreadable: {stamp!r}")
        else:
            # The job runs daily at 4 PM; past 36h it has missed one outright.
            if age_h > 36:
                problems.append(f"the last ingest finished {age_h}h ago — the daily "
                                "job has missed at least one run")
    if status.get("ok") is False:
        problems.append(f"the last ingest FAILED: {status.get('summary')}")
    elif status.get("ok") is None and stamp:
        problems.append("the last ingest recorded no ok/failed verdict")
    for gap in status.get("gaps") or []:
        problems.append(f"ingest gap: {gap}")

    # The nightly job's own verdict, if the nightly job has reported through job_report.py.
    # Absent is reported as absent, never folded into the pass.
    try:
        import job_report
        job_state = job_report.load()
        if job_state.get("steps"):
            _, job_problems = job_report.verdict(job_state, datetime.now())
            problems.extend(job_problems)
            job_seen = True
        else:
            job_seen = False
    except Exception as exc:
        problems.append(f"could not read the nightly job verdict: {exc}")
        job_seen = None

    return jsonify({
        "ok": not problems,
        "problems": problems,
        "auth_mode": auth.AUTH_MODE,
        "allowlist_size": len(auth._allowlist()),
        "gmail_password_set": bool(os.environ.get("GMAIL_APP_PASSWORD")),
        "data_dir": str(db.DATA_DIR),
        "events": events,
        "ingest": status.get("summary"),
        "ingest_finished": stamp,
        "ingest_age_hours": age_h,
        "nightly_job_reporting": job_seen,
        "ingest_every_hours": os.environ.get("FM_INGEST_EVERY_HOURS", "0"),
    }), (200 if not problems else 503)


household.install(app, globals())


def _start_ingest_scheduler():
    """In-process daily ingest for Railway (no Task Scheduler there).

    Enabled by FM_INGEST_EVERY_HOURS > 0. Requires the single-worker gunicorn
    config in the Procfile — more workers would run duplicate schedulers.
    Locally this stays off (0); the Windows Task Scheduler task does the job.
    """
    every_h = float(os.environ.get("FM_INGEST_EVERY_HOURS", "0") or 0)
    if every_h <= 0:
        return

    def loop():
        time.sleep(120)  # let the app settle after deploy before first pull
        while True:
            try:
                import ingest
                ingest.main()
            except Exception as e:
                print(f"[scheduler] ingest failed: {e}", flush=True)
            time.sleep(every_h * 3600)

    threading.Thread(target=loop, daemon=True, name="fm-ingest-scheduler").start()


_start_ingest_scheduler()


if __name__ == "__main__":
    # Local dev. On Railway gunicorn binds $PORT via the Procfile instead.
    # Port from the environment so a second instance can be stood up beside the live
    # hub -- which is how the "send this day to Google" write path gets proven without
    # restarting the thing the family is using, or pointing the real hub at a test
    # invitee. Default is unchanged.
    app.run(host="127.0.0.1", port=int(os.environ.get("FM_PORT", "5088")), debug=False)
