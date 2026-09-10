"""Fill the app with a few weeks of the fictional example family, so a fresh clone has
something to look at before any real mail arrives.

    python demo_seed.py            # add the demo rows (dates are relative to today)
    python demo_seed.py --clear    # remove every demo row

Adding only runs against the example household. With your own data/household.json in place it
refuses, so demo rows can never land in a real family's board. Clearing always runs: if you
tried the demo before writing your household, `--clear` takes the example kids back out.
Every row it writes is tagged source='demo' (or a demo: key), which is what --clear removes.
"""
import json
import sys
from datetime import date, timedelta

import _env  # noqa: F401
import db
import family

TODAY = date.today()


def weekday(offset: int) -> str:
    """A school day `offset` days out, pushed forward past a weekend."""
    d = TODAY + timedelta(days=offset)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def saturday_after(offset: int) -> str:
    d = TODAY + timedelta(days=offset)
    while d.weekday() != 5:
        d += timedelta(days=1)
    return d.isoformat()


def next_birthday(mmdd: str) -> str:
    m, d = (int(x) for x in mmdd.split("-"))
    b = date(TODAY.year, m, d)
    return (b if b >= TODAY else date(TODAY.year + 1, m, d)).isoformat()


def clear(con) -> int:
    before = con.total_changes
    for table in ("events", "checklist", "payments"):
        col = "notes" if table == "payments" else "source"
        con.execute(f"DELETE FROM {table} WHERE {col} = 'demo'")
    con.execute("DELETE FROM mail_actions WHERE msg_id LIKE 'demo:%'")
    con.execute("DELETE FROM emails WHERE msg_id LIKE 'demo:%'")
    con.execute("DELETE FROM coverage_plan WHERE note = 'demo'")
    con.execute("DELETE FROM gathering_items WHERE gathering IN (SELECT slug FROM gatherings WHERE notes = 'demo')")
    con.execute("DELETE FROM gathering_guests WHERE gathering IN (SELECT slug FROM gatherings WHERE notes = 'demo')")
    con.execute("DELETE FROM gatherings WHERE notes = 'demo'")
    con.commit()
    return con.total_changes - before


def seed(con) -> int:
    """Add the demo rows. Returns how many rows were actually written: a second run writes 0."""
    before = con.total_changes
    k0 = family.KIDS[0]
    k1 = family.KIDS[1] if len(family.KIDS) > 1 else family.KIDS[0]
    p0 = family.PARENTS[0]
    p1 = family.PARENTS[1] if len(family.PARENTS) > 1 else family.PARENTS[0]
    school0 = family.KID_SCHOOLS.get(k0) or "School"
    school1 = family.KID_SCHOOLS.get(k1) or "Preschool"
    now = db.now()

    today = TODAY.isoformat()
    events = [
        # Two things today, so the home page's "today" card has something on it.
        (k0, school0, "event", "Library day: bring the books back", today, None, None, "kids_school"),
        (k0, None, "event", "Soccer practice", today, "17:00", "18:00", "kids_school"),
        (k1, school1, "event", "Pajama day", weekday(2), None, None, "kids_school"),
        (k0, school0, "deadline", "Field trip permission slip due", weekday(4), None, None, "kids_school"),
        (k0, school0, "event", "Picture day", weekday(3), None, None, "kids_school"),
        (k0, school0, "meeting", "Back to school night", weekday(6), "18:00", "20:00", "kids_school"),
        (k0, school0, "early_dismissal", "Early dismissal 1:15 PM (conferences)", weekday(8), None, None, "kids_school"),
        (k1, school1, "closure", f"{school1} closed: teacher planning day", weekday(9), None, None, "kids_school"),
        (k0, school0, "event", "Field trip: apple orchard", weekday(10), None, None, "kids_school"),
        ("Both", None, "closure", "Schools closed: Indigenous Peoples' Day", weekday(12), None, None, "kids_school"),
        (k1, None, "event", "Swim lesson", saturday_after(1), "09:30", "10:00", "kids_school"),
        (k1, None, "event", "Swim lesson", saturday_after(8), "09:30", "10:00", "kids_school"),
        ("Both", None, "event", "Dentist: both kids", weekday(15), "15:30", "16:30", "kids_school"),
    ]
    for kid, school, typ, title, day, start, end, cat in events:
        con.execute(
            "INSERT OR IGNORE INTO events (kid, school, type, title, event_date, start_time, end_time, "
            "category, details, source, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,'demo','active',?)",
            (kid, school, typ, title, day, start, end, cat, "", now))

    checklist = [
        ("Sign and return the field trip permission slip", "$12 cash in the envelope", k0, "admin", weekday(4), "open", None, None),
        ("Sneakers for PE", "Velcro, not laces", k0, "supplies", None, "done", None, p1),
        ("Label the nap mat and spare clothes", "", k1, "supplies", weekday(5), "open", None, None),
        ("Flu shots", "", "Both", "health", None, "blocked", "Waiting on the pediatrician's office to call back", None),
    ]
    for title, detail, kid, cat, due, status, blocked, who in checklist:
        con.execute(
            "INSERT OR IGNORE INTO checklist (title, detail, kid, category, due_date, status, blocked_on, "
            "done_by, done_at, source) VALUES (?,?,?,?,?,?,?,?,?, 'demo')",
            (title, detail, kid, cat, due, status, blocked, who, now if status == "done" else None))

    first_of_next = (TODAY.replace(day=1) + timedelta(days=32)).replace(day=1).isoformat()
    payments = [
        (f"{k1} swim lessons", k1, "activity", 45.0, "weekly", None, 0, "Swimming", "Riverside Rec Center", "Mondays"),
        (f"{school1} tuition", k1, "tuition", 1450.0, "monthly", first_of_next, 1, "Pre-K", school1, "the 1st"),
        (f"{k0} aftercare", k0, "aftercare", None, "monthly", first_of_next, 0, "Aftercare", school0, "the 1st"),
    ]
    for name, kid, cat, amt, cad, due, auto, act, org, rule in payments:
        if con.execute("SELECT 1 FROM payments WHERE name=? AND notes='demo'", (name,)).fetchone():
            continue
        con.execute(
            "INSERT INTO payments (name, kid, category, amount, cadence, next_due, autopay, notes, active, "
            "activity, organization, due_rule) VALUES (?,?,?,?,?,?,?, 'demo', 1, ?,?,?)",
            (name, kid, cat, amt, cad, due, auto, act, org, rule))

    src = family.SOURCES[0][0] if family.SOURCES else "pasted"
    summary = {
        "headline": "Apple orchard field trip: permission slip and $12 due " + weekday(4),
        "what_it_is": f"{school0}'s note about the first-grade field trip.",
        "action_items": [
            {"text": "Sign and return the permission slip", "due": weekday(4), "kid": k0},
            {"text": "Send $12 cash in a labeled envelope", "due": weekday(4), "kid": k0},
        ],
        "dates": [{"date": weekday(10), "title": "Field trip: apple orchard", "kid": k0}],
        "kid": k0, "money": "$12 per student",
    }
    cur = con.execute(
        "INSERT OR IGNORE INTO emails (msg_id, source, sender, subject, sent_date, kid, body_text, created_at, "
        "summary, summarized_at) VALUES ('demo:fieldtrip', ?, 'office@maplestreet.example.org', "
        "'Field trip to the apple orchard', ?, ?, ?, ?, ?, ?)",
        (src, TODAY.isoformat(), k0,
         "Our first graders visit the apple orchard. Please sign and return the permission slip with $12 cash.",
         now, json.dumps(summary), now))
    if cur.rowcount:
        db.save_summary(con, "demo:fieldtrip", summary, k0)

    con.execute(
        "INSERT OR IGNORE INTO coverage_plan (day, coverage, note, updated_at, updated_by) "
        "VALUES (?, 'Grandparents', 'demo', ?, ?)", (weekday(9), now, p0))

    if family.KID_BIRTHDAYS.get(k1) and not con.execute("SELECT 1 FROM gatherings WHERE notes='demo'").fetchone():
        import gatherings
        g = gatherings.create(con, {"name": f"{k1}'s birthday party", "kind": "kid_birthday", "honoree": k1,
                                    "occasion_date": next_birthday(family.KID_BIRTHDAYS[k1]), "notes": "demo"}, p0)
        con.execute("UPDATE gatherings SET notes='demo' WHERE slug=?", (g["slug"],))
    con.commit()
    return con.total_changes - before


def main() -> int:
    con = db.connect()
    if "--clear" in sys.argv:
        print(f"Demo rows removed: {clear(con)}.")
        return 0
    if not family.IS_EXAMPLE and "--force" not in sys.argv:
        print("Refusing: data/household.json is a real household. The demo only fills the example "
              "family, so its rows never mix with yours. (python demo_seed.py --clear still works.)")
        return 2
    n = seed(con)
    print(f"Demo rows added: {n}." if n else "The demo is already loaded: nothing added.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
