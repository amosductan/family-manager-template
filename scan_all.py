"""Scan everything, on demand: the school mail and the Google Calendar -- the same feeds the
nightly job runs, in one button press, with what each one found reported at the end.

    python scan_all.py                     # every feed
    python scan_all.py --only mail
    python scan_all.py --self-test

Writes data/scan_status.json the whole way through so the page can show which feed is
running, what stage it is at, and -- when it is done -- what was NEW: mail rows and
calendar events. Each step's output goes to logs/scan_<step>.log and the last lines ride
in the status so a failure names itself.

Deliberately NOT wired into job_report: that is the nightly's verdict, and a scan somebody
runs at 10am failing on a lapsed login should not flip the alert the nightly owns. A step
here fails in the open, on the page, with its reason.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
STATUS = DATA / "scan_status.json"
HEARTBEAT_S = 2
STALE_S = 180          # a "running" status older than this is a scan that died

# Order matters a little: mail first (fast, most often has something), calendar last so the
# day-off board reads what the mail step just stored. Timeouts are per step and generous;
# the nightly has no cap at all.
STEPS = [
    ("mail",     "School and camp mail", ["ingest.py"],                                                      20 * 60),
    ("calendar", "Google Calendar",      ["gcal.py", "--days-back", "30", "--days-ahead", "365", "--rsvp"], 10 * 60),
]
STEP_NAMES = [s[0] for s in STEPS]


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def read_status() -> dict:
    st = None
    for _ in range(4):
        # A reader can land between a writer's truncate and its close (the in-place fallback)
        # or on a locked file; a moment later it is whole again.
        try:
            st = json.loads(STATUS.read_text(encoding="utf-8"))
            break
        except FileNotFoundError:
            return {"running": False, "steps": {}}
        except (ValueError, OSError):
            time.sleep(0.05)
    if st is None:
        return {"running": False, "steps": {}}
    if st.get("running"):
        try:
            age = (datetime.now() - datetime.fromisoformat(st.get("at") or "1970-01-01")).total_seconds()
        except ValueError:
            age = STALE_S + 1
        if age > STALE_S:
            # Alive-but-not-moving is its own state; a dead scan must never pin the page.
            st["running"] = False
            st["died"] = True
            st["summary"] = "the last scan stopped without finishing"
    return st


def _write(st: dict) -> None:
    """Status to disk. On Windows, os.replace onto a file another process is READING raises
    PermissionError (sharing violation) -- and the page polls this file every 2s, so an
    early scan died at its own heartbeat, minutes in, with a child step still running.
    Retry the atomic swap briefly, then write in place; a status write must never be what
    kills the scan."""
    st["at"] = _now()
    DATA.mkdir(exist_ok=True)
    body = json.dumps(st, indent=1)
    tmp = STATUS.with_suffix(".tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        for attempt in range(8):
            try:
                os.replace(tmp, STATUS)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        STATUS.write_text(body, encoding="utf-8")
    except OSError as exc:
        print(f"status write failed ({exc}); continuing", file=sys.stderr)


def _tail(path: Path, n: int = 25) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [l.rstrip() for l in lines[-n:]]


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def sub_stage(step: str) -> str | None:
    """What the step is doing right now, read from the status file the step itself keeps.
    No model, no guess: mail already writes one; the calendar gets its log tail."""
    if step == "mail":
        st = _read_json(DATA / "ingest_status.json", {})
        return st.get("stage")
    for line in reversed(_tail(LOGS / f"scan_{step}.log", 6)):
        l = line.strip()
        if l and not l.startswith("=====") and not _is_noise(l):
            return l[:120]
    return None


def _env() -> dict:
    """The child env: claude_headless.env(utf8=True) -- strips any Anthropic API key the
    same way every other spawn here does and PROVES the subscription first; UTF-8 forced so
    an emoji subject can't kill a step."""
    import claude_headless
    e = claude_headless.env(utf8=True)
    claude_headless.assert_subscription(e)
    return e


def already_busy() -> str | None:
    """Refuse to double-run a feed. The nightly job uses the same scripts; two ingest.py
    processes on one SQLite file is how 'database is locked' lands in the middle of a
    message."""
    st = read_status()
    if st.get("running"):
        return "a scan is already running"
    ing = _read_json(DATA / "ingest_status.json", {})
    if ing.get("running"):
        try:
            age = (datetime.now() - datetime.fromisoformat(ing.get("started") or "1970-01-01")).total_seconds()
        except ValueError:
            age = 0
        if age < 30 * 60:
            return "the mail check is already running (started " + str(ing.get("started")) + ")"
    return None


def whats_new(started: str) -> dict:
    """Counted from the data, after the fact -- never from what a step SAID it did.
    `created_at` is stamped by db.now() in every writer, so 'since the scan started' is
    one comparison."""
    import db

    out: dict = {}
    con = db.connect()
    try:
        mail = con.execute(
            "SELECT id, subject, sender, source, kid FROM emails WHERE created_at >= ? "
            "AND COALESCE(source,'') != 'pasted' ORDER BY id DESC LIMIT 40", (started,)).fetchall()
        events = con.execute(
            "SELECT id, kid, title, event_date, source FROM events WHERE created_at >= ? "
            "AND status = 'active' ORDER BY event_date, id LIMIT 60", (started,)).fetchall()
        actions = con.execute(
            "SELECT COUNT(*) FROM mail_actions WHERE created_at >= ? AND COALESCE(state,'open') = 'open'",
            (started,)).fetchone()[0] if _has(con, "mail_actions", "created_at") else None
    finally:
        con.close()
    out["mail"] = [dict(r) for r in mail]
    out["events"] = [dict(r) for r in events]
    by_src: dict[str, int] = {}
    for e in out["events"]:
        by_src[e["source"] or "?"] = by_src.get(e["source"] or "?", 0) + 1
    out["events_by_source"] = by_src
    out["open_actions_added"] = actions
    return out


def _has(con, table: str, col: str) -> bool:
    return any(r["name"] == col for r in con.execute(f"PRAGMA table_info({table})"))


def run(only: list[str] | None = None) -> int:
    busy = already_busy()
    if busy:
        print("REFUSED:", busy)
        return 3
    wanted = [s for s in STEPS if not only or s[0] in only]
    if not wanted:
        print("nothing to run")
        return 2
    LOGS.mkdir(exist_ok=True)
    started = _now()
    st = {"running": True, "started": started, "finished": None, "current": None,
          "only": only or None,
          "steps": {name: {"label": label, "state": "pending" if (not only or name in only) else "skipped",
                           "detail": None if (not only or name in only) else "not selected"}
                    for name, label, _, _ in STEPS},
          "new": None, "summary": None, "died": False}
    _write(st)
    py = sys.executable
    failed = 0

    for name, label, argv, timeout in wanted:
        step = st["steps"][name]
        step.update({"state": "running", "started": _now(), "detail": "starting"})
        st["current"] = name
        _write(st)
        log = LOGS / f"scan_{name}.log"
        # The env is proven BEFORE the child exists: a refusal (headless Claude would not be
        # on the subscription) is a failed step with the reason, never an exception that
        # leaves this step "running" forever.
        try:
            child_env = _env()
        except Exception as exc:  # noqa: BLE001 -- SubscriptionRequired, or the CLI missing
            failed += 1
            step.update({"state": "failed", "finished": _now(), "detail": f"refused: {exc}"[:300]})
            _write(st)
            continue
        with log.open("w", encoding="utf-8") as fh:
            fh.write(f"===== {started} scan: {name} =====\n")
            fh.flush()
            proc = subprocess.Popen([py, str(ROOT / argv[0]), *argv[1:]], cwd=str(ROOT), stdout=fh,
                                    stderr=subprocess.STDOUT, env=child_env,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            deadline = time.monotonic() + timeout
            code = None
            while True:
                code = proc.poll()
                if code is not None:
                    break
                if time.monotonic() > deadline:
                    proc.kill()
                    code = "timeout"
                    break
                try:
                    step["detail"] = sub_stage(name) or "working"
                    _write(st)
                except Exception as exc:  # noqa: BLE001 -- the child keeps running; so must we
                    print(f"heartbeat skipped: {exc}", file=sys.stderr)
                time.sleep(HEARTBEAT_S)
        tail = _tail(log)
        step["tail"] = tail
        step["finished"] = _now()
        step["code"] = code
        if code == 0:
            step["state"] = "ok"
            step["detail"] = _ok_detail(name, tail)
        elif code == "timeout":
            failed += 1
            step["state"] = "failed"
            step["detail"] = f"gave up after {timeout // 60} min"
        else:
            failed += 1
            step["state"] = "failed"
            step["detail"] = _fail_detail(name, code, tail)
        _write(st)

    st["running"] = False
    st["current"] = None
    st["finished"] = _now()
    try:
        st["new"] = whats_new(started)
    except Exception as exc:  # noqa: BLE001 -- the counts are a bonus; the scan itself is done
        st["new"] = {"error": f"could not count what was new: {exc}"[:200]}
    n_ok = sum(1 for s in st["steps"].values() if s["state"] == "ok")
    ran = sum(1 for s in st["steps"].values() if s["state"] in ("ok", "failed"))
    st["summary"] = f"{n_ok} of {ran} feeds scanned" + (f", {failed} failed" if failed else "")
    _write(st)
    print(st["summary"])
    return 1 if failed else 0


def _ok_detail(name: str, tail: list[str]) -> str:
    """One line for the page. Taken from the step's own last line when it prints a
    summary; 'done' otherwise -- never a number this module did not read."""
    for line in reversed(tail):
        l = line.strip()
        if not l or l.startswith("=====") or l.startswith("Traceback") or _is_noise(l):
            continue
        return l[:160]
    return "done"


_NOISE = ("Ignoring wrong pointing object", "WARNING", "Warning:", "DeprecationWarning",
          "UserWarning", "incorrect startxref", "  File ")


def _is_noise(line: str) -> bool:
    """Library chatter is not a result. pypdf prints 'Ignoring wrong pointing object' for
    every malformed school PDF, and that was about to be the mail step's summary line."""
    return any(line.startswith(n) or n in line for n in _NOISE)


def _fail_detail(name: str, code, tail: list[str]) -> str:
    for line in reversed(tail):
        l = line.strip()
        if l and not l.startswith("====="):
            return f"exit {code}: {l[:150]}"
    return f"exit {code}"


# ---------------------------------------------------------------- self-test


def _self_test() -> int:
    global STATUS, DATA, LOGS
    import tempfile

    import family
    family.use_example()

    fails = 0

    def check(name, ok):
        nonlocal fails
        print(("  PASS  " if ok else "  FAIL  ") + name)
        if not ok:
            fails += 1

    with tempfile.TemporaryDirectory() as td:
        DATA = Path(td) / "data"
        LOGS = Path(td) / "logs"
        STATUS = DATA / "scan_status.json"
        DATA.mkdir()

        check("no status file reads as not running", read_status() == {"running": False, "steps": {}})
        # A reader holding the file open (what the page poll does) must not kill a write.
        _write({"running": True, "steps": {}})
        with open(STATUS, "r", encoding="utf-8") as held:
            _write({"running": True, "steps": {}, "probe": 1})
            held.read()
        check("write survives a concurrent reader", read_status().get("probe") == 1)
        _write({"running": True, "steps": {}})
        check("fresh running status is running", read_status()["running"] is True)
        st = json.loads(STATUS.read_text())
        st["at"] = (datetime.now() - timedelta(seconds=STALE_S + 5)).isoformat()
        STATUS.write_text(json.dumps(st))
        r = read_status()
        check("stale running status reads as died, not running", r["running"] is False and r.get("died"))

        check("nothing busy when nothing is running", already_busy() is None)
        (DATA / "ingest_status.json").write_text(json.dumps({"running": True, "started": _now()}))
        check("a running mail check blocks the scan", "mail check" in (already_busy() or ""))
        (DATA / "ingest_status.json").write_text(json.dumps({"running": False}))
        check("clear again", already_busy() is None)

        (DATA / "ingest_status.json").write_text(json.dumps({"running": True, "stage": "Searching maplestreet"}))
        check("mail sub-stage comes from ingest_status", sub_stage("mail") == "Searching maplestreet")
        LOGS.mkdir()
        (LOGS / "scan_calendar.log").write_text("=====\nfetched 600 events\n")
        check("other steps' sub-stage is the log tail", sub_stage("calendar") == "fetched 600 events")

        check("ok detail skips the banner", _ok_detail("mail", ["===== x", "12 new emails, 3 events"]) == "12 new emails, 3 events")
        check("ok detail skips pypdf chatter",
              _ok_detail("mail", ["12 new emails", "Ignoring wrong pointing object 8 0 (offset 0)"]) == "12 new emails")
        check("failures carry the last line", _fail_detail("mail", 1, ["=====", "IMAP login failed"]) == "exit 1: IMAP login failed")
        check("a failure with no output still says its exit code", _fail_detail("calendar", 2, []) == "exit 2")

        check("the steps are exactly mail and calendar", STEP_NAMES == ["mail", "calendar"])
        check("STEPS have unique names and a timeout each",
              len(set(STEP_NAMES)) == len(STEPS) and all(t > 0 for *_, t in STEPS))
        check("every step's script exists", all((ROOT / argv[0]).exists() for _, _, argv, _ in STEPS))
        check("--only filters to known steps", [s[0] for s in STEPS if s[0] in ["mail", "nope"]] == ["mail"])

    print("\n" + ("ALL PASS" if not fails else f"{fails} FAILED"))
    return 1 if fails else 0


def _tee_own_output() -> None:
    """The app launches this script with no stdout/stderr capture, so when it died the only
    evidence was a heartbeat that stopped. Everything it prints now also lands in
    logs/scan_all.log, and an uncaught exception is written there too."""
    try:
        LOGS.mkdir(exist_ok=True)
        fh = open(LOGS / "scan_all.log", "a", encoding="utf-8", errors="replace")
    except OSError:
        return

    class _Tee:
        def __init__(self, orig):
            self.orig = orig

        def write(self, data):
            for out in (self.orig, fh):
                try:
                    out.write(data)
                    out.flush()
                except Exception:  # noqa: BLE001
                    pass

        def flush(self):
            pass

    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)
    fh.write(f"\n===== {_now()} scan_all {' '.join(sys.argv[1:])} =====\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated: " + ",".join(STEP_NAMES))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    only = [s.strip() for s in a.only.split(",") if s.strip()] if a.only else None
    if only:
        bad = [s for s in only if s not in STEP_NAMES]
        if bad:
            print("unknown step(s):", ", ".join(bad), "-- choose from", ", ".join(STEP_NAMES))
            return 2
    return run(only)


if __name__ == "__main__":
    if "--self-test" not in sys.argv:
        _tee_own_output()
    try:
        code = main()
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        # Leave the status honest: a scan that crashed is not running.
        try:
            st = read_status()
            if st.get("running"):
                st.update({"running": False, "died": True, "finished": _now(),
                           "summary": "the scan crashed -- see logs/scan_all.log"})
                _write(st)
        except Exception:  # noqa: BLE001
            pass
        code = 4
    sys.exit(code)
