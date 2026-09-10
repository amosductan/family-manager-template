"""Deep email extraction for Family Manager — the "open every link and read it" layer.

The v1 ingest stored an email's *body text* and, for one source only (a weekly school
folder), followed one link to one PDF. A district's "Welcome Back" packets broke that:
their body is nothing but a list of nine file NAMES, and every actual notice — the academic
calendar, the drop-off procedures, the lunch price list — lives behind a SchoolMessenger
Secure-Document-Delivery link. So the app "ingested" the email and knew nothing in it.

This module makes the sweep DEEP for every source:

  documents_for(msg, session, msg_key)   -> list[Document]
      Every attachment (pdf, docx, xlsx, ics, image, text) AND every link in the HTML
      body, each resolved to real bytes + extracted text where possible. Handles the
      SchoolMessenger SDD flow, Google Docs/Sheets/Drive exports, direct file URLs, and
      plain web pages. Bytes are saved under data/mail/<msg_key>/ so a human can open the
      original; a link is fetched ONCE (SDD tracker links are single-use) and never re-probed.

  summarize(subject, body, docs, anchor) -> dict
      A model pass (via `claude -p`, through claude_headless like every model call here) over
      the body PLUS every document's text, returning a human summary, dated events, and the
      action items that ask something of a parent. Degrades to a deterministic summary when
      the model is unavailable — never silently returns nothing.

Every document carries a three-state status: "ok" | "failed:<why>" | "skipped:<why>".
An error is never rendered as an empty success — a link we could not open says so.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

import db
import family
import claude_headless

UA =("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

MAIL_DIR = db.DATA_DIR / "mail"

# Links that are never worth fetching — unsubscribe, tracking pixels, social chrome.
SKIP_LINK_PAT = re.compile(
    r"unsub|/unsubscribe|list-manage|mailto:|tel:|facebook\.com|twitter\.com|x\.com/|"
    r"instagram\.com|linkedin\.com|youtube\.com|/optout|preferences|privacy|"
    r"schoolmessenger\.com/\?token|go\.schoolmessenger",
    re.I,
)

MAX_DOC_TEXT = 40000        # per document, into the summary
MAX_BYTES = 25 * 1024 * 1024
LINK_TIMEOUT = 15           # per link fetch — a slow/hanging server must not stall the run
SDD_TIMEOUT = 45            # the SchoolMessenger requestdocument POST
LINK_BUDGET_S = 75          # total time one message may spend opening its links


# ------------------------------------------------------------------ small helpers

def _safe_name(name: str, fallback: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", (name or "").strip())[:120]
    name = name.strip("._ ") or fallback
    # Windows reserved device names would make an unopenable file.
    if re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)", name, re.I):
        name = "_" + name
    return name


def msg_key(msg_id: str) -> str:
    """A short, filesystem-safe, STABLE folder name for a message. Kept short (a hash tail
    over the full id) because Windows still caps a full path at 260 chars by default, and a
    long msg-id + a long attachment name blew past it."""
    import hashlib
    k = (msg_id or "").strip().strip("<>").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", k)[:24].strip("-")
    tail = hashlib.sha1(k.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{tail}" if slug else f"msg-{tail}"


def _kind_for(content_type: str, filename: str, head: bytes) -> str:
    ct = (content_type or "").lower()
    fn = (filename or "").lower()
    if head[:5] == b"%PDF-" or "pdf" in ct or fn.endswith(".pdf"):
        return "pdf"
    if head[:2] == b"PK" and (fn.endswith(".docx") or "word" in ct):
        return "docx"
    if head[:2] == b"PK" and (fn.endswith(".xlsx") or "spreadsheet" in ct or "excel" in ct):
        return "xlsx"
    if head[:2] == b"PK" and (fn.endswith(".pptx") or "presentation" in ct):
        return "pptx"
    if head[:8] == b"\x89PNG\r\n\x1a\n" or fn.endswith(".png") or "png" in ct:
        return "image"
    if head[:3] == b"\xff\xd8\xff" or fn.endswith((".jpg", ".jpeg")) or "jpeg" in ct:
        return "image"
    if fn.endswith(".gif") or head[:6] in (b"GIF87a", b"GIF89a"):
        return "image"
    if fn.endswith(".ics") or "calendar" in ct or head[:15].upper().startswith(b"BEGIN:VCALENDAR"):
        return "ics"
    if fn.endswith((".txt", ".csv")) or "text/plain" in ct or "text/csv" in ct:
        return "text"
    if "html" in ct or head[:15].lower().startswith((b"<!doctype", b"<html")):
        return "html"
    return "file"


# ------------------------------------------------------------------ text extractors

def pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception as e:
        return f"__ERR__ pdf: {e}"


def docx_text(data: bytes) -> str:
    try:
        import docx
        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs if p.text and p.text.strip()]
        for t in d.tables:
            for row in t.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
        return "\n".join(parts).strip()
    except Exception as e:
        # Fallback: pull the raw XML text so a broken python-docx still yields words.
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "replace")
            return re.sub(r"\s+\n", "\n", re.sub(r"<[^>]+>", " ", xml)).strip() or f"__ERR__ docx: {e}"
        except Exception:
            return f"__ERR__ docx: {e}"


def xlsx_text(data: bytes) -> str:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            out.append(f"[sheet: {ws.title}]")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i > 300:
                    out.append("… (truncated)")
                    break
                cells = [str(c) for c in row if c is not None]
                if cells:
                    out.append(" | ".join(cells))
        return "\n".join(out).strip()
    except Exception as e:
        return f"__ERR__ xlsx: {e}"


def ics_text(data: bytes) -> str:
    """A tiny iCalendar reader — no dependency. Pulls SUMMARY/DTSTART/LOCATION per VEVENT."""
    try:
        text = data.decode("utf-8", "replace")
        # Unfold continued lines (RFC5545: a leading space continues the previous line).
        text = re.sub(r"\r?\n[ \t]", "", text)
        out = []
        for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
            fields = dict(re.findall(r"^([A-Z]+)[^:\r\n]*:(.*)$", block, re.M))
            when = fields.get("DTSTART", "")
            m = re.match(r"(\d{4})(\d{2})(\d{2})", when)
            when = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else when
            line = " · ".join(x for x in [fields.get("SUMMARY", "").strip(), when,
                                          fields.get("LOCATION", "").strip()] if x)
            if line:
                out.append(line)
        return "\n".join(out).strip()
    except Exception as e:
        return f"__ERR__ ics: {e}"


def html_page_text(data: bytes) -> str:
    try:
        soup = BeautifulSoup(data, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "form"]):
            tag.decompose()
        main = soup.find("main") or soup.find("article") or soup.body or soup
        return "\n".join(" ".join(l.split()) for l in main.get_text("\n").splitlines()
                         if l.strip()).strip()
    except Exception as e:
        return f"__ERR__ html: {e}"


def extract_text(kind: str, data: bytes) -> tuple[str, str]:
    """(text, status) for a document's bytes. Image/file kinds have no text but are still ok."""
    if kind == "pdf":
        t = pdf_text(data)
    elif kind == "docx":
        t = docx_text(data)
    elif kind == "xlsx":
        t = xlsx_text(data)
    elif kind == "ics":
        t = ics_text(data)
    elif kind == "text":
        t = data.decode("utf-8", "replace").strip()
    elif kind == "html":
        t = html_page_text(data)
    elif kind == "image":
        return "", "ok"          # kept for the human to open; not transcribed
    elif kind == "pptx":
        t = _pptx_text(data)
    else:
        return "", "ok"
    if t.startswith("__ERR__"):
        return "", "failed:" + t[8:].strip()
    return t[:MAX_DOC_TEXT], "ok"


def _pptx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            slides = sorted(n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n))
            out = []
            for n in slides:
                xml = z.read(n).decode("utf-8", "replace")
                out.append(" ".join(re.findall(r"<a:t>(.*?)</a:t>", xml)))
            return "\n".join(o for o in out if o.strip()).strip()
    except Exception as e:
        return f"__ERR__ pptx: {e}"


# ------------------------------------------------------------------ link resolution

def _google_export_url(url: str) -> str | None:
    """A shareable Google Docs/Sheets/Slides/Drive link → a direct export URL."""
    m = re.search(r"docs\.google\.com/(document|spreadsheets|presentation)/d/([\w-]+)", url)
    if m:
        kind, gid = m.group(1), m.group(2)
        fmt = {"document": "txt", "spreadsheets": "csv", "presentation": "pdf"}[kind]
        return f"https://docs.google.com/{kind}/d/{gid}/export?format={fmt}"
    m = re.search(r"drive\.google\.com/file/d/([\w-]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    m = re.search(r"drive\.google\.com/open\?id=([\w-]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return None


def resolve_link(url: str, text: str, session: requests.Session) -> dict | None:
    """Follow one link and return a Document, or None to ignore it.

    Fetched ONCE — a SchoolMessenger tracker link is consumed on first click, so this is
    the only touch. The bytes we get here are what the human will see; nothing re-probes.
    """
    if not url or SKIP_LINK_PAT.search(url) or url.startswith(("mailto:", "tel:", "#")):
        return None

    doc = {"origin": "link", "url": url, "name": (text or "").strip()[:120] or url[:120],
           "kind": "file", "status": "ok", "text": "", "bytes": None, "final_url": url}

    # Google Docs/Sheets/Drive: swap the share URL for its export endpoint up front.
    gexport = _google_export_url(url)
    fetch_url = gexport or url
    try:
        r = session.get(fetch_url, allow_redirects=True, timeout=LINK_TIMEOUT)
    except requests.RequestException as e:
        doc["status"] = f"failed:fetch {type(e).__name__}"
        return doc
    doc["final_url"] = r.url

    ct = r.headers.get("Content-Type", "")
    disp = r.headers.get("Content-Disposition", "")
    fn = ""
    m = re.search(r'filename\*?="?([^"\r\n;]+)', disp)
    if m:
        fn = m.group(1)
    content = r.content or b""

    # A Google sign-in wall (private doc) or a landing page pretending to be a file.
    low_final = r.url.lower()
    if "accounts.google.com" in low_final or "servicelogin" in low_final:
        doc["status"] = "failed:needs-sign-in"
        return doc

    # The SchoolMessenger Secure-Document-Delivery page: POST for the real file.
    if "schoolmessenger.com/m/" in low_final and b"message-link-code" in content:
        got = _schoolmessenger_download(r, session)
        if got is None:
            doc["status"] = "failed:sdd link expired or already used"
            return doc
        content, ct, fn = got

    if not content:
        doc["status"] = "failed:empty response"
        return doc
    if len(content) > MAX_BYTES:
        doc["status"] = f"skipped:too big ({len(content)//1024//1024} MB)"
        return doc

    kind = _kind_for(ct, fn or url, content[:16])
    doc["kind"] = kind
    if fn:
        doc["name"] = _safe_name(fn, doc["name"])
    if kind in ("pdf", "docx", "xlsx", "ics", "image", "pptx", "text"):
        doc["bytes"] = content
        doc["text"], doc["status"] = extract_text(kind, content)
    elif kind == "html":
        # A real web page (an announcement page) — keep the readable text, not the bytes.
        doc["text"], doc["status"] = extract_text("html", content)
        if len(doc["text"]) < 40:
            doc["status"] = "skipped:page had no readable content"
    else:
        doc["bytes"] = content
    return doc


def _schoolmessenger_download(resp, session) -> tuple[bytes, str, str] | None:
    page = BeautifulSoup(resp.text, "html.parser")
    mlc = page.find("input", {"id": "message-link-code"})
    alc = page.find("input", {"id": "attachment-link-code"})
    if not (mlc and alc):
        return None
    dl = urljoin(resp.url.rsplit("/", 1)[0] + "/", "requestdocument.php")
    try:
        r2 = session.post(dl, data={"s": mlc.get("value", ""), "mal": alc.get("value", "")},
                          timeout=SDD_TIMEOUT)
    except requests.RequestException:
        return None
    if r2.status_code != 200 or not r2.content or b"error occurred" in r2.content[:400].lower():
        return None
    fn = ""
    m = re.search(r'filename\*?="?([^"\r\n;]+)', r2.headers.get("Content-Disposition", ""))
    if m:
        fn = m.group(1)
    return r2.content, r2.headers.get("Content-Type", ""), fn


# ------------------------------------------------------------------ attachments

def attachment_docs(msg) -> list[dict]:
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = (part.get("Content-Disposition") or "")
        fn = part.get_filename()
        is_attach = disp.lower().startswith("attachment") or bool(fn)
        if not is_attach:
            continue
        data = part.get_payload(decode=True)
        if not data:
            continue
        fn = db_decode(fn) or "attachment"
        kind = _kind_for(part.get_content_type(), fn, data[:16])
        doc = {"origin": "attachment", "url": None, "name": _safe_name(fn, "attachment"),
               "kind": kind, "status": "ok", "text": "", "bytes": data, "final_url": None}
        if kind in ("pdf", "docx", "xlsx", "ics", "image", "pptx", "text", "html"):
            doc["text"], doc["status"] = extract_text(kind, data)
        out.append(doc)
    return out


def db_decode(raw):
    if not raw:
        return raw
    try:
        import email.header
        out = ""
        for text, enc in email.header.decode_header(raw):
            out += text.decode(enc or "utf-8", "replace") if isinstance(text, bytes) else text
        return out
    except Exception:
        return raw


def body_links(html: str) -> list[tuple[str, str]]:
    """(text, href) for every worth-following link, de-duplicated, order preserved."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href in seen or SKIP_LINK_PAT.search(href) or href.startswith(("mailto:", "tel:", "#")):
            continue
        seen.add(href)
        out.append((a.get_text(strip=True), href))
    return out


# ------------------------------------------------------------------ the public entry

def documents_for(msg, html: str, msg_id: str, max_links: int = 25,
                  session: requests.Session | None = None) -> list[dict]:
    """Every attachment and every link in the message, resolved to bytes + text on disk.

    Returns Documents with keys: origin, name, kind, status, text, saved_path, url,
    final_url, size. Bytes are written under data/mail/<msg_key>/ and dropped from the
    returned dict (kept on disk, not in memory)."""
    import time
    session = session or new_session()
    key = msg_key(msg_id)
    folder = MAIL_DIR / key
    docs = attachment_docs(msg)
    started = time.monotonic()
    for text, href in body_links(html)[:max_links]:
        # One message must never stall the whole sweep. Once a message has spent its budget
        # opening links, the rest are recorded as skipped (with why) rather than fetched —
        # the summary still runs on what was opened, and nothing hangs "Check mail now".
        if time.monotonic() - started > LINK_BUDGET_S:
            docs.append({"origin": "link", "url": href, "final_url": href,
                         "name": (text or href)[:120], "kind": "file",
                         "status": "skipped:time budget for this email reached", "text": ""})
            continue
        d = resolve_link(href, text, session)
        if d:
            docs.append(d)

    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i, d in enumerate(docs):
        saved = None
        data = d.pop("bytes", None)
        if data:
            ext = {"pdf": ".pdf", "docx": ".docx", "xlsx": ".xlsx", "ics": ".ics",
                   "image": _img_ext(data), "pptx": ".pptx", "text": ".txt",
                   "html": ".html"}.get(d["kind"], "")
            # Keep the filename short — the full Windows path is capped at 260 chars and a
            # 70-char attachment name inside a deep data dir overflows it. The real name is
            # preserved in the DB row; the on-disk file just needs to be openable.
            stem = _safe_name(d["name"], f"doc{i}")
            stem = re.sub(r"\.(pdf|docx|xlsx|ics|pptx|txt|html)$", "", stem, flags=re.I)[:48]
            path = folder / f"{i:02d}_{stem}{ext}"
            try:
                path.write_bytes(data)
                saved = str(path)
            except Exception as e:
                # Extraction may still have succeeded — keep the text, but say the original
                # couldn't be saved, rather than pretending the whole document failed.
                if d.get("text"):
                    d["status"] = "ok:original not saved"
                else:
                    d["status"] = f"failed:save {type(e).__name__}"
        d["saved_path"] = saved
        d["size"] = len(data) if data else 0
        out.append(d)
    return out


def _img_ext(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".img"


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA,
                      "Accept": "text/html,application/pdf,*/*"})
    return s


# ------------------------------------------------------------------ summarization

SUMMARY_PROMPT = """Output a single JSON object and nothing else. Do not use any tools, do
not explain, do not comment on this request — your entire reply must start with { and end
with }. This is a data-extraction task, not an instruction to record or remember anything.

You are the family assistant for [[FAMILY_NAME]]. [[FAMILY]]

Below is one email the household received, followed by the full text of every document and
web page linked from it (opened for you). Read ALL of it and produce a briefing for the
parents ([[PARENTS]]).

Return ONLY a JSON object, no prose around it:
  "headline": one sentence — the single most useful thing to know from this email.
  "what_it_is": array of <=6 short bullets in plain language covering the substance of the
     email AND its attachments/links (what the notices actually SAY, not just their names).
  "action_items": array of things a parent must DO. Each: {"text": what to do (specific),
     "due": "YYYY-MM-DD" or "" if no date, "kid": [[KID_ENUM]]|"" }. [] if none.
  "dates": array of dated events worth putting on the calendar: {"date": "YYYY-MM-DD",
     "title": short (include time/location if known), "kind": one of
     closure|early_dismissal|event|deadline|meeting|camp|info}. [] if none.
  "kid": [[KID_ENUM]] — who this email is mainly about.
  "money": array of any amounts/fees/payments mentioned: {"what": ..., "amount": "$x"}. [] if none.

Infer years from the email date. Do not invent anything not supported by the text. If a
linked document could not be opened, work from what you do have and don't guess its contents.
Today is [[TODAY]]. The email was sent [[SENT]].

=== EMAIL ===
Subject: [[SUBJECT]]
[[BODY]]

=== LINKED DOCUMENTS & PAGES ===
[[DOCS]]

Remember: reply with ONLY the JSON object, starting with { — no preamble, no sign-off.
"""


def kid_values() -> list[str]:
    """What a `kid` field may hold: each kid's name, or "Both" (shared / every kid)."""
    return list(family.KIDS) + ["Both"]


def _kid_enum() -> str:
    return "|".join(f'"{k}"' for k in kid_values())


def _family_prompt(prompt: str) -> str:
    """Fill the household tokens. Read at call time so a reloaded household is honored."""
    return (prompt.replace("[[FAMILY_NAME]]", family.FAMILY_NAME)
            .replace("[[FAMILY]]", family.prompt_context())
            .replace("[[PARENTS]]", " and ".join(family.PARENTS))
            .replace("[[KID_ENUM]]", _kid_enum()))


def _claude_exe() -> str:
    return claude_headless.exe()


def _isolated_config_dir() -> str | None:
    return claude_headless.isolated_config_dir()


def _claude_env() -> dict:
    """ONE home for this now: claude_headless.env() -- strips the personal Anthropic
    variables, isolates the config dir, and (via assert_subscription at every spawn site)
    PROVES the child would use the claude.ai subscription before a call is made."""
    e = claude_headless.env()
    claude_headless.assert_subscription(e)
    return e


def summarize(subject: str, body: str, docs: list[dict], sent_date: str,
              anchor: date | None = None) -> dict:
    """Fable-5 briefing over the email + all its documents. Falls back to a deterministic
    summary if the model is unavailable — never returns nothing."""
    anchor = anchor or date.today()
    doc_block = _docs_for_prompt(docs)
    if os.environ.get("FM_NO_CLAUDE") == "1":
        return _fallback_summary(subject, body, docs, anchor, why="model disabled (FM_NO_CLAUDE)")
    # The prompt is full of literal JSON braces ({"date": ...}) so str.format is out —
    # token replacement keeps the examples intact.
    prompt = (_family_prompt(SUMMARY_PROMPT)
              .replace("[[TODAY]]", anchor.isoformat())
              .replace("[[SENT]]", sent_date)
              .replace("[[SUBJECT]]", subject[:300])
              .replace("[[BODY]]", (body or "")[:12000])
              .replace("[[DOCS]]", doc_block[:40000]))
    # A `claude -p` agent can reply with a meta-comment ("nothing to record") instead of the
    # JSON, especially when it inherits a global CLAUDE.md. One firm retry that feeds back
    # its own stray reply almost always fixes it.
    last = ""
    for attempt in range(2):
        p = prompt if attempt == 0 else (
            "You replied with this, which is NOT the JSON object requested:\n\n"
            f"{last[:500]}\n\nDo it again correctly. Output ONLY the JSON object described "
            "below, starting with { and ending with }.\n\n" + prompt)
        try:
            r = claude_headless.run(p, "claude-sonnet-5", timeout=150, purpose="mail_summary")
        except FileNotFoundError:
            return _fallback_summary(subject, body, docs, anchor, why="claude CLI not found")
        except Exception as exc:
            return _fallback_summary(subject, body, docs, anchor, why=f"model call failed: {exc}")
        raw = (r.stdout or "").strip()
        last = raw
        if "credit balance is too low" in raw.lower():
            return _fallback_summary(subject, body, docs, anchor,
                                     why="claude -p: credit balance too low (unset ANTHROPIC_API_KEY)")
        if "failed to authenticate" in raw.lower() or "oauth session expired" in raw.lower():
            # Not a content problem and not worth a retry: the machine's Claude login is
            # gone. Say so by name -- "no JSON after retry" hid this for two runs.
            return _fallback_summary(subject, body, docs, anchor,
                                     why="Claude login on this machine has expired -- run "
                                         "`claude login` here, then use 'Try the briefing again'")
        s, e = raw.find("{"), raw.rfind("}")
        if s >= 0 and e > s:
            try:
                data = json.loads(raw[s:e + 1])
                data["_engine"] = "fable-5"
                return _normalize_summary(data)
            except json.JSONDecodeError:
                continue
    return _fallback_summary(subject, body, docs, anchor, why="model returned no JSON after retry")


def transcribe_image(path: str) -> tuple[str, str]:
    """Verbatim text of a screenshot (a class-app post, a photographed paper note), via the
    same `claude -p` the briefing uses, with ONLY the Read tool allowed so it can open the
    file. Returns (text, why_empty). Measured on a 900x2000 phone screenshot: about 10s,
    verbatim. Never raises -- an empty text with a reason is the failure shape."""
    if os.environ.get("FM_NO_CLAUDE") == "1":
        return "", "model disabled (FM_NO_CLAUDE)"
    prompt = ("Read the image file at the path below with your Read tool and transcribe ALL "
              "the text it contains, verbatim, preserving line breaks. Skip phone status bars, "
              "app navigation labels and button captions. Output ONLY the transcribed text, "
              "nothing else.\n\n" + str(path))
    try:
        r = claude_headless.run(prompt, "claude-sonnet-5", timeout=180,
                                tools="Read", extra_args=["--allowedTools", "Read"],
                                purpose="image_transcribe")
    except FileNotFoundError:
        return "", "claude CLI not found"
    except Exception as exc:
        return "", f"model call failed: {exc}"
    raw = (r.stdout or "").strip()
    if not raw:
        return "", "model returned nothing" + (f" ({r.stderr.strip()[:200]})" if r.stderr else "")
    if "credit balance is too low" in raw.lower():
        return "", "claude -p: credit balance too low (unset ANTHROPIC_API_KEY)"
    if r.returncode != 0:
        # The JSON envelope flagged an error; its message is not a transcription.
        return "", f"model call failed: {raw[:200]}"
    return raw, ""


def _docs_for_prompt(docs: list[dict]) -> str:
    parts = []
    for d in docs:
        head = f"--- {d.get('name','document')} [{d.get('kind')}, {d.get('status')}]"
        txt = (d.get("text") or "").strip()
        if not txt:
            txt = "(no extractable text — a human can open the file)" if d.get("status") == "ok" \
                  else "(could not open this link/file)"
        parts.append(head + " ---\n" + txt[:MAX_DOC_TEXT])
    return "\n\n".join(parts)


def _normalize_summary(d: dict) -> dict:
    d.setdefault("headline", "")
    for k in ("what_it_is", "action_items", "dates", "money"):
        if not isinstance(d.get(k), list):
            d[k] = []
    # A kid the household doesn't have (a model guess, a sibling named in the mail) is not
    # a kid value the app can file under. Blank it; the email's source decides instead.
    valid = set(kid_values())
    if d.get("kid") not in valid:
        d["kid"] = ""
    for a in d["action_items"]:
        if isinstance(a, dict) and a.get("kid") and a["kid"] not in valid:
            a["kid"] = ""
    return d


def _fallback_summary(subject, body, docs, anchor, why: str) -> dict:
    """Deterministic briefing used when the model can't run. Uses the existing regex
    extractor so dates are still surfaced and the email is never left blank."""
    import extract
    text = (subject or "") + "\n\n" + (body or "")
    for d in docs:
        if d.get("text"):
            text += "\n\n" + d["text"]
    evs = extract.extract_events(text, anchor)
    dates = [{"date": e["event_date"], "title": e["title"], "kind": e["type"]}
             for e in evs if e.get("event_date")]
    bullets = [subject.strip()] if subject else []
    opened = [d["name"] for d in docs if d.get("status") == "ok"]
    if opened:
        bullets.append("Attached / linked: " + ", ".join(opened[:6]))
    return _normalize_summary({
        "headline": subject.strip() or "(no subject)",
        "what_it_is": bullets,
        "action_items": [], "dates": dates, "kid": "", "money": [],
        "_engine": "deterministic", "_note": why,
    })


# ------------------------------------------------------------------ self-test

def _self_test() -> int:
    import email
    family.use_example()
    ok = 0
    fail = []

    def check(name, cond):
        nonlocal ok
        if cond:
            ok += 1
        else:
            fail.append(name)

    # kind sniffing
    check("pdf magic", _kind_for("", "x", b"%PDF-1.7") == "pdf")
    check("png magic", _kind_for("", "x", b"\x89PNG\r\n\x1a\n") == "image")
    check("docx by name", _kind_for("application/zip", "a.docx", b"PK\x03\x04") == "docx")
    check("xlsx by ct", _kind_for("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "s", b"PK") == "xlsx")
    check("ics by name", _kind_for("", "c.ics", b"BEGIN:VCALENDAR") == "ics")

    # reserved device names
    check("reserved name guarded", _safe_name("CON.pdf", "x").startswith("_"))
    check("bad chars stripped", "/" not in _safe_name("a/b:c*.pdf", "x"))

    # ics parser
    ics = (b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Back to School Night\r\n"
           b"DTSTART:20260915T180000\r\nLOCATION:Maple Street\r\nEND:VEVENT\r\nEND:VCALENDAR")
    t, st = extract_text("ics", ics)
    check("ics text", "Back to School Night" in t and "2026-09-15" in t)
    check("ics ok", st == "ok")

    # docx round-trip (build a real minimal docx)
    try:
        import docx
        d = docx.Document()
        d.add_paragraph("Lunch price is $2.50 starting September 2.")
        buf = io.BytesIO()
        d.save(buf)
        t, st = extract_text("docx", buf.getvalue())
        check("docx text", "Lunch price" in t and st == "ok")
    except Exception as e:
        fail.append(f"docx build: {e}")

    # xlsx round-trip
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        wb.active.append(["Item", "Qty"])
        wb.active.append(["Glue sticks", 4])
        buf = io.BytesIO()
        wb.save(buf)
        t, st = extract_text("xlsx", buf.getvalue())
        check("xlsx text", "Glue sticks" in t and st == "ok")
    except Exception as e:
        fail.append(f"xlsx build: {e}")

    # a failed extraction reports failed, not empty-ok
    t, st = extract_text("pdf", b"not a pdf at all")
    check("bad pdf -> failed", st.startswith("failed:"))

    # link skip rules
    check("skip unsubscribe", resolve_link("https://x.com/unsubscribe?u=1", "Unsubscribe",
                                            new_session()) is None)
    check("skip mailto", body_links('<a href="mailto:a@b.com">m</a>') == [])
    check("keep real link", body_links('<a href="https://school.org/cal.pdf">Cal</a>')
          == [("Cal", "https://school.org/cal.pdf")])

    # google export mapping
    check("google doc export",
          _google_export_url("https://docs.google.com/document/d/ABC123/edit")
          == "https://docs.google.com/document/d/ABC123/export?format=txt")
    check("google sheet export",
          "format=csv" in (_google_export_url("https://docs.google.com/spreadsheets/d/XYZ/edit?usp=sharing") or ""))
    check("drive file export",
          "uc?export=download&id=FILE9" in (_google_export_url("https://drive.google.com/file/d/FILE9/view") or ""))

    # attachment walk
    m = email.message.EmailMessage()
    m["Subject"] = "Test"
    m.set_content("body")
    m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf",
                     filename="note.pdf")
    ad = attachment_docs(m)
    check("attachment found", len(ad) == 1 and ad[0]["kind"] == "pdf" and ad[0]["origin"] == "attachment")

    # fallback summary never blank, surfaces a date
    fs = _fallback_summary("Early Dismissal Day",
                           "Early Dismissal for all students on September 3 — a half day.",
                           [], date(2026, 8, 1), why="test")
    check("fallback headline", bool(fs["headline"]))
    check("fallback date", any(x["date"] == "2026-09-03" for x in fs["dates"]))
    check("fallback engine tagged", fs["_engine"] == "deterministic")

    # normalize coerces bad shapes
    n = _normalize_summary({"action_items": "nope"})
    check("normalize coerces", n["action_items"] == [] and n["what_it_is"] == [])

    # kid values come from the household, not from the model
    n2 = _normalize_summary({"kid": "Zed", "action_items": [{"text": "x", "kid": "Zed"},
                                                            {"text": "y", "kid": "Ava"}]})
    check("an unknown kid is blanked", n2["kid"] == "" and n2["action_items"][0]["kid"] == "")
    check("a household kid is kept", n2["action_items"][1]["kid"] == "Ava")
    pr = _family_prompt(SUMMARY_PROMPT)
    check("prompt names the household's kids and parents",
          all(k in pr for k in family.KIDS) and all(p in pr for p in family.PARENTS)
          and '"Both"' in pr and "[[" not in pr.split("[[TODAY]]")[0])

    print(f"mailsweep self-test: {ok} passed, {len(fail)} failed")
    for f in fail:
        print("  FAIL:", f)
    return 0 if not fail else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    print("mailsweep.py — import me; run with --self-test to verify.")
