"""Family Manager ingest — IMAP pull from known school senders.

Auth: a Gmail app password in the GMAIL_APP_PASSWORD environment variable (put it in this
folder's .env; _env.py loads it). The mailbox is the household's own (self_email in
data/household.json). IMAP rather than the Gmail API on purpose: an OAuth grant expires or
gets revoked and needs a person to re-approve it, while an app password keeps working
unattended until you revoke it.

Run: python ingest.py [--days N] [--dry-run] [--reprocess]
Writes data/ingest_status.json so the dashboard can show live progress.
"""
import email
import email.header
import email.utils
import imaplib
import json
import os
import _env  # noqa: F401  -- loads this project's .env (personal secrets) before anything reads os.environ
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

import db
import extract
import family
import mailsweep

# The nightly job pipes stdout to a log file, and Windows opens that pipe as cp1252. A
# single emoji in a subject line (a swim-lesson "Enrollment Is Open") then raises
# UnicodeEncodeError and kills the whole ingest — which is exactly how one nightly run
# crashed after storing one message. Force UTF-8 so a school's emoji subject can never
# take the sweep down again.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
PDF_DIR = db.DATA_DIR / "pdfs"
STATUS_FILE = db.DATA_DIR / "ingest_status.json"

IMAP_HOST = os.environ.get("FM_IMAP_HOST", "imap.gmail.com")
IMAP_USER = family.SELF_EMAIL
PASSWORD_VAR = "GMAIL_APP_PASSWORD"
DEFAULT_WINDOW_DAYS = 21

# One HTTP session for the whole run — reused across every message's link fetches so
# connection pooling and cookies (SchoolMessenger sets one) carry over.
_SWEEP_SESSION = mailsweep.new_session()
# The model summary is the point of the deep sweep, so it's ON by default. FM_NO_SUMMARY=1
# skips it (documents still get opened and stored, deterministic extraction still runs).
NO_SUMMARY = os.environ.get("FM_NO_SUMMARY") == "1"
# One run summarizes at most this many NEW messages, so a first-time backfill of a big
# window can't run for an hour. School sources are read first (enabled_froms orders gated
# senders last), so the cap only ever defers low-signal tail mail — which gets its summary
# on the next run, since a message without a fable-5 summary is re-processed, not skipped.
MAX_NEW_SUMMARIES = int(os.environ.get("FM_MAX_NEW_SUMMARIES", "40"))
_summaries_done = [0]

# IMAP FROM queries (db.sources holds the classification rules; these are just the
# server-side prefilters). The fallback when the sources table has nothing enabled: the
# senders the household file configures.
SEARCH_FROMS = list(dict.fromkeys(s[3] for s in family.SOURCES if s[3]))


class MissingSetting(RuntimeError):
    """A setting the ingest can't run without. Reported by name, never as a traceback."""


def gmail_app_password() -> str:
    """The app password, from the environment only (.env is loaded by _env.py). Google shows
    it in groups of four with spaces; the spaces aren't part of it."""
    pw = os.environ.get(PASSWORD_VAR, "").strip()
    if not pw:
        raise MissingSetting(
            f"{PASSWORD_VAR} is not set. Create a Gmail app password (Google Account > "
            f"Security > App passwords) for {IMAP_USER or 'the household mailbox'} and add "
            f"{PASSWORD_VAR}=... to the .env file in this folder.")
    return pw.replace(" ", "")


def imap_user() -> str:
    if not IMAP_USER:
        raise MissingSetting("No mailbox to read: set self_email (or the first parent's email) "
                             "in data/household.json.")
    return IMAP_USER


def set_status(**kw):
    STATUS_FILE.parent.mkdir(exist_ok=True)
    cur = {}
    if STATUS_FILE.exists():
        try:
            cur = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            cur = {}
    cur.update(kw, updated=datetime.now().isoformat(timespec="seconds"))
    STATUS_FILE.write_text(json.dumps(cur, indent=2), encoding="utf-8")


def decode_hdr(raw) -> str:
    if raw is None:
        return ""
    out = ""
    for text, enc in email.header.decode_header(raw):
        out += text.decode(enc or "utf-8", "replace") if isinstance(text, bytes) else text
    return " ".join(out.split())


def body_parts(msg) -> tuple[str, str]:
    """Return (plain_text, html) best-effort."""
    plain = html = None
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get("Content-Disposition", "").startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        if ctype == "text/plain" and plain is None:
            plain = payload.decode(part.get_content_charset() or "utf-8", "replace")
        elif ctype == "text/html" and html is None:
            html = payload.decode(part.get_content_charset() or "utf-8", "replace")
    return plain or "", html or ""


def html_to_text(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    return "\n".join(" ".join(l.split()) for l in text.splitlines() if l.strip())


def classify_source(con, sender: str, subject: str, body: str = ""):
    """Match against db.sources. Precedence: a subject_match rule (specific) beats a plain
    sender rule. Only ENABLED sources match. A keyword-gated source (e.g. a partner's
    forwards) matches only when the subject or body carries one of its keywords — so their
    personal mail isn't swept, but a school note they forward is."""
    rows = con.execute("SELECT * FROM sources WHERE COALESCE(enabled,1)=1").fetchall()
    hay_subject = subject.lower()
    hay_all = (subject + "\n" + (body or "")).lower()
    best = None
    for r in rows:
        if not r["sender_match"] or r["sender_match"].lower() not in sender.lower():
            continue
        kws = json.loads(r["keywords"]) if r["keywords"] else []
        if kws and not any(k.lower() in hay_all for k in kws):
            continue
        if r["subject_match"]:
            if r["subject_match"].lower() in hay_subject:
                return r
        elif best is None:
            best = r
    return best


# Domains/addresses whose mail is always relevant even before a rule names them — used by
# the discovery pass to auto-flag, never to silently ingest. The SCHOOL_KEYWORDS score the
# rest of the inbox.
def relevance_score(sender: str, subject: str, body: str) -> float:
    """0..1 — how school/kid-relevant a message looks. Used only to SUGGEST a new source to
    a parent, never to ingest on its own, so a promotional blast can't become an event."""
    import db as _db
    hay = (subject + " " + (body or "")[:2000]).lower()
    kws = _db.SCHOOL_KEYWORDS
    hits = sum(1 for k in kws if k in hay)
    score = min(hits / 4.0, 1.0)
    dom = sender.split("@")[-1].lower()
    if any(t in dom for t in _SCHOOLISH_DOMAIN_WORDS):
        score = max(score, 0.6)
    return round(score, 2)


# Words that make a sender's DOMAIN look like a school, camp or kids' program on its own.
_SCHOOLISH_DOMAIN_WORDS = (".edu", "school", "k12", "camp", "pta", "pto", "montessori",
                           "daycare", "preschool", "academy", "learning")


def save_pdf_attachments(msg, stamp: str) -> list[Path]:
    paths = []
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    for i, part in enumerate(msg.walk()):
        if part.get_content_type() == "application/pdf":
            data = part.get_payload(decode=True)
            if data and data[:5] == b"%PDF-":
                p = PDF_DIR / f"{stamp}_att{i}.pdf"
                p.write_bytes(data)
                paths.append(p)
    return paths


def download_linked_pdf(html: str, stamp: str) -> Path | None:
    """A weekly school folder's PDFs often arrive as SchoolMessenger Secure Document
    Delivery links, not attachments (many districts use it)."""
    soup = BeautifulSoup(html, "html.parser")
    indicators = ["pdf", "download", "wednesday", "folder", "announcement",
                  "newsletter", "view", "open", "docs.google", "drive.google"]
    candidates = []
    for link in soup.find_all("a", href=True):
        combined = (link["href"] + " " + link.get_text(strip=True)).lower()
        score = sum(1 for ind in indicators if ind in combined)
        if score:
            candidates.append((score, link["href"]))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    url = candidates[0][1]

    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        r = s.get(url, allow_redirects=True, timeout=30)
    except requests.RequestException:
        return None
    pdf_data = None
    if "pdf" in r.headers.get("Content-Type", "").lower() or r.content[:5] == b"%PDF-":
        pdf_data = r.content
    else:
        page = BeautifulSoup(r.text, "html.parser")
        mlc = page.find("input", {"id": "message-link-code"})
        alc = page.find("input", {"id": "attachment-link-code"})
        if mlc and alc:
            from urllib.parse import urljoin
            dl = urljoin(r.url.rsplit("/", 1)[0] + "/", "requestdocument.php")
            try:
                r2 = s.post(dl, data={"s": mlc.get("value", ""),
                                      "mal": alc.get("value", "")}, timeout=60)
                if r2.content[:5] == b"%PDF-":
                    pdf_data = r2.content
            except requests.RequestException:
                pass
    if not pdf_data:
        return None
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    p = PDF_DIR / f"{stamp}_linked.pdf"
    p.write_bytes(pdf_data)
    return p


def pdf_text(path: Path) -> str:
    try:
        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        print(f"  PDF extract failed for {path.name}: {e}")
        return ""


def stable_msg_id(msg) -> str:
    """The dedup key for a message, stable across processes.

    The fallback for a message with no Message-ID used to be Python's builtin
    `hash()` over the first 500 bytes. `hash()` of bytes is SALTED PER PROCESS
    (PYTHONHASHSEED), so the same message got a different key on every run: it never
    matched the `emails` row it had already written, so it was re-ingested and
    re-extracted every single night, quietly multiplying its events. sha256 over the
    identifying headers plus the head of the body is stable forever.
    """
    real = decode_hdr(msg.get("Message-ID"))
    if real:
        return real
    import hashlib
    seed = "|".join([
        decode_hdr(msg.get("From")),
        decode_hdr(msg.get("Subject")),
        decode_hdr(msg.get("Date")),
        (msg.as_bytes()[:2000]).decode("utf-8", "replace"),
    ])
    return "nomsgid-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def process_message(con, msg, dry_run=False) -> str:
    msg_id = stable_msg_id(msg)
    existing = con.execute("SELECT swept_at, summary FROM emails WHERE msg_id = ?",
                           (msg_id,)).fetchone()
    if existing is not None:
        # A row exists — but "stored" is not "deeply read". A message ingested by the old
        # shallow path (or before the deep sweep existed) has no documents and no summary;
        # re-running depth for it is exactly the backfill "Check mail now" should do. Only a
        # message that has already been swept AND summarized is a true no-op.
        summ = json.loads(existing["summary"]) if existing["summary"] else {}
        if existing["swept_at"] and summ.get("_engine") == "fable-5":
            return "dup"
        # else: fall through and (re)run the deep sweep + summary on this stored message.

    sender = email.utils.parseaddr(msg.get("From", ""))[1]
    subject = decode_hdr(msg.get("Subject"))
    try:
        sent_dt = email.utils.parsedate_to_datetime(msg.get("Date"))
        sent_date = sent_dt.strftime("%Y-%m-%d")
    except Exception:
        sent_date = date.today().isoformat()

    plain, html = body_parts(msg)
    body = plain or html_to_text(html)

    src = classify_source(con, sender, subject, body)
    if src is None:
        # Not a known source — but is it school/kid-relevant? Flag it for a parent to
        # approve at /mail/sources, then move on. This is what makes the sweep get more
        # expansive over time without silently ingesting the whole inbox.
        if not dry_run:
            score = relevance_score(sender, subject, body)
            if score >= 0.5:
                db.record_suggestion(con, sender, decode_hdr(msg.get("From")).split("<")[0].strip(),
                                     subject, score)
                con.commit()
                return "suggested"
        return "skip"
    # A keyword-gated forward (a partner's) that is really a Google Calendar invitation is
    # gcal's job, not the mail sweep's — the event is already on the shared board. Skipping
    # these keeps the forwards down to the ones carrying actual content (supply lists,
    # notes home).
    if src["keywords"] and json.loads(src["keywords"]) and re.match(
            r"^\s*(invitation:|updated invitation:|accepted:|declined:|tentative:|"
            r"canceled event:|cancelled:)", subject, re.I):
        return "skip"

    key, kid = src["key"], src["kid"]
    print(f"  [{key}] {sent_date} {subject[:70]}")

    if dry_run:
        return "would-ingest"

    # DEPTH: open every attachment and every link (PDFs, DOCX, XLSX, ICS, Google Docs,
    # web pages), extract their text, and keep the originals on disk for a human to open.
    session = _SWEEP_SESSION
    try:
        docs = mailsweep.documents_for(msg, html, msg_id, session=session)
    except Exception as e:
        print(f"    depth sweep raised {type(e).__name__}: {e}")
        docs = []
    opened = sum(1 for d in docs if d["status"].startswith("ok"))
    failed = [d for d in docs if d["status"].startswith("failed")]

    # The email body plus the full text of everything opened — this is what the summary and
    # the deterministic extractor both read now, not just the body's list of file names.
    full_text = body
    for d in docs:
        if d.get("text"):
            full_text += f"\n\n--- {d['name']} ---\n" + d["text"]

    pdf_path = next((d["saved_path"] for d in docs if d["kind"] == "pdf" and d["saved_path"]), None)
    con.execute(
        "INSERT INTO emails (msg_id, source, sender, subject, sent_date, kid, "
        "body_text, pdf_path, created_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(msg_id) DO UPDATE SET source=excluded.source, kid=excluded.kid, "
        "body_text=excluded.body_text, pdf_path=excluded.pdf_path",
        (msg_id, key, sender, subject, sent_date, kid, full_text[:200000], pdf_path, db.now()),
    )
    db.save_documents(con, msg_id, docs)

    # Newsletter-class sources (a weekly cadence: the school's weekly folder, a preschool's
    # weekly update) also get a newsletters row for the per-kid pages.
    if (src["cadence"] or "") == "weekly":
        label = subject or f"{src['name']} {sent_date}"
        con.execute(
            "INSERT OR IGNORE INTO newsletters (kid, source, label, nl_date, subject, content) "
            "VALUES (?,?,?,?,?,?)",
            (kid, key, label, sent_date, subject,
             json.dumps({"body": full_text[:20000]})),
        )

    # SUMMARY: a Fable-5 briefing over the body + every document, giving a headline, the
    # substance, dated events, action items and any money. Degrades to the deterministic
    # extractor if the model is unavailable — never blank.
    anchor = date.fromisoformat(sent_date)
    n_summary_actions = 0
    if not NO_SUMMARY and _summaries_done[0] < MAX_NEW_SUMMARIES:
        _summaries_done[0] += 1
        try:
            summary = mailsweep.summarize(subject, body, docs, sent_date, anchor)
            db.save_summary(con, msg_id, summary, kid)
            n_summary_actions = len(summary.get("action_items") or [])
            events = [{"type": d.get("kind", "info"), "title": d["title"],
                       "event_date": d["date"], "end_date": None, "details": ""}
                      for d in (summary.get("dates") or []) if d.get("date") and d.get("title")]
        except Exception as e:
            print(f"    summary raised {type(e).__name__}: {e}")
            events = []
    else:
        # Over the per-run cap (or summaries disabled): store a deterministic summary now so
        # the message still shows its substance and its documents. The model upgrade happens
        # on the next run or via /mail/reprocess (which re-summarizes from the stored docs,
        # no re-fetch) — a message without a fable-5 summary is re-processed, never skipped.
        if not NO_SUMMARY:
            summary = mailsweep._fallback_summary(subject, body, docs, anchor,
                                                  why="deferred past this run's summary cap")
            db.save_summary(con, msg_id, summary, kid)
        events = []

    # Deterministic events from the FULL text (body + all documents) as the floor, so dates
    # land even when the summary is off. INSERT OR IGNORE dedups against the summary's dates.
    if not events:
        events = extract.extract_events(subject + "\n\n" + full_text, anchor)
    n_ev = 0
    for ev in events:
        # A monthly-cadence sender is a program newsletter (a rec center, an activity
        # club): page after page of class listings, each with a date. Its "info" rows are
        # catalog, not the family's plans; closures and deadlines from it still land.
        if (src["cadence"] or "") == "monthly" and ev["type"] == "info":
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO events (kid, school, type, title, event_date, end_date, "
            "details, category, source, source_ref, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (kid, src["name"], ev["type"], ev["title"], ev.get("event_date"),
             ev.get("end_date"), ev.get("details", ""), db.category_for_source(key),
             key, msg_id, db.now()),
        )
        n_ev += cur.rowcount
    db.touch_source(con, key, sent_date)
    con.commit()
    note = f"{opened} docs opened"
    if failed:
        note += f", {len(failed)} could not open"
    print(f"    -> stored ({n_ev} events, {n_summary_actions} action items, {note})")
    return "new"


def enabled_froms(con) -> list[str]:
    """The distinct sender_match values of every enabled source — the targeted IMAP
    prefilters. Derived from the DB so a new source (or a parent-approved suggestion) is
    scanned the very next run, with no code change. Keyword-gated senders (a partner's
    forwards, which need a subject prefilter over a big mailbox) go LAST, so the high-signal school
    feeds are read and summarized first and are visible even if the tail runs long."""
    rows = con.execute("SELECT DISTINCT sender_match, COALESCE(keywords,'') kw FROM sources "
                       "WHERE COALESCE(enabled,1)=1 AND sender_match IS NOT NULL").fetchall()
    plain, gated = [], []
    for r in rows:
        if not r["sender_match"]:
            continue
        (gated if r["kw"] not in ("", "[]") else plain).append(r["sender_match"])
    return sorted(set(plain)) + sorted(set(gated))


def reprocess(con, days: int) -> str:
    """Re-run the summary over already-stored messages (using their stored body + documents)
    without touching IMAP — the recovery path for when the model was unavailable at ingest
    (empty/failed summaries) and single-use links were already consumed. Never re-opens links."""
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = con.execute(
        "SELECT msg_id, subject, sent_date, kid, body_text, summary FROM emails "
        "WHERE sent_date >= ? ORDER BY sent_date", (since,)).fetchall()
    done = 0
    for i, r in enumerate(rows):
        cur = json.loads(r["summary"]) if r["summary"] else {}
        if cur.get("_engine") == "fable-5":
            continue   # already has a good summary
        set_status(stage=f"Re-summarizing {i + 1}/{len(rows)}: {r['subject'][:40]}")
        docs = [dict(d) for d in con.execute(
            "SELECT name, kind, status, text FROM mail_documents WHERE msg_id=?",
            (r["msg_id"],)).fetchall()]
        summary = mailsweep.summarize(r["subject"] or "", r["body_text"] or "", docs,
                                      r["sent_date"], date.fromisoformat(r["sent_date"]))
        db.save_summary(con, r["msg_id"], summary, r["kid"])
        con.commit()
        done += 1
        print(f"  re-summarized [{summary.get('_engine')}] {r['subject'][:60]}")
    return f"re-summarized {done} of {len(rows)} message(s) in the last {days} days"


def discovery_scan(con, M, since: str, already: set) -> int:
    """Scan the whole window's inbox headers and flag any school/kid-relevant sender that no
    rule covers yet, as a suggestion a parent can approve. Headers only — cheap. Never
    ingests on its own: a promotional blast can score words but only a human turns it into a
    source."""
    try:
        typ, data = M.search(None, f"(SINCE {since})")
    except Exception as e:
        print(f"discovery: search failed ({e})")
        return 0
    ids = data[0].split()
    enabled_domains = {sm.lower() for sm in enabled_froms(con)}
    n = 0
    # Fetch headers in one batch call per 200 for speed.
    for start in range(0, len(ids), 200):
        chunk = b",".join(ids[start:start + 200])
        try:
            typ, md = M.fetch(chunk, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        except Exception:
            continue
        for item in md:
            if not isinstance(item, tuple):
                continue
            h = email.message_from_bytes(item[1])
            sender = email.utils.parseaddr(h.get("From", ""))[1]
            if not sender or any(d in sender.lower() for d in enabled_domains):
                continue
            subject = decode_hdr(h.get("Subject"))
            score = relevance_score(sender, subject, "")
            if score >= 0.6:
                db.record_suggestion(con, sender,
                                     decode_hdr(h.get("From")).split("<")[0].strip(),
                                     subject, score)
                n += 1
    if n:
        con.commit()
    return n


def main():
    days = DEFAULT_WINDOW_DAYS
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    dry_run = "--dry-run" in sys.argv

    set_status(running=True, stage="Connecting to Gmail (IMAP)", started=db.now(),
               summary=None)
    con = db.connect()
    counts = {"new": 0, "dup": 0, "skip": 0, "suggested": 0, "would-ingest": 0}
    _summaries_done[0] = 0

    # Recovery mode: re-summarize stored mail, no IMAP.
    if "--reprocess" in sys.argv:
        try:
            summary = reprocess(con, days)
            set_status(running=False, stage="done", finished=db.now(), summary=summary, ok=True)
            print(summary)
            return 0
        finally:
            con.close()

    # Settings first, before any network: a missing password is a sentence naming the
    # variable and a non-zero exit, not a traceback from the middle of an IMAP login.
    try:
        user, password = imap_user(), gmail_app_password()
    except MissingSetting as e:
        set_status(running=False, stage="error", finished=db.now(),
                   summary=f"FAILED: {e}", ok=False)
        con.close()
        print(f"ingest: {e}", file=sys.stderr)
        return 2

    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST)
        M.login(user, password)
        M.select("INBOX", readonly=True)
        since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
        froms = enabled_froms(con) or SEARCH_FROMS
        # A keyword-gated sender (a partner's forwards: 100+ personal messages in the window)
        # must NOT have every message full-fetched just to classify it. Collect the subject
        # keywords so the loop can prefilter on a cheap headers-only fetch and only download
        # the bodies that actually look like school/kid mail.
        gated = {}
        for r in con.execute("SELECT sender_match, keywords FROM sources "
                             "WHERE COALESCE(enabled,1)=1 AND keywords IS NOT NULL "
                             "AND keywords NOT IN ('', '[]')"):
            kws = json.loads(r["keywords"])
            if kws:
                gated[r["sender_match"]] = [k.lower() for k in kws]
        # A source that fails to scan is NOT a source with no mail. Both used to end
        # the same way — zero messages, a clean "done" — so a broken search or a
        # dropped connection looked exactly like a quiet week at school.
        gaps = []
        for frm in froms:
            set_status(stage=f"Searching {frm}")
            try:
                typ, data = M.search(None, f'(FROM "{frm}" SINCE {since})')
            except Exception as e:
                gaps.append(f"{frm}: search raised {type(e).__name__}: {e}")
                print(f"{frm}: SEARCH FAILED ({e}) — this sender was NOT scanned")
                continue
            if typ != "OK":
                gaps.append(f"{frm}: search returned {typ}")
                print(f"{frm}: SEARCH returned {typ} — this sender was NOT scanned")
                continue
            ids = data[0].split()
            # Prefilter a keyword-gated sender on the SUBJECT with a cheap headers-only
            # fetch, so a mailbox of personal forwards doesn't cost 100+ full downloads. A
            # message whose keyword is only in the BODY is not deep-swept here — for a
            # forward the tell is almost always in the subject ("Fwd: … school …").
            kws = gated.get(frm)
            if kws and ids:
                keep = []
                for start in range(0, len(ids), 200):
                    chunk = b",".join(ids[start:start + 200])
                    try:
                        typ, hd = M.fetch(chunk, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
                    except Exception:
                        keep = ids  # on a header-fetch failure, fall back to scanning all
                        break
                    hdrs = [h for h in hd if isinstance(h, tuple)]
                    for idx_h, item in enumerate(hdrs):
                        subj = decode_hdr(email.message_from_bytes(item[1]).get("Subject")).lower()
                        if any(k in subj for k in kws):
                            keep.append(ids[start + idx_h])
                ids = keep
            print(f"{frm}: {len(ids)} message(s) to read since {since}")
            for i, mid in enumerate(ids):
                set_status(stage=f"{frm}: message {i + 1}/{len(ids)}")
                try:
                    typ, mdata = M.fetch(mid, "(RFC822)")
                except Exception as e:
                    gaps.append(f"{frm}: fetch of {mid.decode()} raised {e}")
                    continue
                if typ != "OK" or not mdata or not isinstance(mdata[0], tuple):
                    gaps.append(f"{frm}: fetch of {mid.decode()} returned {typ}")
                    continue
                msg = email.message_from_bytes(mdata[0][1])
                try:
                    result = process_message(con, msg, dry_run)
                except Exception as e:
                    gaps.append(f"{frm}: process of {mid.decode()} raised "
                                f"{type(e).__name__}: {e}")
                    print(f"    process raised {type(e).__name__}: {e}")
                    continue
                counts[result] = counts.get(result, 0) + 1

        # Discovery: flag relevant senders no rule covers yet (suggestions, never ingest).
        if not dry_run:
            set_status(stage="Looking for school mail from new senders")
            try:
                found = discovery_scan(con, M, since, set())
                counts["suggested"] += found
            except Exception as e:
                print(f"discovery raised {type(e).__name__}: {e}")
        M.logout()

        summary = (f"{counts['new']} new, {counts['dup']} already stored, "
                   f"{counts['skip']} skipped")
        if counts["suggested"]:
            summary += f", {counts['suggested']} new sender(s) to review"
        if dry_run:
            summary = f"[dry run] {counts['would-ingest']} would ingest, " + summary
        if gaps:
            summary += f" — INCOMPLETE: {len(gaps)} source/message(s) not scanned"
        set_status(running=False, stage="done", finished=db.now(), summary=summary,
                   ok=not gaps, gaps=gaps or None)
        print(f"Ingest complete: {summary}")
        for g in gaps:
            print(f"  gap: {g}")
        # A partial run must not exit 0, or the nightly job reports success forever
        # while a whole school's mail goes unread.
        if gaps:
            return 4
        return 0
    except Exception as e:
        set_status(running=False, stage="error", finished=db.now(),
                   summary=f"FAILED: {e}", ok=False)
        raise
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
