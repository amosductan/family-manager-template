"""Deterministic extraction: text -> typed, dated school events.

First pass is regex/keyword only (per SCOPING: "deterministic date/regex first
pass; model only where regex can't"). The optional Fable 5 pass is gated behind
FM_USE_MODEL=1 and is OFF by default — cost-watched.
"""
import os
import re
import json
from datetime import datetime, date

MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}
MONTHS.update({m[:3].lower(): v for m, v in list(MONTHS.items())})

# "June 10th", "June 10", "Jun 10, 2026", "6/10", "6/10/26"
DATE_RX = re.compile(
    r"\b(?:(?P<mname>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s*(?P<year>20\d{2}))?"
    r"|(?P<mnum>\d{1,2})/(?P<dnum>\d{1,2})(?:/(?P<ynum>\d{2,4}))?)\b",
    re.IGNORECASE,
)

TYPE_RULES = [
    ("closure", ["school closed", "no school", "closed for", "closure", "district closed"]),
    ("early_dismissal", ["early dismissal", "dismissed early", "half day", "half-day"]),
    ("deadline", ["deadline", "due by", "due on", "last day to", "register by",
                  "rsvp by", "order by", "sign up by", "submit by"]),
    ("meeting", ["pta meeting", "general meeting", "board meeting", "conference",
                 "back to school night", "orientation"]),
    ("camp", ["camp week", "summer camp"]),
    ("event", ["event", "night", "dance", "fair", "picnic", "bbq", "trip",
               "assembly", "concert", "celebration", "spirit"]),
]


def classify_type(text: str) -> str:
    low = text.lower()
    for etype, needles in TYPE_RULES:
        if any(n in low for n in needles):
            return etype
    return "info"


def parse_dates(text: str, default_year_anchor: date | None = None) -> list[str]:
    """All parseable dates in text as YYYY-MM-DD.

    Year-less dates get the school-year-aware year: pick the year that puts the
    date within -30/+330 days of the anchor (email date), so a "September 3"
    mentioned in an August email lands in the right year.
    """
    anchor = default_year_anchor or date.today()
    out = []
    for m in DATE_RX.finditer(text):
        try:
            if m.group("mname"):
                month = MONTHS[m.group("mname").lower()[:3]]
                day = int(m.group("day"))
                year = int(m.group("year")) if m.group("year") else None
            else:
                month = int(m.group("mnum"))
                day = int(m.group("dnum"))
                y = m.group("ynum")
                year = int(y) + 2000 if y and len(y) == 2 else (int(y) if y else None)
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue
            if year is None:
                cand = date(anchor.year, month, day)
                delta = (cand - anchor).days
                if delta < -30:
                    cand = date(anchor.year + 1, month, day)
                elif delta > 330:
                    cand = date(anchor.year - 1, month, day)
                year = cand.year
            out.append(date(year, month, day).isoformat())
        except ValueError:
            continue
    # de-dupe, preserve order
    seen = set()
    return [d for d in out if not (d in seen or seen.add(d))]


def split_blocks(text: str) -> list[str]:
    """Split newsletter/PDF text into candidate announcement blocks."""
    blocks = re.split(r"\n\s*\n|\r\n\s*\r\n", text)
    return [b.strip() for b in blocks if len(b.strip()) > 30]


STOPWORDS = {
    "and", "or", "the", "at", "on", "of", "to", "in", "for", "per", "week",
    "am", "pm", "x", "schedule", "sundays", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "mondays", "tuesdays",
    "wednesdays", "thursdays", "fridays", "saturdays",
}


TITLE_CAP = 90


def informative_title(title: str) -> bool:
    """Reject titles that are just dates/times/schedule fragments
    ("* July 13 & 15 | August 3 & 5", "Thursday, July 30, 7:30-8:30 pm")."""
    words = re.findall(r"[A-Za-z]{3,}", title)
    real = [w for w in words if w.lower() not in STOPWORDS
            and w.lower()[:3] not in MONTHS]
    return len(real) >= 2


def is_prose(first_line: str) -> bool:
    """True when a block's first line is a PARAGRAPH, not a heading.

    This gate exists because of a real, expensive false positive: a rec center newsletter
    sentence — "Effective Thursday, August 20, at 12 pm, the pool will be closed for
    annual maintenance and will reopen..." — became an event whose type was `closure`
    and whose two parsed dates became a three-week span. That single row rendered as
    FOURTEEN "school closed, both kids" days on the coverage board, including both
    children's actual first day of school.

    A heading is short. A sentence long enough to be truncated at TITLE_CAP is not a
    title, and nothing downstream can tell the difference once it is stored.
    """
    line = first_line.strip()
    if len(line) > TITLE_CAP:
        return True
    # A heading does not run to a dozen-plus words with mid-sentence commas.
    return len(line.split()) > 14 and "," in line


def extract_events(text: str, anchor: date | None = None) -> list[dict]:
    """Deterministic pass: one event per dated block."""
    events = []
    for block in split_blocks(text):
        dates = parse_dates(block, anchor)
        if not dates:
            continue
        first_line = block.splitlines()[0].strip()
        if is_prose(first_line):
            continue
        title = re.sub(r"\s+", " ", first_line)[:TITLE_CAP]
        if not informative_title(title):
            continue
        events.append({
            "type": classify_type(block),
            "title": title,
            "event_date": dates[0],
            "end_date": dates[1] if len(dates) > 1 else None,
            "details": re.sub(r"\s+", " ", block)[:600],
        })
    return events



def claude_extract(text: str, anchor: date | None = None) -> list[dict]:
    """Optional model pass (llm.py). OFF unless FM_USE_MODEL=1 (FM_USE_CLAUDE=1 still works)."""
    if "1" not in (os.environ.get("FM_USE_MODEL"), os.environ.get("FM_USE_CLAUDE")):
        return []
    prompt = (
        "Extract school events from this email/newsletter text. Return ONLY a JSON "
        "array; each item: {\"type\": one of closure|early_dismissal|event|deadline|"
        "meeting|camp|info, \"title\": short, \"event_date\": YYYY-MM-DD or null, "
        "\"end_date\": YYYY-MM-DD or null, \"details\": <=400 chars}. "
        f"Email date for year inference: {(anchor or date.today()).isoformat()}.\n\n"
        + text[:12000]
    )
    try:
        import llm
        r = llm.complete(prompt, purpose="extract", timeout=180)
        if not r.ok:
            return []
        raw = r.text
        start, end = raw.find("["), raw.rfind("]")
        if start >= 0 and end > start:
            items = json.loads(raw[start:end + 1])
            return [i for i in items if isinstance(i, dict) and i.get("title")]
    except Exception:
        pass
    return []
