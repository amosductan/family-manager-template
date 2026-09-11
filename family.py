"""Who this household is. The ONE place a family's names, schools and addresses live.

Everything that used to be written into the code -- the parents in the picker, the kids'
pages, the birthdays, which senders count as school mail, the paragraph the model reads
about the family -- comes from `data/household.json`. Nothing else in the app may name a
person, a school or an address.

    data/household.json            yours (gitignored -- it never leaves your machine)
    config/household.example.json  a fictional family, used until you write your own

Set it up by hand, or open this folder in your AI coding assistant and say "set up my family".

    python family.py               # print what the app currently believes
    python family.py --self-test
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("FM_DATA_DIR", str(ROOT / "data")))
HOUSEHOLD_FILE = Path(os.environ.get("FM_HOUSEHOLD", str(DATA_DIR / "household.json")))
EXAMPLE_FILE = ROOT / "config" / "household.example.json"

# Who can cover a day a kid is out of school, beyond the parents. A household edits this
# list in its own file; these are only the defaults.
DEFAULT_COVERAGE_EXTRA = ["Camp", "Grandparents", "Babysitter", "Other"]
ALL_MONTHS = list(range(1, 13))
SCHOOL_YEAR = [9, 10, 11, 12, 1, 2, 3, 4, 5, 6]


class HouseholdError(ValueError):
    """The household file is wrong in a way the app can't guess its way past."""


def _read() -> tuple[dict, bool]:
    if HOUSEHOLD_FILE.exists():
        return json.loads(HOUSEHOLD_FILE.read_text(encoding="utf-8")), False
    return json.loads(EXAMPLE_FILE.read_text(encoding="utf-8")), True


def validate(cfg: dict) -> dict:
    """Refuse a household the app can't run on, naming the field. A missing kid or parent
    list is never defaulted: a plausible default family is worse than an error."""
    parents = cfg.get("parents") or []
    kids = cfg.get("kids") or []
    if not parents or not all((p.get("name") or "").strip() for p in parents):
        raise HouseholdError("household.json: 'parents' needs at least one entry with a name")
    if not kids or not all((k.get("name") or "").strip() for k in kids):
        raise HouseholdError("household.json: 'kids' needs at least one entry with a name")
    names = [p["name"].strip() for p in parents] + [k["name"].strip() for k in kids]
    if len({n.lower() for n in names}) != len(names):
        raise HouseholdError("household.json: every parent and kid needs a distinct name")
    reserved = {"both", "tbd", "everyone", "unassigned", "other"}
    clash = [n for n in names if n.lower() in reserved]
    if clash:
        raise HouseholdError(f"household.json: {clash[0]!r} is a reserved word in the app")
    for k in kids:
        b = (k.get("birthday") or "").strip()
        if b and not re.fullmatch(r"\d{2}-\d{2}", b):
            raise HouseholdError(f"household.json: {k['name']}'s birthday must be MM-DD, got {b!r}")
    for s in cfg.get("sources") or []:
        if not s.get("key") or not s.get("sender_match"):
            raise HouseholdError("household.json: every source needs a 'key' and a 'sender_match'")
    return cfg


def _build(cfg: dict, is_example: bool) -> None:
    g = globals()
    g["CONFIG"] = cfg
    g["IS_EXAMPLE"] = is_example
    g["FAMILY_NAME"] = (cfg.get("family_name") or "Our household").strip()
    g["PARENTS"] = [p["name"].strip() for p in cfg["parents"]]
    g["PARENT_EMAILS"] = {p["name"].strip(): (p.get("email") or "").strip() for p in cfg["parents"]}
    g["KIDS"] = [k["name"].strip() for k in cfg["kids"]]
    g["KID_BIRTHDAYS"] = {k["name"].strip(): k["birthday"].strip()
                          for k in cfg["kids"] if (k.get("birthday") or "").strip()}
    g["KID_SCHOOLS"] = {k["name"].strip(): (k.get("school") or "").strip() for k in cfg["kids"]}
    # The mailbox the app reads and the calendar it writes to. The first parent unless the
    # file says otherwise.
    self_email = (cfg.get("self_email") or "").strip() or PARENT_EMAILS.get(PARENTS[0], "")
    g["SELF_EMAIL"] = self_email
    others = [p for p in PARENTS if PARENT_EMAILS.get(p) != self_email] or PARENTS[1:]
    g["PARTNER_NAME"] = others[0] if others else ""
    g["PARTNER_EMAIL"] = PARENT_EMAILS.get(PARTNER_NAME, "") if PARTNER_NAME else ""
    g["TRUSTED_ORGANIZERS"] = [e.lower() for e in
                               (cfg.get("trusted_organizers") or [PARTNER_EMAIL]) if e]
    g["COVERAGE_EXTRA"] = cfg.get("coverage_options") or list(DEFAULT_COVERAGE_EXTRA)
    g["HUB_URL"] = (cfg.get("hub_url") or "http://127.0.0.1:5088").rstrip("/")
    g["TIMEZONE"] = cfg.get("timezone") or "America/New_York"

    # Words that tie a calendar title to a kid: the kid's own name, any aliases (a
    # nickname, "1st grade"), and their school's name and short name.
    pats = []
    for k in cfg["kids"]:
        words = [k["name"]] + list(k.get("aliases") or [])
        for key in ("school", "school_short"):
            if (k.get(key) or "").strip():
                words.append(k[key])
        alt = "|".join(r"\b" + re.escape(w.strip().lower()).replace(r"\ ", r"\s*") + r"\b"
                       for w in words if w.strip())
        pats.append((k["name"].strip(), alt))
    g["KID_PATTERNS"] = pats

    # Tokens that carry no meaning when comparing two titles for the same day: the family's
    # own names and schools appear in one feed's wording and not the other's.
    stop = set()
    for n in PARENTS + KIDS:
        stop.add(n.lower())
    for k in cfg["kids"]:
        for key in ("school", "school_short"):
            stop.update(w.lower() for w in re.findall(r"[A-Za-z]+", k.get(key) or ""))
    g["NAME_TOKENS"] = stop

    # Sender rules seeded into the `sources` table on first run. A parent edits them later
    # at /mail/sources; the file only decides the starting set.
    srcs = []
    for s in cfg.get("sources") or []:
        months = s.get("active_months")
        if months == "school_year":
            months = SCHOOL_YEAR
        srcs.append((s["key"], s.get("name") or s["key"], s.get("kid") or "Both",
                     s["sender_match"], s.get("subject_match"), s.get("cadence") or "adhoc",
                     months or ALL_MONTHS, 1 if s.get("enabled", True) else 0,
                     s.get("keywords") or []))
    g["SOURCES"] = srcs


def reload(cfg: dict | None = None, is_example: bool = False) -> None:
    """Re-read the household (or install one, for a test)."""
    if cfg is None:
        cfg, is_example = _read()
    _build(validate(cfg), is_example)


def use_example() -> None:
    """Load the fictional example family. Every module self-test calls this first, so the
    tests pass the same way whatever a user has put in their own household.json."""
    reload(json.loads(EXAMPLE_FILE.read_text(encoding="utf-8")), True)


def kid_class(name: str | None) -> str:
    """CSS hook for a kid's chip color: kid-0, kid-1 ... by position; 'Both' is kid-both."""
    if name in KIDS:
        return f"kid-{KIDS.index(name)}"
    return "kid-both"


def kids_label(names) -> str:
    """'Ava + Leo', or 'Both kids' when it's every kid in a two-kid house."""
    names = [n for n in KIDS if n in set(names or [])]
    if len(KIDS) > 1 and len(names) == len(KIDS):
        return "Both kids" if len(KIDS) == 2 else "All the kids"
    return " + ".join(names)


def prompt_context() -> str:
    """The paragraph every model prompt carries about who the family is."""
    kids = []
    for k in CONFIG["kids"]:
        bits = [k["name"]]
        if k.get("grade"):
            bits.append(k["grade"])
        if k.get("school"):
            bits.append("at " + k["school"] + (f' ("{k["school_short"]}")' if k.get("school_short") else ""))
        kids.append(" ".join(bits))
    line = f"The household: {' and '.join(PARENTS)} (parents); kids: {'; '.join(kids)}."
    extra = (CONFIG.get("context") or "").strip()
    return line + (" " + extra if extra else "")


def school_keywords() -> list[str]:
    """Words that make a message from a broad sender (a partner forwarding mail) relevant."""
    words = [n.lower() for n in KIDS]
    for k in CONFIG["kids"]:
        for key in ("school", "school_short"):
            if (k.get(key) or "").strip():
                words.append(k[key].lower())
    return words


reload()


def _self_test() -> int:
    failures = 0

    def check(label, ok):
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + label)
        failures += 0 if ok else 1

    base = json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))
    reload(base, True)
    check("example loads", IS_EXAMPLE and len(KIDS) >= 1 and len(PARENTS) >= 1)
    check("self email is the first parent's by default",
          SELF_EMAIL == PARENT_EMAILS[PARENTS[0]] or bool(base.get("self_email")))
    check("partner is the other parent", PARTNER_NAME in PARENTS and PARTNER_NAME != PARENTS[0])
    k0 = KIDS[0]
    check("a kid's name matches its own pattern",
          re.search(dict(KID_PATTERNS)[k0], f"{k0.lower()} dentist") is not None)
    check("kid_class by position", kid_class(k0) == "kid-0" and kid_class("Both") == "kid-both")
    check("prompt context names every kid", all(k in prompt_context() for k in KIDS))
    for bad, why in [({**base, "kids": []}, "no kids"),
                     ({**base, "parents": [{"name": ""}]}, "blank parent"),
                     ({**base, "kids": [{"name": "Both"}]}, "reserved name"),
                     ({**base, "kids": [{"name": "Kim", "birthday": "3/14"}]}, "bad birthday")]:
        try:
            validate(bad)
            check(f"refuses {why}", False)
        except HouseholdError:
            check(f"refuses {why}", True)
    reload()
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(("EXAMPLE household (write data/household.json to make it yours)\n" if IS_EXAMPLE else ""))
    print(prompt_context())
    print("self email:", SELF_EMAIL or "(none)", "| partner:", PARTNER_NAME or "(none)")
    print("sources:", ", ".join(s[0] for s in SOURCES) or "(none)")
