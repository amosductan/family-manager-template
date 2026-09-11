"""Ask Family Manager a question in plain words -- or tell it what to change.

"what time is back to school night?"                  -> an answer, with the entries it came from
"add Leo's swimming at the rec center, $45 a week"     -> a PROPOSED change the parent confirms
<attach a card statement> "what did we pay the rec center?" -> the charges found, and proposals

Why this exists: a parent opened the app to find one time, walked /, /days-off, /events
(three filters) and /checklist, hit a 500, and still hadn't seen it. The answer was in the
store the whole time. A parent shouldn't have to know which page holds a fact; they should
be able to ask -- and to say what they need instead of hand-typing it, by voice, and to
hand it a document and have it pull out the details.

The rules it keeps:
- RETRIEVAL FIRST, from the app's own tables only. The question is tokenized, every
  candidate row is scored on word overlap with its title/details, and the top matches --
  plus what is coming up in the next ten days, open asks, the checklist, the days-off plan,
  sitters, payments and routines -- are laid out as a compact text context,
  each row carrying its #id. The model never sees the whole database and never invents a
  source: every fact it can cite is a row we hand it, shown under the answer.
- THE MODEL NEVER WRITES. It may end its answer with an ACTIONS block: a JSON list of
  proposed changes drawn from a fixed menu (add/update a payment, add/tick a checklist item,
  add/dismiss an event). The page shows each as a card with an Apply button; nothing touches
  the database until a parent taps it, and every applied action records who, when, and the
  row's state BEFORE the change (ask_log.actions).
- DOCUMENTS stay on this box. Text is extracted locally (pypdf / docx / xlsx / text; images
  and scanned PDFs are transcribed by the same model with only its Read tool), 12+ digit
  runs are masked to their last four BEFORE anything is sent, and what is sent goes only
  through the household's model (llm.py). The file is kept under data/ask/<id>/.
- A FAILED MODEL CALL IS NOT AN EMPTY ANSWER. If the model doesn't answer, the page says why
  and still shows the matching rows.
- Runs on a thread; the page polls /ask/<id>.json with a staged progress line.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import db
import family

import llm  # noqa: E402 -- which model answers is the household's choice (llm.py, FM_LLM_PROVIDER)
TIMEOUT = 200
MAX_UPLOAD = 15 * 1024 * 1024
MAX_DOC_CHARS = 45000

SCHEMA = """
CREATE TABLE IF NOT EXISTS ask_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asked_at    TEXT NOT NULL,
    who         TEXT,
    question    TEXT NOT NULL,
    status      TEXT NOT NULL,          -- working | done | failed
    stage       TEXT,
    answer      TEXT,
    sources     TEXT,                   -- JSON list of the rows the model was shown
    error       TEXT,
    model       TEXT,
    seconds     REAL
);
"""
# Columns added after the first version shipped (9/5 morning -> 9/5 afternoon).
EXTRA_COLS = [("actions", "TEXT"), ("document", "TEXT")]

STOP = set("""a an and are as at be but by can could did do does for from get has have how i if in
is it its me my of on or our should that the their them there these this to was we were what when
where which who why will with would you your tell show know need want find give any there's whats
what's does time day date next week weeks coming upcoming soon today tomorrow tonight this add
update change set record please make put new mark""".split())

# Words parents use that the source rows spell differently. The household's own words (a
# kid's name -> their school, grade and nicknames) are added by _synonyms() from family.
SYNONYMS = {
    "bts": ["back to school", "back-to-school"],
    "back": ["back to school", "back-to-school", "bts"],
    "school": ["school"],
    "off": ["closed", "closure", "no school", "days off", "recess", "holiday"],
    "closed": ["closed", "closure", "no school", "recess", "holiday"],
    "holiday": ["closed", "closure", "recess", "holiday"],
    "sitter": ["sitter", "babysitter", "coverage"],
    "babysitter": ["sitter", "babysitter", "coverage"],
    "lunch": ["lunch", "menu"],
    "menu": ["lunch", "menu", "breakfast"],
    "zoom": ["zoom", "virtual"],
    "night": ["night", "evening"],
    "conference": ["conference", "conferences", "parent-teacher", "parent teacher"],
    "conferences": ["conference", "conferences", "parent-teacher", "parent teacher"],
    "picture": ["picture day", "photo"],
    "early": ["early dismissal", "dismissal"],
    "dismissal": ["early dismissal", "dismissal", "1:15"],
    "trip": ["trip", "field trip", "travel"],
    "pay": ["pay", "payment", "tuition", "due"],
    "paid": ["pay", "payment", "tuition", "charge"],
    "tuition": ["tuition", "payment", "invoice"],
    "swim": ["swim", "swimming", "lesson"],
    "swimming": ["swim", "swimming", "lesson"],
    "camp": ["camp"],
}


def _synonyms() -> dict:
    """SYNONYMS plus the household's own: a kid's name also finds their school's full and
    short name, grade and aliases, and "school" finds every school's short name. Built at
    call time so a reloaded household is honored."""
    out = {k: list(v) for k, v in SYNONYMS.items()}
    for k in family.CONFIG.get("kids") or []:
        name = (k.get("name") or "").strip()
        if not name:
            continue
        words = [name.lower()] + [str(a).lower() for a in (k.get("aliases") or [])]
        for key in ("school", "school_short", "grade"):
            if (k.get(key) or "").strip():
                words.append(k[key].strip().lower())
        out[name.lower()] = list(dict.fromkeys(words))
        if (k.get("school_short") or "").strip():
            out["school"].append(k["school_short"].strip().lower())
    out["school"] = list(dict.fromkeys(out["school"]))
    return out


def kid_values() -> list[str]:
    """What a proposed action's `kid` may hold: a kid's name, or "Both"."""
    return list(family.KIDS) + ["Both"]

# ---------------------------------------------------------------------------
# The menu of changes the model may PROPOSE (never apply)
# ---------------------------------------------------------------------------
PAYMENT_FIELDS = ("name", "activity", "organization", "kid", "category", "amount", "cadence",
                  "due_rule", "next_due", "autopay", "notes", "active")
CADENCES = ("weekly", "biweekly", "monthly", "per-session", "semester", "annual", "one-time")
PAY_CATEGORIES = ("tuition", "camp", "aftercare", "activity", "other")
CHECKLIST_FIELDS = ("title", "detail", "kid", "category", "due_date")
EVENT_FIELDS = ("kid", "title", "event_date", "end_date", "start_time", "end_time", "details")

ACTION_KINDS = {
    "add_payment": PAYMENT_FIELDS,
    "update_payment": ("id",) + PAYMENT_FIELDS,
    "add_checklist": CHECKLIST_FIELDS,
    "tick_checklist": ("id",),
    "add_event": EVENT_FIELDS,
    "dismiss_event": ("id",),
}


def ensure(con) -> None:
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(ask_log)")}
    for name, decl in EXTRA_COLS:
        if name not in have:
            con.execute(f"ALTER TABLE ask_log ADD COLUMN {name} {decl}")
    con.commit()


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _tokens(q: str) -> list[str]:
    # A possessive is the commonest way a kid's name arrives ("Leo's swim lesson"), and
    # "leo's" would never match the synonyms keyed on "leo".
    words = re.findall(r"[a-z0-9][a-z0-9'.\-]*", q.lower().replace("’", "'"))
    words = [re.sub(r"'s$", "", w).strip(".'-") for w in words]
    return [w for w in words if w and w not in STOP]


def _needles(q: str) -> list[str]:
    """The strings we look for in a row: the question's own words, their synonyms, and the
    question's 2-word phrases (a phrase hit is worth more than a word hit)."""
    toks = _tokens(q)
    syn = _synonyms()
    out: list[str] = []
    for t in toks:
        out.append(t)
        out.extend(syn.get(t, []))
    for a, b in zip(toks, toks[1:]):
        out.append(f"{a} {b}")
    seen, uniq = set(), []
    for n in out:
        if n not in seen and len(n) >= 3:
            seen.add(n)
            uniq.append(n)
    return uniq


def _score(text: str, needles: list[str]) -> int:
    t = (text or "").lower()
    s = 0
    for n in needles:
        if n in t:
            s += 3 if " " in n else 1
    return s


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower())[:60]


def _event_dict(r) -> dict:
    return {
        "kind": "event", "id": r["id"], "kid": r["kid"], "date": r["event_date"],
        "end": r["end_date"], "time": r["start_time"], "end_time": r["end_time"],
        "title": (r["title"] or "").strip(), "details": (r["details"] or "").strip()[:400],
        "source": r["source"], "category": r["category"],
    }


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%a %b %d, %Y")
    except ValueError:
        return iso


def _fmt_time(t: str | None) -> str:
    if not t:
        return ""
    try:
        return datetime.strptime(t[:5], "%H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return t


def _event_line(d: dict) -> str:
    when = _fmt_date(d["date"])
    if d.get("end") and d["end"] != d["date"]:
        when += f" to {_fmt_date(d['end'])}"
    if d.get("time"):
        when += f" {_fmt_time(d['time'])}"
        if d.get("end_time"):
            when += f"-{_fmt_time(d['end_time'])}"
    else:
        when += " (no time on this copy)"
    line = f"#{d['id']} [{d['kid']}] {when}: {d['title']} (source: {d['source']})"
    if d.get("details"):
        line += f" -- {d['details'][:220]}"
    return line


def _payment_line(p) -> str:
    amt = f"${p['amount']:.2f}" if p["amount"] is not None else "amount TBD"
    bits = [f"#{p['id']} {p['name']} ({p['kid'] or 'Both'}, {p['category']})"]
    if p["activity"]:
        bits.append(f"activity: {p['activity']}")
    if p["organization"]:
        bits.append(f"organization: {p['organization']}")
    bits.append(amt)
    bits.append(f"cadence: {p['cadence'] or '?'}" + (f", due {p['due_rule']}" if p["due_rule"] else ""))
    if p["next_due"]:
        bits.append(f"next date {_fmt_date(p['next_due'])}")
    if p["autopay"]:
        bits.append("autopay")
    if not p["active"]:
        bits.append("INACTIVE")
    if p["notes"]:
        bits.append(f"notes: {p['notes'][:160]}")
    return "; ".join(bits)


def retrieve(con, question: str, today: date | None = None) -> dict:
    """Everything the model is allowed to know for this question. Returns
    {'sections': [(heading, [line, ...]), ...], 'sources': [row dict, ...]}."""
    today = today or date.today()
    t0 = today.isoformat()
    needles = _needles(question)
    ql = question.lower()
    far_back = (today - timedelta(days=400 if re.search(r"last year|202[0-5]", ql) else 45)).isoformat()
    far_fwd = (today + timedelta(days=420)).isoformat()

    sections: list[tuple[str, list[str]]] = []
    sources: list[dict] = []

    # --- events: scored matches + the next ten days -------------------------------------
    rows = con.execute(
        "SELECT * FROM events WHERE status='active' AND event_date BETWEEN ? AND ? "
        "ORDER BY event_date, start_time IS NOT NULL, start_time", (far_back, far_fwd)).fetchall()
    scored = []
    for r in rows:
        s = _score(f"{r['title']} {r['details'] or ''}", needles)
        if s:
            scored.append((s, r))
    # Score first; among equals, what is AHEAD beats what is behind (a "when is"
    # question is almost always about the next one), nearest first on each side.
    scored.sort(key=lambda x: (-x[0], x[1]["event_date"] < t0,
                               abs((datetime.strptime(x[1]["event_date"][:10], "%Y-%m-%d").date() - today).days)))
    matched, seen = [], set()
    for s, r in scored:
        key = (r["kid"], r["event_date"], _norm_title(r["title"]))
        if key in seen:
            continue
        seen.add(key)
        matched.append(r)
        if len(matched) >= 30:
            break
    if matched:
        lines = []
        for r in sorted(matched, key=lambda r: (r["event_date"], r["start_time"] or "")):
            d = _event_dict(r)
            sources.append(d)
            lines.append(_event_line(d))
        sections.append(("Entries matching the question (every copy we hold, from every feed)", lines))

    soon_end = (today + timedelta(days=10)).isoformat()
    soon = [r for r in rows if t0 <= r["event_date"] <= soon_end]
    seen2, lines = set(), []
    for r in soon:
        key = (r["kid"], r["event_date"], _norm_title(r["title"]))
        if key in seen2:
            continue
        seen2.add(key)
        lines.append(_event_line(_event_dict(r)))
        if len(lines) >= 45:
            break
    if lines:
        sections.append(("Coming up in the next 10 days", lines))

    # --- open asks from school mail -------------------------------------------------------
    try:
        acts = db.open_actions(con, limit=60, today=t0)
        picked = [a for a in acts if _score(f"{a['text']} {a['subject']}", needles)]
        due_soon = [a for a in acts if a["due"] and t0 <= a["due"] <= (today + timedelta(days=21)).isoformat()]
        merged, ids = [], set()
        for a in picked + due_soon:
            if a["id"] not in ids:
                ids.add(a["id"])
                merged.append(a)
        lines = []
        for a in merged[:25]:
            lines.append(f"#{a['id']} [{a['kid'] or 'Both'}] {a['text']}" + (f" -- due {_fmt_date(a['due'])}" if a["due"] else "")
                         + f" (from: {a['subject']})")
            sources.append({"kind": "action", "id": a["id"], "kid": a["kid"], "date": a["due"],
                            "title": a["text"], "details": a["subject"], "source": a["source"]})
        if lines:
            sections.append(("Open asks from school mail (still need a parent)", lines))
    except Exception:
        pass

    # --- shared checklist ----------------------------------------------------------------
    try:
        cl = con.execute("SELECT * FROM checklist WHERE status != 'na' ORDER BY kid, category, sort").fetchall()
        lines = [f"#{c['id']} [{c['kid']}/{c['category']}] {c['title']} -- {c['status']}"
                 + (f", due {_fmt_date(c['due_date'])}" if c["due_date"] else "")
                 + (f", done by {c['done_by']}" if c["done_by"] else "")
                 for c in cl if c["status"] != "done" or _score(c["title"], needles)]
        if lines:
            sections.append(("Shared checklist (open unless marked done); categories: supplies, admin, activity, health, other", lines[:40]))
    except Exception:
        pass

    # --- days-off coverage plan ----------------------------------------------------------
    try:
        plans = con.execute(
            "SELECT p.*, s.name AS sitter FROM coverage_plan p LEFT JOIN sitters s ON s.id = p.sitter_id "
            "WHERE p.day BETWEEN ? AND ? ORDER BY p.day", (t0, (today + timedelta(days=60)).isoformat())).fetchall()
        lines = [f"{_fmt_date(p['day'])}: {p['coverage'] or '?'}"
                 + f" — arrangement {p['confirmation'] or 'proposed'} (only confirmed means agreed)"
                 + (f" ({p['sitter']})" if p["sitter"] else "")
                 + (f", PTO: {p['pto']}" if p["pto"] else "")
                 + (f" -- {p['note']}" if p["note"] else "") for p in plans]
        if lines:
            sections.append(("Days-off coverage plan, next 60 days", lines))
    except Exception:
        pass

    # --- sitters, payments, trips, routines ------------------------------------------------
    try:
        sit = con.execute("SELECT * FROM sitters WHERE active=1 ORDER BY sort, name").fetchall()
        lines = [f"{s['name']}" + (f", {s['phone']}" if s["phone"] else "") + (f", {s['rate']}" if s["rate"] else "")
                 + (f", kids ok: {s['kids_ok']}" if s["kids_ok"] else "") + (f", availability: {s['availability']}" if s["availability"] else "")
                 for s in sit]
        if lines:
            sections.append(("Sitters", lines))
    except Exception:
        pass
    try:
        pay = con.execute("SELECT * FROM payments ORDER BY active DESC, kid, name").fetchall()
        lines = [_payment_line(p) for p in pay]
        if lines:
            sections.append(("Payments & activities registry (ALL rows, with #id; cadences: weekly, biweekly, "
                             "monthly, per-session, semester, annual, one-time; categories: tuition, camp, "
                             "aftercare, activity, other)", lines))
    except Exception:
        pass
    try:
        trips = con.execute("SELECT * FROM trips ORDER BY start_date").fetchall()
        lines = [f"{t['name']} -- {t['destination']}, {_fmt_date(t['start_date'])} to {_fmt_date(t['end_date'])}"
                 + (f" ({t['party']})" if t["party"] else "") for t in trips]
        if lines:
            sections.append(("Trips", lines))
    except Exception:
        pass
    try:
        import gatherings
        lines = gatherings.ask_lines(con, today)
        if lines:
            sections.append(("Gatherings being planned (parties, visits; plan tallies and RSVPs counted from the rows)", lines))
    except Exception:
        pass
    try:
        wd_today, wd_tom = today.strftime("%A"), (today + timedelta(days=1)).strftime("%A")
        rout = con.execute("SELECT * FROM kid_routines ORDER BY kid, weekday, sort").fetchall()
        lines = [f"{r['kid']} {r['weekday']}: {r['label']}" + (f" -- {r['note']}" if r["note"] else "")
                 for r in rout if r["weekday"] in (wd_today, wd_tom) or _score(f"{r['label']} {r['note'] or ''}", needles)]
        if lines:
            sections.append(("Weekly routines (today, tomorrow, and any that match)", lines[:30]))
    except Exception:
        pass

    import household
    if household.H:
        shared = household.collect(con, today)
        shared.sort(key=lambda t: -_score(t['title'] + ' ' + (t['owner'] or ''), needles))
        lines = [f"{t['key']}: {t['title']} — {t['state']}; owner {t['owner'] or 'Unassigned'}; "
                 f"stage {t['stage']}; due {t['due'] or 'unknown'}; reminder {t['snooze_until'] or 'none'}; source {t['href']}"
                 for t in shared[:35]]
        sections.append(('Shared actions and parent handoffs (change these at /actions)', lines))
        transition_lines = [f"{p['name']}: starts {p['starts'] or 'unknown'}, ends {p['ends'] or 'unknown'}, "
                            f"price changes {p['change_date'] or 'unknown'} to {p['expected_amount'] if p['expected_amount'] is not None else 'unknown'}; "
                            f"{p['change_note'] or ''}. Details: /payment-plans#payment-{p['id']}"
                            for p in household.plans(con, today) if p['starts'] or p['ends'] or p['change_date']]
        if transition_lines:
            sections.append(('Recorded payment transitions; amounts are expectations, not proof of payment', transition_lines))
        review = con.execute('SELECT * FROM household_reviews ORDER BY week DESC LIMIT 1').fetchone()
        if review:
            sections.append(('Latest shared weekly plan', [f"Week {review['week']}, saved by {review['updated_by']}: {review['plan']}"]))
    return {"sections": sections, "sources": sources, "needles": needles}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def mask_numbers(text: str) -> str:
    """Card / account numbers never leave this box: any run of 12+ digits (spaces or dashes
    allowed) becomes ****last4. Short numbers (amounts, dates, phone) are untouched."""
    return re.sub(r"\d(?:[ -]?\d){11,}", lambda m: "****" + re.sub(r"\D", "", m.group(0))[-4:], text or "")


def _safe_filename(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "document").strip())[:80] or "document"
    stem = base.upper().split(".")[0]
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.match(r"^(COM|LPT)\d$", stem):
        base = "_" + base
    return base


def read_document(ask_id: int, filename: str, data: bytes, progress=None) -> dict:
    """Save the upload under data/ask/<id>/ and turn it into masked text. Returns the doc
    record that goes into ask_log.document: {name, kind, path, chars, status, why, text}."""
    import mailsweep
    folder = db.DATA_DIR / "ask" / str(ask_id)
    folder.mkdir(parents=True, exist_ok=True)
    kind = mailsweep._kind_for("", filename, data[:16])
    path = folder / _safe_filename(filename)
    path.write_bytes(data)
    doc = {"name": filename, "kind": kind, "path": str(path), "chars": 0, "status": "ok", "why": ""}
    text = ""
    if kind == "image":
        if progress:
            progress("Transcribing the image")
        text, why = mailsweep.transcribe_image(str(path))
        if not text:
            doc.update(status="failed", why=why)
    else:
        text, status = mailsweep.extract_text(kind, data)
        if status != "ok":
            doc.update(status="failed", why=status)
        if kind == "pdf" and len((text or "").strip()) < 200:
            # A scanned statement: no text layer. Render the first pages and read them.
            pages = _render_pdf_pages(path, limit=4)
            if pages:
                if progress:
                    progress(f"Transcribing {len(pages)} scanned page(s)")
                parts = []
                for pg in pages:
                    t, why = mailsweep.transcribe_image(str(pg))
                    if t:
                        parts.append(t)
                if parts:
                    text = "\n\n".join(parts)
                    doc.update(status="ok", why="scanned; transcribed")
    text = mask_numbers(text or "")
    doc["chars"] = len(text)
    doc["text"] = text[:MAX_DOC_CHARS]
    if len(text) > MAX_DOC_CHARS:
        doc["why"] = f"truncated to {MAX_DOC_CHARS} of {len(text)} characters"
    return doc


def _render_pdf_pages(path: Path, limit: int = 4) -> list[Path]:
    try:
        import fitz
    except Exception:
        return []
    out = []
    try:
        with fitz.open(str(path)) as pdf:
            for i, page in enumerate(pdf):
                if i >= limit:
                    break
                pix = page.get_pixmap(dpi=130)
                p = path.with_name(f"{path.stem}_p{i + 1}.png")
                pix.save(str(p))
                out.append(p)
    except Exception:
        return out
    return out


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

PROMPT = """You are the household assistant for [[FAMILY_NAME]]. Today is [[TODAY]] ([[WEEKDAY]]).
[[FAMILY]]

Answer the parent using ONLY the records below, which come from the family's own app (school
emails, the shared Google calendar, and things the parents typed in), plus any document the
parent attached. Rules for ANSWERING:
- Lead with the answer in one or two plain sentences: the date, the day of the week, the
  time, and where. Then, if useful, one short line of context.
- The records hold several copies of the same happening from different feeds. Treat copies
  on the same date as one thing; prefer the copy that carries a time. If copies DISAGREE on
  the date or time, say so plainly and show both -- never pick silently.
- If a copy says "details to follow" or no copy has a time, say the time is not in the
  records yet and name the feed most likely to send it.
- If the records do not answer the question, say "That's not in Family Manager" and say
  what the closest thing you found was. Never guess or use outside knowledge for dates.
- Never comment on the app's own setup, configuration, API keys or pages needing steps; you
  only know the records. If a record is missing, say what's missing, not why.
- Plain text. No markdown headers, no emoji, no bullet symbols other than "-". Keep it
  under 120 words unless the question genuinely needs a list. Use contractions. American English.

Rules for CHANGING things. When the parent asks you to add, record, update, change, mark
done, dismiss or remove something -- or attaches a document whose details belong in the
registry (a statement showing what an activity actually costs, an invoice, a schedule) --
you PROPOSE the change; a parent confirms it on the page. You never apply anything yourself.
- Say in plain words what you'll do (and what you found in the document, with the date and
  amount of each relevant charge), then end your reply with a line that is exactly
  ACTIONS:
  followed by ONE JSON object on the following lines: {"actions": [ ... ]}
- Each action is an object with "kind" and its fields. Kinds:
  add_payment: name, activity, organization, kid ([[KID_ENUM]]), category (tuition|camp|
    aftercare|activity|other), amount (number or null), cadence (weekly|biweekly|monthly|
    per-session|semester|annual|one-time), due_rule (text like "Mondays" / "the 1st" / null),
    next_due (YYYY-MM-DD or null), autopay (true|false), notes
  update_payment: id (the #id from the registry) plus ONLY the fields that change
  add_checklist: title, detail, kid, category (supplies|admin|activity|health|other), due_date
  tick_checklist: id
  add_event: kid, title, event_date (YYYY-MM-DD), end_date, start_time (HH:MM 24h), end_time, details
  dismiss_event: id
- Prefer update_payment over add_payment when a registry row already covers the same
  activity or organization. Leave an unknown amount null and say what's missing rather
  than guessing. Dates must be real dates; if the parent said "Mondays" that is a due_rule,
  not a next_due. Never propose more than 8 actions. If nothing should change, omit the
  ACTIONS line entirely.
- For a card statement or bank export: match charges to the organizations in the registry
  or named in the question (the schools, a rec center, a camp, a lesson studio...). Report each match
  with its date and amount. Propose update_payment amount only when the charge is clearly
  the recurring one; otherwise report the numbers and let the parent decide.

QUESTION: [[QUESTION]]
[[DOCUMENT]]
RECORDS:
[[RECORDS]]
"""


def build_prompt(question: str, ctx: dict, today: date, doc: dict | None = None) -> str:
    parts = []
    for heading, lines in ctx["sections"]:
        parts.append(f"## {heading}")
        parts.extend(f"- {ln}" for ln in lines)
        parts.append("")
    records = "\n".join(parts).strip() or "(no records matched)"
    doc_block = ""
    if doc and doc.get("text"):
        doc_block = (f"\nATTACHED DOCUMENT: {doc['name']} ({doc['kind']}, {doc['chars']} characters"
                     + (f", {doc['why']}" if doc.get("why") else "") + "). Long numbers are masked to their last four digits.\n"
                     "--- begin document ---\n" + doc["text"] + "\n--- end document ---\n")
    elif doc:
        doc_block = f"\nATTACHED DOCUMENT: {doc['name']} -- could not be read ({doc.get('why') or doc.get('status')}). Say so.\n"
    return (PROMPT.replace("[[FAMILY_NAME]]", family.FAMILY_NAME)
            .replace("[[FAMILY]]", family.prompt_context())
            .replace("[[KID_ENUM]]", "|".join(kid_values()))
            .replace("[[TODAY]]", today.isoformat())
            .replace("[[WEEKDAY]]", today.strftime("%A"))
            .replace("[[QUESTION]]", question.strip()[:500])
            .replace("[[DOCUMENT]]", doc_block)
            .replace("[[RECORDS]]", records[:60000]))


def call_model(prompt: str) -> tuple[str, str]:
    """(raw reply, error). Exactly one of them is non-empty. Goes through llm.complete,
    which uses the household's provider and records the call in the cost ledger."""
    r = llm.complete(prompt, purpose="ask", timeout=TIMEOUT)
    return (r.text, "") if r.ok else ("", f"the model didn't answer: {r.error}"[:300])


def parse_reply(raw: str) -> tuple[str, list[dict]]:
    """Split the model's reply into the answer text and the validated proposed actions."""
    text, actions = raw, []
    m = re.search(r"(?:^|\n)\s*ACTIONS:\s*\n", raw)
    if m:
        text = raw[:m.start()].rstrip()
        tail = raw[m.end():]
        tail = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", tail.strip())
        s, e = tail.find("{"), tail.rfind("}")
        if s >= 0 and e > s:
            try:
                data = json.loads(tail[s:e + 1])
                for a in (data.get("actions") or [])[:8]:
                    v = _validate_action(a)
                    if v:
                        actions.append(v)
            except Exception:
                text = raw  # keep the whole reply visible rather than lose the block silently
    # The page renders the answer as plain text, so stray markdown emphasis would show literally.
    text = text.replace("**", "").replace("__", "")
    return text.strip(), actions


def _validate_action(a: dict) -> dict | None:
    if not isinstance(a, dict):
        return None
    kind = a.get("kind")
    allowed = ACTION_KINDS.get(kind)
    if not allowed:
        return None
    out = {"kind": kind, "status": "proposed"}
    for f in allowed:
        if f in a and a[f] is not None:
            out[f] = a[f]
    if "id" in allowed:
        try:
            out["id"] = int(a.get("id"))
        except (TypeError, ValueError):
            return None
    if kind in ("add_payment", "update_payment"):
        if "amount" in out:
            try:
                out["amount"] = round(float(str(out["amount"]).replace("$", "").replace(",", "")), 2)
            except ValueError:
                out.pop("amount")
        if "cadence" in out and out["cadence"] not in CADENCES:
            out.pop("cadence")
        if "category" in out and out["category"] not in PAY_CATEGORIES:
            out["category"] = "other"
        if "kid" in out and out["kid"] not in kid_values():
            out["kid"] = "Both"
        if "autopay" in out:
            out["autopay"] = 1 if out["autopay"] in (True, 1, "true", "yes", "1") else 0
        if "active" in out:
            out["active"] = 1 if out["active"] in (True, 1, "true", "yes", "1") else 0
        if kind == "add_payment" and not out.get("name"):
            return None
    if kind == "add_checklist":
        if not out.get("title"):
            return None
        if out.get("kid") not in kid_values():
            out["kid"] = "Both"
        if out.get("category") not in ("supplies", "admin", "activity", "health", "other"):
            out["category"] = "other"
    if kind == "add_event":
        if not out.get("title") or not _iso_date(out.get("event_date")):
            return None
        if out.get("kid") not in kid_values():
            out["kid"] = "Both"
        for f in ("start_time", "end_time"):
            if f in out and not re.match(r"^\d{2}:\d{2}$", str(out[f])):
                out.pop(f)
    for f in ("next_due", "due_date", "end_date"):
        if f in out and not _iso_date(out[f]):
            out.pop(f)
    return out


def _iso_date(s) -> bool:
    try:
        datetime.strptime(str(s)[:10], "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def describe(a: dict) -> str:
    """One human line per proposed action, for the card and the history."""
    k = a.get("kind")
    if k == "add_payment":
        bits = [a.get("name", "?")]
        what = " · ".join(x for x in (a.get("activity"), a.get("organization")) if x)
        if what:
            bits.append(what)
        bits.append(f"${a['amount']:.2f}" if a.get("amount") is not None else "amount TBD")
        bits.append(a.get("cadence", "monthly") + (f" ({a['due_rule']})" if a.get("due_rule") else ""))
        if a.get("kid"):
            bits.append(a["kid"])
        return "Add payment: " + " — ".join(bits)
    if k == "update_payment":
        changes = ", ".join(f"{f}: {a[f]}" for f in PAYMENT_FIELDS if f in a)
        return f"Update payment #{a['id']}: {changes or 'no fields'}"
    if k == "add_checklist":
        return f"Add to checklist ({a.get('kid', 'Both')}/{a.get('category', 'other')}): {a.get('title')}" + (f", due {a['due_date']}" if a.get("due_date") else "")
    if k == "tick_checklist":
        return f"Mark checklist item #{a['id']} done"
    if k == "add_event":
        when = a.get("event_date", "") + (f" {a['start_time']}" if a.get("start_time") else "")
        return f"Add event ({a.get('kid', 'Both')}): {a.get('title')} on {when}"
    if k == "dismiss_event":
        return f"Dismiss event #{a['id']}"
    return k or "?"


def fallback_answer(ctx: dict) -> str:
    """What the page shows when the model is unavailable: the raw matches, dated, so the
    parent still gets the facts. Never an empty box."""
    ev = [s for s in ctx["sources"] if s["kind"] == "event"]
    if not ev:
        return "Nothing in Family Manager matched those words. Try the kid's name or a different word for it."
    lines = ["Claude is unavailable, so here's every matching entry as recorded:"]
    for d in ev[:12]:
        lines.append("- " + _event_line(d))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Applying a confirmed action
# ---------------------------------------------------------------------------

def apply_action(con, a: dict, who: str | None) -> dict:
    """Execute ONE confirmed action. Returns the action dict updated with status, message,
    applied_by/at and the row's BEFORE state where one existed. Raises nothing: a failure
    is a status the page shows."""
    k = a.get("kind")
    now = datetime.now().isoformat(timespec="seconds")
    try:
        if k == "add_payment":
            cur = con.execute(
                "INSERT INTO payments (name, activity, organization, kid, category, amount, cadence, due_rule, "
                "next_due, autopay, notes, active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.get("name"), a.get("activity"), a.get("organization"), a.get("kid", "Both"),
                 a.get("category", "other"), a.get("amount"), a.get("cadence", "monthly"), a.get("due_rule"),
                 a.get("next_due"), a.get("autopay", 0), a.get("notes", ""), a.get("active", 1)))
            a.update(status="applied", row_id=cur.lastrowid, link="/payments",
                     message=f"Added payment #{cur.lastrowid}")
        elif k == "update_payment":
            row = con.execute("SELECT * FROM payments WHERE id=?", (a["id"],)).fetchone()
            if not row:
                raise ValueError(f"payment #{a['id']} does not exist")
            fields = [f for f in PAYMENT_FIELDS if f in a]
            if not fields:
                raise ValueError("no fields to change")
            a["before"] = {f: row[f] for f in fields}
            con.execute(f"UPDATE payments SET {', '.join(f'{f}=?' for f in fields)} WHERE id=?",
                        (*[a[f] for f in fields], a["id"]))
            a.update(status="applied", row_id=a["id"], link="/payments",
                     message=f"Updated {row['name']}: " + ", ".join(f"{f} {a['before'][f]!r} -> {a[f]!r}" for f in fields))
        elif k == "add_checklist":
            cur = con.execute(
                "INSERT INTO checklist (title, detail, kid, category, due_date, status, source, sort) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (a["title"][:200], (a.get("detail") or "")[:500], a.get("kid", "Both"), a.get("category", "other"),
                 a.get("due_date"), "open", "ask", 999))
            a.update(status="applied", row_id=cur.lastrowid, link="/checklist",
                     message=f"Added checklist item #{cur.lastrowid}")
        elif k == "tick_checklist":
            row = con.execute("SELECT * FROM checklist WHERE id=?", (a["id"],)).fetchone()
            if not row:
                raise ValueError(f"checklist item #{a['id']} does not exist")
            if row["status"] == "blocked":
                raise ValueError(f"'{row['title']}' is blocked on {row['blocked_on']}; clear that first")
            a["before"] = {"status": row["status"], "done_by": row["done_by"], "done_at": row["done_at"]}
            con.execute("UPDATE checklist SET status='done', done_by=?, done_at=? WHERE id=?",
                        (who or "Ask", now, a["id"]))
            a.update(status="applied", row_id=a["id"], link="/checklist", message=f"Ticked '{row['title']}'")
        elif k == "add_event":
            kid = a.get("kid", "Both")
            cur = con.execute(
                "INSERT INTO events (kid, school, type, title, event_date, end_date, details, source, source_ref, "
                "status, created_at, start_time, end_time, category) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kid, None, "event", a["title"][:200], a["event_date"], a.get("end_date"), (a.get("details") or "")[:1000],
                 "ask", f"ask:{who or 'parent'}", "active", now, a.get("start_time"), a.get("end_time"),
                 "kids_school" if kid in family.KIDS else "other"))
            a.update(status="applied", row_id=cur.lastrowid, link="/events",
                     message=f"Added event #{cur.lastrowid} on {a['event_date']}")
        elif k == "dismiss_event":
            row = con.execute("SELECT * FROM events WHERE id=?", (a["id"],)).fetchone()
            if not row:
                raise ValueError(f"event #{a['id']} does not exist")
            a["before"] = {"status": row["status"]}
            con.execute("UPDATE events SET status='dismissed' WHERE id=?", (a["id"],))
            a.update(status="applied", row_id=a["id"], link="/events", message=f"Dismissed '{row['title']}'")
        else:
            raise ValueError(f"unknown action kind {k!r}")
        con.commit()
        a.update(applied_by=who or "parent", applied_at=now)
    except Exception as exc:
        con.rollback()
        a.update(status="failed", message=f"{type(exc).__name__}: {exc}"[:300])
    return a


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def _set(ask_id: int, **cols) -> None:
    con = db.connect()
    sets = ", ".join(f"{k}=?" for k in cols)
    con.execute(f"UPDATE ask_log SET {sets} WHERE id=?", (*cols.values(), ask_id))
    con.commit()
    con.close()


def start(question: str, who: str | None, upload: tuple[str, bytes] | None = None) -> int:
    con = db.connect()
    ensure(con)
    cur = con.execute("INSERT INTO ask_log (asked_at, who, question, status, stage, model) VALUES (?,?,?,?,?,?)",
                      (datetime.now().isoformat(timespec="seconds"), who, question.strip()[:500],
                       "working", "Reading the document" if upload else "Finding matching entries", llm.describe()))
    ask_id = cur.lastrowid
    con.commit()
    con.close()
    threading.Thread(target=_run, args=(ask_id, question, upload), daemon=True).start()
    return ask_id


def _run(ask_id: int, question: str, upload: tuple[str, bytes] | None = None) -> None:
    t = time.time()
    try:
        today = date.today()
        doc = None
        if upload:
            doc = read_document(ask_id, upload[0], upload[1], progress=lambda s: _set(ask_id, stage=s))
            _set(ask_id, document=json.dumps({k: v for k, v in doc.items() if k != "text"}),
                 stage="Finding matching entries")
        con = db.connect()
        ctx = retrieve(con, question, today)
        con.close()
        _set(ask_id, stage=f"Asking Claude ({len(ctx['sources'])} matching entries" + (", plus the document" if doc else "") + ")",
             sources=json.dumps(ctx["sources"][:60]))
        raw, err = call_model(build_prompt(question, ctx, today, doc))
        if raw:
            text, actions = parse_reply(raw)
            _set(ask_id, status="done", stage="Done", answer=text, error=None,
                 actions=json.dumps(actions) if actions else None, seconds=round(time.time() - t, 1))
        else:
            _set(ask_id, status="done", stage="Done (without Claude)", answer=fallback_answer(ctx),
                 error=err, seconds=round(time.time() - t, 1))
    except Exception as exc:  # the row must never stay 'working' forever
        _set(ask_id, status="failed", stage="Failed", error=f"{type(exc).__name__}: {exc}"[:300],
             seconds=round(time.time() - t, 1))


def apply(ask_id: int, idx: int | None, who: str | None) -> dict | None:
    """Apply one proposed action (idx) or every still-proposed one (idx None). Returns the
    updated ask row."""
    con = db.connect()
    ensure(con)
    r = con.execute("SELECT actions FROM ask_log WHERE id=?", (ask_id,)).fetchone()
    if not r or not r["actions"]:
        con.close()
        return None
    actions = json.loads(r["actions"])
    for i, a in enumerate(actions):
        if (idx is None or i == idx) and a.get("status") == "proposed":
            actions[i] = apply_action(con, a, who)
    con.execute("UPDATE ask_log SET actions=? WHERE id=?", (json.dumps(actions), ask_id))
    con.commit()
    out = row(con, ask_id)
    con.close()
    return out


def row(con, ask_id: int) -> dict | None:
    ensure(con)
    r = con.execute("SELECT * FROM ask_log WHERE id=?", (ask_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for f in ("sources", "actions"):
        try:
            d[f] = json.loads(d[f]) if d.get(f) else []
        except Exception:
            d[f] = []
    try:
        d["document"] = json.loads(d["document"]) if d.get("document") else None
    except Exception:
        d["document"] = None
    return d


def history(con, limit: int = 20) -> list[dict]:
    ensure(con)
    out = []
    for r in con.execute("SELECT * FROM ask_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
        d = dict(r)
        d["sources"] = []
        try:
            acts = json.loads(d["actions"]) if d.get("actions") else []
        except Exception:
            acts = []
        d["actions"] = acts
        d["n_applied"] = sum(1 for a in acts if a.get("status") == "applied")
        d["n_proposed"] = sum(1 for a in acts if a.get("status") == "proposed")
        try:
            d["document"] = json.loads(d["document"]) if d.get("document") else None
        except Exception:
            d["document"] = None
        out.append(d)
    return out


def answer_sync(question: str, today: date | None = None, upload: tuple[str, bytes] | None = None) -> dict:
    """Same pipeline without the thread/table -- for tests and the CLI."""
    today = today or date.today()
    doc = read_document(0, upload[0], upload[1]) if upload else None
    con = db.connect()
    ctx = retrieve(con, question, today)
    con.close()
    raw, err = call_model(build_prompt(question, ctx, today, doc))
    text, actions = parse_reply(raw) if raw else (fallback_answer(ctx), [])
    return {"answer": text, "actions": actions, "error": err, "sources": ctx["sources"], "document": doc}


def _self_test() -> int:
    """Offline: the household-driven parts (synonyms, kid validation, the prompt, masking).
    No model call, no database."""
    family.use_example()
    fails = []

    def check(name, cond):
        print(("  PASS  " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    n = _needles("when is Ava's picture day")
    check("a kid's name pulls in their school", "maple street elementary" in n and "maple street" in n)
    check("'school' pulls in every school's short name",
          {"maple street", "little oaks"} <= set(_needles("school closed")))
    check("an add_event for a household kid keeps the kid",
          (_validate_action({"kind": "add_event", "title": "Dentist", "event_date": "2026-10-01",
                             "kid": "Leo"}) or {}).get("kid") == "Leo")
    check("an unknown kid becomes Both",
          (_validate_action({"kind": "add_checklist", "title": "Form", "kid": "Zed"}) or {}).get("kid") == "Both")
    check("a payment kid is validated the same way",
          (_validate_action({"kind": "add_payment", "name": "Swim", "kid": "Zed",
                             "amount": "$45"}) or {}).get("kid") == "Both")
    p = build_prompt("when is picture day?", {"sections": [], "sources": []}, date(2026, 9, 10))
    check("prompt names the household and its kid values",
          "The Rivera household" in p and "Ava|Leo|Both" in p and "[[" not in p)
    check("long card numbers are masked to their last four",
          mask_numbers("card 4111 1111 1111 1234 paid $45") == "card ****1234 paid $45")
    print(("\nSELF-TEST FAILED: " + ", ".join(fails)) if fails else "\nSELF-TEST PASSED")
    return 1 if fails else 0


if __name__ == "__main__":  # python ask.py "what time is back to school night" [--file path]
    import sys
    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    args = [x for x in sys.argv[1:]]
    up = None
    if "--file" in args:
        i = args.index("--file")
        fp = Path(args[i + 1])
        up = (fp.name, fp.read_bytes())
        del args[i:i + 2]
    q = " ".join(args) or "what time is back to school night?"
    res = answer_sync(q, upload=up)
    print(res["answer"])
    for a in res["actions"]:
        print("PROPOSED:", describe(a))
    if res["error"]:
        print("\n[model error]", res["error"])
    print(f"\n[{len(res['sources'])} sources]")
