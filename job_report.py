"""The nightly job's own verdict — and a mail that fires only when it CHANGES.

The nightly job runs independent steps (school mail, Google Calendar). Left alone, a
wrapper script runs them all, appends their output to `logs/ingest.log`, and exits 0
whatever happened. A step could fail every night for a month and the only trace would be
a traceback in a log nobody opens — which is how a broken IMAP password or an expired
OAuth token goes unnoticed. The board keeps rendering yesterday's truth and looks
perfectly healthy.

So each step reports its exit code here, and this file answers three questions:

    python job_report.py --step mail --code $?    # record one step
    python job_report.py --verdict                # print it, exit non-zero if bad
    python job_report.py --alert                  # ...and mail you ON CHANGE
    python job_report.py --self-test              # offline, writes nothing

The alert fires on a CHANGE of verdict, never on every run: a nightly "still fine" mail
is trained away within a week and then the one that matters is invisible too. First run
after a failure alerts; a second identical failure does not; recovery alerts once.

A step that never reported at all is its own state — `never ran` is not `ran clean`.
"""
from __future__ import annotations

import argparse
import json
import os
import _env  # noqa: F401  -- loads this project's .env before anything reads os.environ
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import family

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("FM_DATA_DIR", ROOT / "data"))
STATE = DATA_DIR / "job_status.json"

# The alert goes to, and is sent from, the mailbox the app already reads.
MAIL_TO = family.SELF_EMAIL
MAIL_FROM = family.SELF_EMAIL

# Every step the nightly job runs. Naming them here is what makes "this step never
# reported" detectable — a dict of whatever happened to show up can't notice an absence.
STEPS = {
    "mail": "school / camp / activity mail (ingest.py)",
    "calendar": "Google Calendar sync + RSVP (gcal.py)",
}

# The job runs once a day. A step whose last report is older than this has MISSED a
# run — which is a different failure from reporting an error, and the one that hides.
STALE_AFTER = timedelta(hours=36)


def load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        # A corrupt state file must not read as "no problems". Say it, and rebuild.
        print(f"WARN {STATE.name} is unreadable ({exc}) — starting a fresh one.")
        return {}


def save(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def record(state: dict, step: str, code: int, now: str) -> dict:
    steps = state.setdefault("steps", {})
    steps[step] = {"code": int(code), "at": now}
    state["updated"] = now
    return state


def verdict(state: dict, now: datetime) -> tuple[bool, list[str]]:
    """(ok, problems). Three states per step: clean / failed / never reported."""
    problems: list[str] = []
    steps = state.get("steps") or {}
    for key, label in STEPS.items():
        entry = steps.get(key)
        if entry is None:
            problems.append(f"{label}: has NEVER reported — it may not be wired into "
                            "the nightly job at all")
            continue
        if entry.get("code", 0) != 0:
            problems.append(f"{label}: exited {entry['code']} on "
                            f"{str(entry.get('at'))[:19]}")
            continue
        try:
            age = now - datetime.fromisoformat(entry["at"])
        except Exception:
            problems.append(f"{label}: last report has an unreadable timestamp "
                            f"{entry.get('at')!r}")
            continue
        if age > STALE_AFTER:
            hrs = round(age.total_seconds() / 3600)
            problems.append(f"{label}: last ran {hrs}h ago — the daily job has missed "
                            "at least one run")
    return (not problems), problems


def send_mail(subject: str, body: str) -> bool:
    """Gmail over SMTP with an app password from the environment. Missing is an error
    with the variable's name, never a silent skip: an alert that can't send must say so."""
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    if not pw:
        raise RuntimeError("GMAIL_APP_PASSWORD is not set (put it in .env)")
    if not MAIL_TO:
        raise RuntimeError("no mailbox to alert: set self_email in data/household.json")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465,
                          context=ssl.create_default_context()) as smtp:
        smtp.login(MAIL_FROM, pw)
        smtp.send_message(msg)
    return True


def alert_body(ok: bool, problems: list[str], now: datetime) -> str:
    return ("Family Manager's nightly job changed state.\n\n"
            + ("Everything is running clean again.\n"
               if ok else "\n".join(f"- {p}" for p in problems) + "\n")
            + f"\nChecked {now:%Y-%m-%d %H:%M}. Board: {family.HUB_URL}\n"
              "Log: logs/ingest.log on the machine that runs the hub.\n\n"
              "You get this only when the verdict CHANGES, not every night.\n")


def self_test() -> int:
    family.use_example()
    fails = []

    def check(name, cond):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            fails.append(name)

    print("job_report self-test")
    now = datetime(2026, 8, 21, 16, 30)
    fresh = now.isoformat(timespec="seconds")
    stale = (now - timedelta(hours=50)).isoformat(timespec="seconds")

    all_clean = {"steps": {k: {"code": 0, "at": fresh} for k in STEPS}}
    ok, problems = verdict(all_clean, now)
    check("every step clean and fresh is ok", ok and not problems)

    one_failed = json.loads(json.dumps(all_clean))
    one_failed["steps"]["calendar"]["code"] = 1
    ok, problems = verdict(one_failed, now)
    check("a non-zero exit is a problem", not ok and any("exited 1" in p for p in problems))

    one_stale = json.loads(json.dumps(all_clean))
    one_stale["steps"]["mail"]["at"] = stale
    ok, problems = verdict(one_stale, now)
    check("a step that has not run in 50h is a problem",
          not ok and any("missed at least one run" in p for p in problems))

    missing = {"steps": {"mail": {"code": 0, "at": fresh}}}
    ok, problems = verdict(missing, now)
    check("a step that NEVER reported is its own problem, not a pass",
          not ok and sum("NEVER reported" in p for p in problems) == len(STEPS) - 1)

    ok, problems = verdict({}, now)
    check("an empty state is one problem per step, never an all-clear",
          not ok and len(problems) == len(STEPS))

    check("a corrupt state file does not read as clean",
          verdict({"steps": {"mail": {"code": 0, "at": "not-a-date"}}}, now)[0] is False)

    # Alert-on-change: the point is that a repeated identical failure stays quiet.
    check("the first failure is a change", changed({"last_alert": None}, False))
    check("a repeat of the same failure is not", not changed({"last_alert": "bad"}, False))
    check("recovery is a change", changed({"last_alert": "bad"}, True))
    check("a repeat of ok is not", not changed({"last_alert": "ok"}, True))

    recorded = record({}, "mail", 3, fresh)
    check("recording a step keeps its exit code", recorded["steps"]["mail"]["code"] == 3)

    check("the alert goes to the household's own mailbox",
          family.SELF_EMAIL == "sam@example.com")
    check("the alert body links the hub from the household file",
          family.HUB_URL in alert_body(False, ["x"], now))

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
    return 1 if fails else 0


def changed(state: dict, ok: bool) -> bool:
    return state.get("last_alert") != ("ok" if ok else "bad")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", choices=sorted(STEPS))
    ap.add_argument("--code", type=int)
    ap.add_argument("--verdict", action="store_true")
    ap.add_argument("--alert", action="store_true", help="--verdict, and mail on change")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    now = datetime.now()
    state = load()

    if args.step:
        if args.code is None:
            print("--step needs --code")
            return 2
        save(record(state, args.step, args.code, now.isoformat(timespec="seconds")))
        print(f"recorded {args.step} = {args.code}")
        if not (args.verdict or args.alert):
            return 0
        state = load()

    ok, problems = verdict(state, now)
    print("Family Manager nightly job: " + ("OK" if ok else "PROBLEMS"))
    for p in problems:
        print(f"  - {p}")

    if args.alert and changed(state, ok):
        try:
            send_mail(f"Family Manager nightly job: {'recovered' if ok else 'FAILING'}",
                      alert_body(ok, problems, now))
            state["last_alert"] = "ok" if ok else "bad"
            state["last_alert_at"] = now.isoformat(timespec="seconds")
            save(state)
            print("  (alert sent — the verdict changed)")
        except Exception as exc:
            # Do NOT record the alert as sent. An un-sent alert marked sent means the
            # next change is the one that goes missing.
            print(f"  ALERT FAILED TO SEND: {exc}")
            return 5
    elif args.alert:
        print("  (no alert — the verdict has not changed since the last one)")

    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
