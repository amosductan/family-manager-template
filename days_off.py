"""Days off / coverage board — every day a kid is out of school, and who has it.

The hard part is NOT the UI. Two or three feeds describe the same day and none of them
dedupe against each other: one kid's closures arrive from a partner's Google invites AND
from the school's own calendar; the other's from Google, the preschool's weekly email, and
an auto-populated class calendar. Labor Day 2026-09-07 once existed as four rows.
gcal.dedupe() only collapses duplicates WITHIN one calendar pull, so this module merges on
(date, kid) across everything.

Dropping a feed is not an option either — the school's calendar carries days the invites
do not (a 2026-09-04 early dismissal), and the preschool's feed carries days the school's
calendar can't know about. Union, then merge.

Two kinds of day, and they are not the same problem:
  FULL   — school is closed. Somebody has to have the whole day.
  EARLY  — early dismissal / half day. Somebody has to collect them mid-day. This is a
           PICKUP problem, NOT a day off, and must never be labeled as time off unless a
           parent explicitly says so. (A parent deleted 12 events that got this wrong.)

Who the kids are and who can cover a day come from family.py (data/household.json).
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import family

FULL = "full"
EARLY = "early"

# Coverage is the human's answer to "who has this day". TBD is the default on purpose:
# an uncovered day has to LOOK uncovered, or the board is decoration.
COVERAGE_TBD = "TBD"
COVERAGE_BABYSITTER = "Babysitter"
# "Both" is the stored value for "both parents / every kid"; it only means something as a
# coverage answer when there are two or more parents.
COVERAGE_BOTH = "Both"


def coverage_vocabulary() -> list[str]:
    """Every answer to "who has this day": TBD, each parent, Both (2+ parents only), then
    the household's extra options (camp, grandparents, a babysitter...)."""
    out = [COVERAGE_TBD] + list(family.PARENTS)
    if len(family.PARENTS) >= 2:
        out.append(COVERAGE_BOTH)
    for extra in family.COVERAGE_EXTRA:
        if extra not in out:
            out.append(extra)
    return out


def self_title_regex() -> re.Pattern:
    """Title shapes of the events this app writes to the calendar: "<coverage> off — ...",
    "Sitter ...", "Pickup — ...". Built from the same vocabulary as COVERAGE_OPTIONS so a
    household's own names are recognized; gcal._OURS_TITLE and _SELF below share it.
    The babysitter option is always included, since a calendar event carrying it may
    outlive a household removing it from its options."""
    words = coverage_vocabulary()
    if COVERAGE_BABYSITTER not in words:
        words.append(COVERAGE_BABYSITTER)
    alt = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"^(?:{alt}) off\b|^Sitter\b|^Pickup\s*[—-]", re.IGNORECASE)


def _school_names() -> list[str]:
    """Each kid's school and its short name, lowercased, for recognizing a school notice."""
    names = []
    for k in family.CONFIG.get("kids") or []:
        for key in ("school", "school_short"):
            v = (k.get(key) or "").strip().lower()
            if v and v not in names:
                names.append(v)
    return names


def bind_household() -> None:
    """(Re)derive every household-dependent constant from family. Runs at import; a
    self-test calls it again after family.use_example()."""
    g = globals()
    g["KIDS"] = tuple(family.KIDS)
    g["COVERAGE_OPTIONS"] = coverage_vocabulary()
    g["_SELF"] = self_title_regex()
    schools = "|".join(re.escape(n).replace(r"\ ", r"\s*") for n in _school_names())
    g["_SCHOOLISH"] = re.compile(r"school|district|\bcamp\b|classes|dismissal|recess|\bpre-?k\b"
                                 + (f"|{schools}" if schools else ""), re.I)


# Only meaningful when coverage is a parent. A parent requests time off in their
# employer's system, which nothing here can see — so it is something they record, never
# something this module infers.
PTO_OPTIONS = ["none", "requested", "approved"]

# What a created calendar event is FOR. A pickup is deliberately not a day off.
GCAL_DAYOFF = "dayoff"
GCAL_PICKUP = "pickup"


def _d(s: str) -> date:
    return date.fromisoformat(s)


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def _kids_of(row_kid: str | None) -> set[str]:
    """A row's kid column is one kid's name or Both. Anything unrecognized means every kid,
    because guessing one child wrong hides a day from the other one's parent."""
    k = (row_kid or "").strip()
    return {k} if k in KIDS else set(KIDS)


# Feeds that restate the date inside the title ("Monday, September 7th - Little Oaks closed
# for Labor Day"). On a row already keyed by date that prefix is pure noise, and worse, it
# inflates the length so a padded title beats an informative one — two Labor Day titles
# tied at exactly 50 characters, one of which spent 24 of them on the date.
_DATE_PREFIX = re.compile(
    r"""^\s*(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?\s*,?\s*"""
    r"""(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d{1,2}(?:st|nd|rd|th)?"""
    r"""\s*[-–—:]\s*""",
    re.IGNORECASE,
)


def clean_title(title: str) -> str:
    """Drop a leading date phrase. The row already knows its date."""
    return _DATE_PREFIX.sub("", (title or "").strip()).strip()


def _richness(title: str) -> int:
    """When several feeds describe the same day, keep the copy that says the most. The
    school's feed says "Teachers' Convention - Schools Closed"; the Google invite says
    'Maple Street Closed'. The first one is the reason, and the reason is what a parent reads.
    """
    return len(clean_title(title))


def expand_span(event_date: str, end_date: str | None) -> list[date]:
    """A row may carry an end_date (a recess block stored as one row). Expand to weekdays.

    end_date here is INCLUSIVE — that is how the ingest stores it. Google's exclusive-end
    convention is converted at the boundary, never carried inland.
    """
    if not event_date:
        return []
    start = _d(event_date)
    stop = _d(end_date) if end_date else start
    if stop < start:
        stop = start
    out, cur = [], start
    while cur <= stop:
        if is_weekday(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


# An amenity closing is not a school closing. The rec center's pool shutting for
# maintenance produced 14 phantom "school closed, both kids" days — two of them the
# children's actual first day of school. extract.py now refuses to make an event out of
# newsletter prose at all; this is the second line of defense, because the bad rows it
# already wrote are still in the table and no re-ingest removes them.
_AMENITY = re.compile(r"\bpool\b|\bgym\b|fitness|locker|front desk|\blibrary\b|cafeteria|"
                      r"\bcafe\b|\bstore\b|\bshop\b|parking|elevator|maintenance", re.I)
# _SCHOOLISH (generic school words + each kid's school names) is built in bind_household().


# This board's own output, read back in by the nightly ingest. gcal.is_ours now drops these
# at fetch time, but rows written before that fix are still in the table — and one of them
# had already replaced "Little Oaks Closed" as the stated reason for a day, because it was
# longer. _SELF is built in bind_household() from the coverage vocabulary.
bind_household()


def is_school_closure(title: str) -> tuple[bool, str]:
    """Does this row actually say a KID'S SCHOOL is shut? Returns (ok, why-not).

    Never silently drops: the caller surfaces the rejects with their reason, because a day
    that disappears without explanation is indistinguishable from a day nobody noticed.
    """
    t = (title or "").strip()
    if not t:
        return False, "no title"
    if _SELF.match(t):
        return False, "an event this board created, not a school notice"
    if len(t.split()) > 14:
        return False, "reads as prose, not a closure notice"
    if _AMENITY.search(t) and not _SCHOOLISH.search(t):
        return False, "closes a facility, not a school"
    return True, ""


def merge_rows(rows, rejected_out: list | None = None) -> dict[str, dict]:
    """Cross-source merge. Returns {iso_date: {full: {kid: title}, early: {kid: title}}}.

    A kid marked FULL on a date is removed from EARLY on that date — being closed beats
    being dismissed early, and showing both reads as two separate problems.
    """
    days: dict[str, dict] = {}
    rejected: list[tuple[str, str, str]] = []
    for r in rows:
        kind = FULL if r["type"] == "closure" else EARLY
        title = clean_title(r["title"])
        ok, why = is_school_closure(title)
        if not ok:
            rejected.append((r["event_date"], title[:80], why))
            continue
        for d in expand_span(r["event_date"], r["end_date"]):
            iso = d.isoformat()
            slot = days.setdefault(iso, {FULL: {}, EARLY: {}})
            for kid in _kids_of(r["kid"]):
                have = slot[kind].get(kid)
                if have is None or _richness(title) > _richness(have):
                    slot[kind][kid] = title

    for iso, slot in days.items():
        for kid in list(slot[EARLY]):
            if kid in slot[FULL]:
                del slot[EARLY][kid]
    if rejected_out is not None:
        rejected_out.extend(rejected)
    return {k: v for k, v in days.items() if v[FULL] or v[EARLY]}


def board(rows, plans: dict, holidays: dict, today: str | None = None,
          rejected_out: list | None = None) -> list[dict]:
    """One row per school-out day, newest information merged, plan attached.

    A company holiday is MARKED, never dropped. A day that silently disappears is how a
    parent ends up at work with two kids at home.
    """
    merged = merge_rows(rows, rejected_out)
    out = []
    for iso in sorted(merged):
        if today and iso < today:
            continue
        slot = merged[iso]
        plan = plans.get(iso, {})
        full_kids = sorted(slot[FULL], key=lambda k: KIDS.index(k))
        early_kids = sorted(slot[EARLY], key=lambda k: KIDS.index(k))
        out.append({
            "day": iso,
            "weekday": _d(iso).strftime("%a"),
            "kind": FULL if full_kids else EARLY,
            "full_kids": full_kids,
            "early_kids": early_kids,
            "titles": {**slot[FULL], **slot[EARLY]},
            "who": " + ".join(full_kids or early_kids),
            "company_holiday": iso in holidays,
            "holiday_label": holidays.get(iso),
            "coverage": plan.get("coverage") or COVERAGE_TBD,
            "confirmation": plan.get("confirmation") or "proposed",
            # WHICH sitter, when coverage is Babysitter. Name + phone resolved by the caller
            # from the sitters roster; None when coverage is anyone else.
            "sitter_id": plan.get("sitter_id") if plan.get("coverage") == COVERAGE_BABYSITTER else None,
            "sitter": plan.get("sitter") if plan.get("coverage") == COVERAGE_BABYSITTER else None,
            "sitter_phone": plan.get("sitter_phone") if plan.get("coverage") == COVERAGE_BABYSITTER else None,
            "pto": plan.get("pto") or "none",
            "note": plan.get("note") or "",
            "gcal_event_id": plan.get("gcal_event_id"),
            "gcal_kind": plan.get("gcal_kind"),
        })
    return out


def collapse_runs(days: list[dict]) -> list[dict]:
    """Group consecutive weekdays that are the same plan into one calendar event.

    Winter break is one invite, not four. Two days only join when everything a reader would
    see is identical — same kids out, same kind, same coverage — otherwise the merged event
    would assert something about a day that is not true of it.
    """
    blocks: list[dict] = []
    for d in days:
        key = (d["kind"], tuple(d["full_kids"]), tuple(d["early_kids"]), d["coverage"],
               d.get("sitter_id"))
        if blocks:
            prev = blocks[-1]
            gap_ok = _next_weekday(_d(prev["end"])) == _d(d["day"])
            if prev["_key"] == key and gap_ok:
                prev["end"] = d["day"]
                prev["days"].append(d)
                continue
        blocks.append({"_key": key, "start": d["day"], "end": d["day"], "days": [d],
                       "kind": d["kind"], "full_kids": d["full_kids"],
                       "early_kids": d["early_kids"], "coverage": d["coverage"],
                       "sitter": d.get("sitter"), "sitter_phone": d.get("sitter_phone")})
    return blocks


def _short(iso: str) -> str:
    d = _d(iso)
    return d.strftime("%a ") + str(d.day)


def _run_label(start: str, end: str) -> str:
    """'Thu 26 – Fri 27', or 'Thu Dec 24 – Fri Jan 1' when the run crosses a month."""
    if start == end:
        return _short(start)
    a, b = _d(start), _d(end)
    if a.month == b.month:
        return f"{_short(start)} – {_short(end)}"
    return f"{a.strftime('%a %b')} {a.day} – {b.strftime('%a %b')} {b.day}"


def display_months(days: list[dict]) -> list[dict]:
    """The board the way a parent scans it: months, and inside each month one ROW per run
    of consecutive school days of the same kind — winter break is one line, not six.

    A parent, 8/29: "right now it just reads as a list… organized by month so I could
    easily scan", "if both kids are off then just say both kids, I don't need two rows",
    and "it needs to show days off versus half days easily". So:
      * a run joins consecutive weekdays of the same KIND (a day off never merges with a
        half day) regardless of which kid or what coverage — kids are the UNION, shown as
        "Both kids" when both, with a note when a kid's feed only covers part of the run
        (a preschool feed listed Dec 24 and Dec 28 but not the days between; that is the
        feed, not the school, and the note keeps it honest instead of quietly claiming or
        dropping days);
      * coverage / time off on the run are the common value, or "mixed" when the days differ;
      * a run that crosses a month is filed under the month it starts in.
    The per-day rows ride along under each run for overrides and for the calendar buttons.
    """
    runs: list[dict] = []
    for d in days:
        if runs and runs[-1]["kind"] == d["kind"] and _next_weekday(_d(runs[-1]["end"])) == _d(d["day"]):
            runs[-1]["end"] = d["day"]
            runs[-1]["days"].append(d)
        else:
            runs.append({"start": d["day"], "end": d["day"], "kind": d["kind"], "days": [d]})

    for r in runs:
        ds = r["days"]
        n = len(ds)
        per_kid = {k: sum(1 for d in ds if k in (d["full_kids"] or d["early_kids"])) for k in KIDS}
        kids = [k for k in KIDS if per_kid[k]]
        r["kids"] = kids
        r["who"] = family.kids_label(kids)
        r["partial"] = [f"{k}'s feed lists {per_kid[k]} of the {n} days" for k in kids if 0 < per_kid[k] < n]
        r["n"] = n
        r["label"] = _run_label(r["start"], r["end"])
        # reasons: distinct titles per kid, in first-seen order
        reasons: dict[str, list[str]] = {}
        for d in ds:
            for kid, title in d["titles"].items():
                if title not in reasons.setdefault(kid, []):
                    reasons[kid].append(title)
        r["reasons"] = [(k, reasons[k]) for k in KIDS if k in reasons]
        cov = {d["coverage"] for d in ds}
        pto = {d["pto"] for d in ds}
        notes = {d["note"] for d in ds if d["note"]}
        r["coverage"] = cov.pop() if len(cov) == 1 else "mixed"
        confirmations = {d.get('confirmation', 'proposed') for d in ds}
        r['confirmation'] = confirmations.pop() if len(confirmations) == 1 else 'mixed'
        r['unconfirmed'] = sum(d.get('confirmation') != 'confirmed' for d in ds)
        sit = {(d.get("sitter_id"), d.get("sitter")) for d in ds}
        r["sitter_id"], r["sitter"] = sit.pop() if len(sit) == 1 else (None, "mixed")
        r["pto"] = pto.pop() if len(pto) == 1 else "mixed"
        r["note"] = notes.pop() if len(notes) == 1 else ("" if not notes else "(differs by day)")
        r["on_calendar"] = sum(1 for d in ds if d["gcal_event_id"])
        r["company_holiday"] = all(d["company_holiday"] for d in ds)
        r["holiday_label"] = next((d["holiday_label"] for d in ds if d["holiday_label"]), None)
        r["tbd"] = sum(1 for d in ds if d["coverage"] == COVERAGE_TBD and not d["company_holiday"])
        r["days_csv"] = ",".join(d["day"] for d in ds)

    months: list[dict] = []
    for r in runs:
        key = r["start"][:7]
        if not months or months[-1]["key"] != key:
            months.append({"key": key, "label": _d(r["start"]).strftime("%B %Y"),
                           "short": _d(r["start"]).strftime("%b ’%y"), "runs": [],
                           "full": 0, "early": 0, "days": 0, "tbd": 0})
        m = months[-1]
        m["runs"].append(r)
        m["days"] += r["n"]
        m[FULL if r["kind"] == FULL else EARLY] += r["n"]
        m["tbd"] += r["tbd"]
    return months


def _next_weekday(d: date) -> date:
    nxt = d + timedelta(days=1)
    while not is_weekday(nxt):
        nxt += timedelta(days=1)
    return nxt


def event_summary(block: dict) -> str:
    who = " + ".join(block["full_kids"] or block["early_kids"])
    if block["kind"] == FULL:
        if block["coverage"] == COVERAGE_BABYSITTER:
            # "Babysitter off" reads as the sitter having a day off. Name them instead;
            # gcal._OURS_TITLE and _SELF both know the "Sitter" shape.
            name = f" {block['sitter']}" if block.get("sitter") else ""
            return f"Sitter{name} — {who} (school closed)"
        return f"{block['coverage']} off — {who} (school closed)"
    return f"Pickup — {who} (early dismissal)"


def coverage_label(block: dict) -> str:
    """Coverage as a sentence fragment, naming the sitter and their number when known."""
    if block.get("coverage") == COVERAGE_BABYSITTER and block.get("sitter"):
        phone = f" ({block['sitter_phone']})" if block.get("sitter_phone") else ""
        return f"Babysitter — {block['sitter']}{phone}"
    return block.get("coverage") or COVERAGE_TBD


def event_description(block: dict) -> str:
    lines = []
    if block["kind"] == FULL:
        lines.append("School closed. This day is covered by: " + coverage_label(block) + ".")
    else:
        lines.append("Early dismissal — someone needs to collect them mid-day. "
                     "This is a pickup, not a day off.")
        lines.append("Pickup covered by: " + coverage_label(block) + ".")
    lines.append("")
    seen = []
    for d in block["days"]:
        for kid, title in sorted(d["titles"].items()):
            entry = f"{d['day']} — {kid}: {title}"
            if entry not in seen:
                seen.append(entry)
    lines.extend(seen)
    lines.append("")
    lines.append("Created from the Family Manager days-off board.")
    return "\n".join(lines)


def fm_key(block: dict) -> str:
    """Stable identity for a block, so re-running can never create a second copy."""
    return f"{block['kind']}:{block['start']}:{block['end']}:{','.join(block['full_kids'] or block['early_kids'])}"


# ---------------------------------------------------------------- self-test


class _Row(dict):
    def keys(self):  # sqlite3.Row-compatible enough for _richness
        return super().keys()

    def __getitem__(self, k):
        return super().get(k)


def _row(date_, kid, type_, title, end=None):
    return _Row(event_date=date_, end_date=end, kid=kid, type=type_, title=title, details="")


def self_test() -> int:
    fails = []

    def check(name, cond):
        print(("  PASS  " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    # Every fixture below is the example family (Sam + Jordan; Ava at Maple Street
    # Elementary, Leo at Little Oaks Preschool), whatever the user's own household says.
    family.use_example()
    bind_household()

    # Labor Day arrives from four feeds; the board must show it once.
    rows = [
        _row("2026-09-07", "Leo", "closure", "Little Oaks Closure - Labor Day"),
        _row("2026-09-07", "Leo", "closure", "Little Oaks Preschool Closed - Labor Day"),
        _row("2026-09-07", "Leo", "closure", "Monday, September 7th - Little Oaks closed for Labor Day"),
        _row("2026-09-07", "Ava", "closure", "Labor Day - District Closed"),
    ]
    b = board(rows, {}, {})
    check("four feeds collapse to one day", len(b) == 1)
    check("both kids on that day", b[0]["full_kids"] == ["Ava", "Leo"])
    # The date-padded title is the longest raw, so raw length picks the padding, not the
    # information. Stripping the restated date makes the informative one win on merit.
    check("keeps the most descriptive title",
          b[0]["titles"]["Leo"] == "Little Oaks Preschool Closed - Labor Day")
    check("strips a restated date from the title",
          clean_title("Monday, September 7th - Little Oaks closed for Labor Day")
          == "Little Oaks closed for Labor Day")
    check("leaves a normal title alone",
          clean_title("Teachers' Convention - Schools Closed") == "Teachers' Convention - Schools Closed")

    # The rec center pool sentence: one row, a three-week span, 14 phantom "school closed" days.
    pool = ("Effective Thursday, August 20, at 12 pm, the pool will be closed for annual "
            "maintenance an")
    rej = []
    b = board([_row("2026-08-20", "Both", "closure", pool, end="2026-09-08")], {}, {},
              rejected_out=rej)
    check("newsletter prose never becomes a closed day", b == [])
    check("the rejected row is reported, not silently dropped",
          len(rej) == 1 and "prose" in rej[0][2])

    # Even a short amenity notice is not a school closure.
    ok, why = is_school_closure("Pool closed for maintenance")
    check("a facility closure is not a school closure", not ok and "facility" in why)
    # ...but a school closing its pool day is still a school closure.
    check("a school closure naming a facility still counts",
          is_school_closure("School closed - pool maintenance")[0])
    check("an ordinary closure passes", is_school_closure("Teachers' Convention - Schools Closed")[0])

    # The feedback loop: this board's own event, read back in, must not become the REASON.
    b = board([_row("2026-08-31", "Leo", "closure", "Little Oaks Closed"),
               _row("2026-08-31", "Leo", "closure", "Sam off - Leo (Little Oaks closed)")], {}, {})
    check("our own event never becomes the reason", b[0]["titles"]["Leo"] == "Little Oaks Closed")
    check("a pickup event we made is rejected too",
          not is_school_closure("Pickup - Ava (early dismissal)")[0])
    check("a real title starting with a name still passes",
          is_school_closure("Sam Elementary closed")[0])

    # Prose must not out-rank a real title when both describe the same day.
    b = board([_row("2026-08-31", "Leo", "closure", "Little Oaks Closed"),
               _row("2026-08-31", "Leo", "closure", pool)], {}, {})
    check("prose cannot beat a real title", b[0]["titles"]["Leo"] == "Little Oaks Closed")

    # A day carried by only one feed must survive.
    rows = [_row("2026-09-04", "Ava", "early_dismissal", "Early Dismissal")]
    b = board(rows, {}, {})
    check("single-feed early dismissal survives", len(b) == 1 and b[0]["kind"] == EARLY)

    # Closed beats early dismissal for the same kid on the same day.
    rows = [_row("2027-06-15", "Ava", "early_dismissal", "Early Dismissal"),
            _row("2027-06-15", "Ava", "closure", "Closed")]
    b = board(rows, {}, {})
    check("closure outranks early dismissal", b[0]["full_kids"] == ["Ava"] and not b[0]["early_kids"])

    # A company holiday is marked, never dropped.
    rows = [_row("2026-11-26", "Ava", "closure", "Thanksgiving Recess - District Closed")]
    b = board(rows, {}, {"2026-11-26": "Thanksgiving"})
    check("company holiday is kept", len(b) == 1)
    check("company holiday is marked", b[0]["company_holiday"] and b[0]["holiday_label"] == "Thanksgiving")

    # Coverage defaults to TBD, not to a parent.
    check("coverage defaults to TBD", b[0]["coverage"] == COVERAGE_TBD)

    # Weekend days inside a stored span are not school days.
    rows = [_row("2026-12-28", "Ava", "closure", "Winter Recess", end="2027-01-01")]
    b = board(rows, {}, {})
    check("span expands to weekdays only", [d["day"] for d in b] ==
          ["2026-12-28", "2026-12-29", "2026-12-30", "2026-12-31", "2027-01-01"])

    # Contiguous same-plan days collapse; a weekend gap does not break a Mon-Fri run.
    blocks = collapse_runs(b)
    check("winter recess collapses to one block", len(blocks) == 1)
    check("block spans the whole run", blocks[0]["start"] == "2026-12-28" and blocks[0]["end"] == "2027-01-01")

    # Different coverage must NOT be merged into one event.
    days = board([_row("2026-11-05", "Ava", "closure", "Teachers' Convention"),
                  _row("2026-11-06", "Ava", "closure", "Teachers' Convention")], {}, {})
    days[0]["coverage"], days[1]["coverage"] = "Sam", "Jordan"
    check("differing coverage stays separate", len(collapse_runs(days)) == 2)

    # A run separated by a real school day must not merge.
    days = board([_row("2026-11-03", "Ava", "closure", "Election Day"),
                  _row("2026-11-05", "Ava", "closure", "Teachers' Convention")], {}, {})
    check("gap of a school day breaks the run", len(collapse_runs(days)) == 2)

    # An early-dismissal event must never call itself a day off.
    blk = collapse_runs(board([_row("2026-09-15", "Ava", "early_dismissal", "Early Dismissal")], {}, {}))[0]
    summary = event_summary(blk)
    check("pickup event is not labeled off", "off" not in summary.lower() and "Pickup" in summary)
    check("pickup description says it is not a day off",
          "not a day off" in event_description(blk))

    # A full day names who is covering it.
    blk = collapse_runs(board([_row("2026-08-31", "Leo", "closure", "Little Oaks Closed")], {}, {}))[0]
    blk["coverage"] = "Sam"
    check("day-off event names the coverer", event_summary(blk).startswith("Sam off"))

    # Identity is stable across runs.
    check("fm_key is stable", fm_key(blk) == fm_key(blk))

    # The scannable board: months -> runs, "Both kids", days off vs half days kept apart.
    wb = board([
        _row("2026-11-25", "Ava", "early_dismissal", "Maple Street Early Dismissal"),
        _row("2026-11-25", "Leo", "early_dismissal", "Little Oaks half day"),
        _row("2026-11-26", "Ava", "closure", "Thanksgiving Recess - District Closed"),
        _row("2026-11-26", "Leo", "closure", "Thanksgiving holiday"),
        _row("2026-11-27", "Ava", "closure", "Thanksgiving Recess - District Closed"),
        _row("2026-11-27", "Leo", "closure", "Thanksgiving holiday"),
        _row("2026-12-24", "Ava", "closure", "Christmas - District Closed"),
        _row("2026-12-24", "Leo", "closure", "Little Oaks NO SCHOOL"),
        _row("2026-12-25", "Ava", "closure", "Christmas - District Closed"),
        _row("2026-12-28", "Ava", "closure", "Winter Recess - District Closed"),
        _row("2026-12-28", "Leo", "closure", "Little Oaks NO SCHOOL"),
        _row("2026-12-29", "Ava", "closure", "Winter Recess - District Closed"),
        _row("2026-12-30", "Ava", "closure", "Winter Recess - District Closed"),
        _row("2026-12-31", "Ava", "closure", "Winter Recess - District Closed"),
        _row("2027-01-01", "Ava", "closure", "New Year's Day - District Closed"),
        _row("2027-01-18", "Ava", "closure", "MLK Day - District Closed"),
    ], {"2026-11-26": {"coverage": "Sam"}, "2026-11-27": {"coverage": "Jordan"}}, {})
    months = display_months(wb)
    check("months in order", [m["key"] for m in months] == ["2026-11", "2026-12", "2027-01"])
    nov = months[0]
    check("half day never merges with a day off", len(nov["runs"]) == 2 and nov["runs"][0]["kind"] == EARLY and nov["runs"][1]["kind"] == FULL)
    check("two-day Thanksgiving is ONE row", nov["runs"][1]["n"] == 2 and nov["runs"][1]["label"] == "Thu 26 – Fri 27")
    check("both kids reads as Both kids", nov["runs"][1]["who"] == "Both kids" and nov["runs"][0]["who"] == "Both kids")
    check("mixed coverage is named", nov["runs"][1]["coverage"] == "mixed" and nov["runs"][1]["tbd"] == 0)
    check("month counts split the kinds", nov["full"] == 2 and nov["early"] == 1 and nov["days"] == 3)
    dec = months[1]
    check("winter break is ONE row across the month edge", len(dec["runs"]) == 1 and dec["runs"][0]["n"] == 7
          and dec["runs"][0]["label"] == "Thu Dec 24 – Fri Jan 1")
    check("partial feed coverage is disclosed, not hidden", dec["runs"][0]["who"] == "Both kids"
          and dec["runs"][0]["partial"] == ["Leo's feed lists 2 of the 7 days"])
    check("reasons dedupe per kid", dict(dec["runs"][0]["reasons"])["Ava"] == [
        "Christmas - District Closed", "Winter Recess - District Closed", "New Year's Day - District Closed"])
    check("run filed under its start month", months[2]["runs"][0]["label"] == "Mon 18" and months[2]["days"] == 1)
    check("unassigned counted per month", dec["tbd"] == 7 and months[2]["tbd"] == 1)
    check("days_csv drives the bulk tick", dec["runs"][0]["days_csv"].split(",")[0] == "2026-12-24"
          and len(dec["runs"][0]["days_csv"].split(",")) == 7)

    print(("\nSELF-TEST FAILED: " + ", ".join(fails)) if fails else "\nALL PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
