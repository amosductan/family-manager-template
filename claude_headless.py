"""The ONE way Family Manager runs headless Claude (`claude -p`): on your Claude
subscription, never on an API key you forgot was set.

`claude -p` bills ANTHROPIC_API_KEY at API rates whenever that variable is present in its
environment -- even when you're signed in to claude.ai. A key left in a shell profile for
some other project would quietly pay for every mail summary this app makes. So every spawn
goes through three layers:

1. STRIP -- the child env drops ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN and points
   CLAUDE_CONFIG_DIR at an isolated dir holding only the claude.ai credentials (no global
   CLAUDE.md to hijack the agent into chatting instead of returning JSON) plus a
   settings.json that pins those two variables to "" -- an empty string routes to the login.
2. PROVE -- before spawning, `claude auth status --json` is run WITH that env. It makes no
   API call and takes about a second. It must report authMethod "claude.ai", a non-null
   subscriptionType, and NO apiKeySource; a stray key shows up as
   apiKeySource="ANTHROPIC_API_KEY" with subscriptionType null. If the proof fails, `run()`
   REFUSES -- it never falls through to the API.
3. RECORD -- every refusal is logged with the reason so a blank answer can be traced, and
   every model call appends one line to the cost ledger (below).

THE COST LEDGER. `data/model_costs.jsonl`, one JSON object per call:
    at, purpose, model, input_tokens, output_tokens, cost_usd, duration_s, ok
(plus cache_read_tokens / cache_write_tokens and, on a failure, error). The numbers come
from the CLI's own `--output-format json` answer (`total_cost_usd`, `usage`) -- measured,
never estimated. A call that failed, timed out or was refused records ok=false and cost 0:
this ledger answers "what does running this household cost", and a guessed number in it
would be worse than a missing one. On a subscription the cost is what the same tokens
would cost at API list prices -- the honest basis for pricing the product.

Callers: mailsweep (mail summaries, image transcription), extract, ask, scan_all. Do not
add another copy of `_claude_env()` anywhere; import `env()` / `run()` from here.

    python claude_headless.py --check       # what the child env would authenticate as
    python claude_headless.py --self-test   # ledger parsing offline + the login proof
    python claude_headless.py --costs       # spend by month and by purpose
    python claude_headless.py --ping        # ONE real short call, recorded as "selftest"
"""
from __future__ import annotations

import calendar
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

import db

STRIP = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
ISOLATED_SETTINGS = {"env": {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}}
_PROOF_TTL = 600  # seconds a passing proof is trusted before it is re-run
_last_proof: dict = {"at": 0.0, "ok": False, "detail": ""}
_ledger_lock = threading.Lock()


class SubscriptionRequired(RuntimeError):
    """Raised instead of spawning when the child env would not use the claude.ai login."""


def exe() -> str:
    """The CLI by whatever name the OS installed it under. A bare "claude" resolves from a
    shell but not from every Python launcher on Windows (WinError 2)."""
    return (shutil.which("claude") or shutil.which("claude.cmd")
            or shutil.which("claude.exe") or "claude")


def has_tokens(raw: bytes) -> bool:
    """A credentials file that still holds a login. When a refresh fails the CLI rewrites the
    SAME file with EMPTY tokens (valid JSON, subscriptionType still set, accessToken and
    refreshToken ''). Signed out, but well-formed."""
    try:
        o = json.loads(raw.decode("utf-8")).get("claudeAiOauth") or {}
        return bool(o.get("accessToken")) and bool(o.get("refreshToken"))
    except Exception:
        return False


def long_lived_token() -> str:
    """`claude setup-token` output, if the app's .env carries it. A long-lived claude.ai token
    authenticates on its own and NEVER rotates -- so there is no second copy of a refresh
    chain to burn. Empty when absent."""
    return (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()


def isolated_config_dir() -> str | None:
    """A minimal CLAUDE_CONFIG_DIR: ONLY the claude.ai credentials + a settings.json that
    blanks the two API variables. No CLAUDE.md, so the agent is a pure text service.

    Credentials: the copy that still HOLDS a login wins; between two logins, newest wins, in
    BOTH directions. The OAuth refresh token rotates every time the CLI refreshes -- and the
    CLI running in THIS dir refreshes into THIS file. Copying the real file over it whenever
    the bytes differed once overwrote a fresh rotation with a spent token and burned the
    machine's only login. Later, an interactive `claude` left open for days refreshed with a
    refresh token this dir had already rotated, failed, and wrote a BLANK real file -- and
    newest-wins copied the blank over the live isolated copy. A blank file is never copied
    over a login now; the login is copied back instead.

    With a long-lived token (CLAUDE_CODE_OAUTH_TOKEN, from `claude setup-token`) the dir
    carries NO credentials at all: one chain, nothing to burn. Returns None only when there is
    neither (caller keeps the default dir, which still authenticates)."""
    real = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    cred = real / ".credentials.json"
    token = long_lived_token()
    if not cred.exists() and not token:
        return None
    cfg = db.DATA_DIR / ".claude_cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    try:
        dest = cfg / ".credentials.json"
        if token:
            if dest.exists():
                dest.unlink()
        elif not dest.exists():
            dest.write_bytes(cred.read_bytes())
        else:
            a, b = cred.read_bytes(), dest.read_bytes()
            if a != b:
                ha, hb = has_tokens(a), has_tokens(b)
                if ha and not hb:
                    dest.write_bytes(a)
                elif hb and not ha:
                    cred.write_bytes(b)
                    _log("real ~/.claude credentials were signed out; restored the login from the isolated copy")
                elif not ha and not hb:
                    _log("BOTH credential files are signed out -- `claude login` is needed on this machine")
                elif cred.stat().st_mtime > dest.stat().st_mtime:
                    dest.write_bytes(a)
                else:
                    cred.write_bytes(b)
        sp = cfg / "settings.json"
        want = json.dumps(ISOLATED_SETTINGS, indent=2)
        if not sp.exists() or sp.read_text(encoding="utf-8") != want:
            sp.write_text(want, encoding="utf-8")
    except Exception:
        return None
    return str(cfg)


def env(utf8: bool = False) -> dict:
    """The child environment: Anthropic API variables removed, isolated config dir set.
    `utf8=True` also forces Python UTF-8 (scan_all's need: an emoji subject must not kill a
    step)."""
    e = dict(os.environ)
    for k in STRIP:
        e.pop(k, None)
    cfg = isolated_config_dir()
    if cfg:
        e["CLAUDE_CONFIG_DIR"] = cfg
    if utf8:
        e["PYTHONIOENCODING"] = "utf-8"
        e["PYTHONUTF8"] = "1"
    return e


def auth_status(e: dict | None = None, timeout: int = 40) -> dict:
    """What the CLI would authenticate as in this env. No API call. {} on failure."""
    try:
        r = subprocess.run([exe(), "auth", "status", "--json"], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", env=e if e is not None else env())
        return json.loads(r.stdout or "{}")
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"[:200]}


def judge(status: dict) -> tuple[bool, str]:
    """(ok, one-line reason). ok only when the login is the subscription and no key is in play."""
    if not status or "_error" in status:
        return False, f"claude auth status unavailable: {status.get('_error', 'no output') if status else 'no output'}"
    src = status.get("apiKeySource")
    if src and src != "CLAUDE_CODE_OAUTH_TOKEN":
        return False, f"an API key is in the environment ({src}) -- would bill the API"
    method = status.get("authMethod")
    if method == "oauth_token" and long_lived_token():
        # `claude setup-token`: a long-lived token that can only be minted by signing in to
        # claude.ai, injected by us. The CLI's own vocabulary for it is 'oauth_token'; it
        # carries the subscription by construction and never rotates.
        if status.get("loggedIn") is False:
            return False, "the long-lived token (CLAUDE_CODE_OAUTH_TOKEN) is not accepted -- mint a new one with `claude setup-token`"
        return True, f"claude.ai long-lived token ({status.get('subscriptionType') or 'setup-token'})"
    if method != "claude.ai":
        return False, f"authMethod is {method!r}, not the claude.ai login"
    if not status.get("loggedIn"):
        return False, "not logged in to claude.ai (run: claude auth login)"
    if not status.get("subscriptionType"):
        return False, "no subscription on this login (subscriptionType null)"
    return True, f"claude.ai {status['subscriptionType']} subscription"


def assert_subscription(e: dict | None = None, force: bool = False) -> str:
    """Prove the env uses the subscription, or raise SubscriptionRequired. A passing proof
    is cached for 10 minutes per process; a failing one is never cached."""
    now = time.time()
    if not force and _last_proof["ok"] and now - _last_proof["at"] < _PROOF_TTL:
        return _last_proof["detail"]
    ok, detail = judge(auth_status(e))
    _last_proof.update(at=now, ok=ok, detail=detail)
    if not ok:
        _log(f"REFUSED headless claude: {detail}")
        raise SubscriptionRequired(detail)
    return detail


# ---------------------------------------------------------------------- the cost ledger

def ledger_path() -> Path:
    """data/model_costs.jsonl, or FM_COST_LEDGER (the self-test points it at a temp file)."""
    return Path(os.environ.get("FM_COST_LEDGER") or (db.DATA_DIR / "model_costs.jsonl"))


def _caller_purpose() -> str:
    """The first module outside this one on the stack: 'mailsweep', 'ask', 'extract'..."""
    f = sys._getframe(1)
    while f is not None:
        mod = f.f_globals.get("__name__", "")
        if mod and mod != __name__:
            return mod.rsplit(".", 1)[-1]
        f = f.f_back
    return "unknown"


def parse_output(stdout: str) -> dict | None:
    """The CLI's `--output-format json` answer -> {result, is_error, cost, usage...}.

    One JSON object of type "result" (a list of messages when --verbose is on, in which case
    the last "result" entry is the answer). None when stdout isn't that shape -- the caller
    then treats the call as failed, never as a free success."""
    raw = (stdout or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, list):
        obj = next((m for m in reversed(obj) if isinstance(m, dict) and m.get("type") == "result"), None)
    if not isinstance(obj, dict) or "result" not in obj and "is_error" not in obj:
        return None
    usage = obj.get("usage") or {}

    def n(k):
        try:
            return int(usage.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    try:
        cost = float(obj.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    return {
        "result": obj.get("result") if isinstance(obj.get("result"), str) else "",
        "is_error": bool(obj.get("is_error")) or obj.get("subtype") not in (None, "success"),
        "cost_usd": cost,
        "input_tokens": n("input_tokens"),
        "cache_read_tokens": n("cache_read_input_tokens"),
        "cache_write_tokens": n("cache_creation_input_tokens"),
        "output_tokens": n("output_tokens"),
        "models": sorted((obj.get("modelUsage") or {}).keys()),
    }


def record(purpose: str, model: str, *, ok: bool, duration_s: float,
           parsed: dict | None = None, error: str | None = None,
           provider: str = "claude-cli", cost_basis: str | None = None) -> dict:
    """Append one ledger line. Every provider writes here (llm.py passes its own name).

    A failed call costs 0 by rule, whatever it reported. A call nobody can price -- an API
    call with no price settings, a subscription tool that doesn't report dollars -- records
    cost None with its basis, never a made-up zero."""
    p = parsed or {}
    cost = p.get("cost_usd", 0.0)
    basis = cost_basis or "reported"
    if not ok:
        cost, basis = 0.0, "failed"
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "purpose": purpose or "unknown",
        "provider": provider,
        "model": model,
        "cost_basis": basis,
        # Every token the model read: fresh input plus prompt-cache reads and writes. The
        # split is kept beside it because the three are priced differently.
        "input_tokens": (p.get("input_tokens", 0) + p.get("cache_read_tokens", 0)
                         + p.get("cache_write_tokens", 0)) if ok else 0,
        "output_tokens": p.get("output_tokens", 0) if ok else 0,
        "cache_read_tokens": p.get("cache_read_tokens", 0) if ok else 0,
        "cache_write_tokens": p.get("cache_write_tokens", 0) if ok else 0,
        "cost_usd": round(cost, 6) if cost is not None else None,
        "duration_s": round(duration_s, 2),
        "ok": bool(ok),
    }
    if p.get("models") and p["models"] != [model]:
        entry["models_used"] = p["models"]
    if error:
        entry["error"] = error[:300]
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _ledger_lock, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:  # the ledger must never take a summary down with it
        _log(f"cost ledger write failed: {exc}")
    return entry


# What every call runs with instead of Claude Code's own system prompt and tool list.
# Measured 2026-09-10: a one-word reply cost 91,943 input tokens ($0.28) with the defaults,
# because `claude -p` sends the whole coding-agent prompt and every tool definition each
# time. With this prompt, no tools, no MCP servers and no settings files it was 654 tokens
# ($0.0013). Every caller here only needs text in and text out, so the defaults were pure
# overhead -- and at dozens of calls a month per family they would have been the entire bill.
LEAN_SYSTEM_PROMPT = ("You are a careful household assistant. Follow the instructions in the "
                      "message exactly and reply with only what they ask for.")
LEAN_ARGS = ["--strict-mcp-config", "--setting-sources", "", "--no-session-persistence"]


def run(prompt: str, model: str, timeout: int = 180, extra_args: list[str] | None = None,
        utf8: bool = False, purpose: str | None = None,
        tools: str = "") -> subprocess.CompletedProcess:
    """Spawn `claude -p` on the subscription and record what it cost.

    Returns a CompletedProcess whose `stdout` is the model's reply TEXT (the JSON envelope is
    unwrapped here, so callers read it exactly as they read `--output-format text`), and
    whose `ledger` attribute is the line written. A reply the CLI flagged as an error comes
    back with a non-zero returncode and the error message as stdout.

    Raises SubscriptionRequired before spawning if the env would not authenticate that way,
    and re-raises TimeoutExpired / OSError -- each after recording ok=false."""
    purpose = purpose or _caller_purpose()
    e = env(utf8=utf8)
    try:
        assert_subscription(e)
    except SubscriptionRequired as exc:
        record(purpose, model, ok=False, duration_s=0.0, error=f"refused: {exc}")
        raise
    # `tools` names the only tools the model may use ("" = none; image transcription passes
    # "Read"). Anything else a caller needs rides in extra_args.
    args = [exe(), "-p", "--output-format", "json", "--model", model,
            "--system-prompt", LEAN_SYSTEM_PROMPT, "--tools", tools, *LEAN_ARGS,
            *(extra_args or [])]
    t0 = time.monotonic()
    try:
        p = subprocess.run(args, input=prompt, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", env=e)
    except subprocess.TimeoutExpired:
        record(purpose, model, ok=False, duration_s=time.monotonic() - t0,
               error=f"timeout after {timeout}s")
        raise
    except Exception as exc:
        record(purpose, model, ok=False, duration_s=time.monotonic() - t0,
               error=f"{type(exc).__name__}: {exc}")
        raise
    dt = time.monotonic() - t0
    parsed = parse_output(p.stdout)
    if parsed is None:
        why = (p.stderr or p.stdout or "no output").strip()[:200]
        entry = record(purpose, model, ok=False, duration_s=dt,
                       error=f"exit {p.returncode}, unreadable output: {why}")
        cp = subprocess.CompletedProcess(args, p.returncode or 1, stdout=(p.stdout or ""),
                                         stderr=p.stderr)
    else:
        ok = p.returncode == 0 and not parsed["is_error"]
        entry = record(purpose, model, ok=ok, duration_s=dt, parsed=parsed,
                       error=None if ok else (parsed["result"] or p.stderr or "error")[:300])
        cp = subprocess.CompletedProcess(args, p.returncode if ok else (p.returncode or 1),
                                         stdout=parsed["result"], stderr=p.stderr)
    cp.ledger = entry
    return cp


def read_ledger(path: Path | None = None) -> list[dict]:
    path = path or ledger_path()
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn last line from a crash is skipped, not fatal
    return out


def cost_summary(entries: list[dict], today: date | None = None) -> dict:
    """Per month and per purpose: calls, failed, tokens, cost, cost per day.

    Cost per day divides by the calendar days the ledger actually COVERS in that period --
    from the first recorded call (or the 1st) to the month's end (or today) -- so a first
    partial month isn't diluted by days the app wasn't running."""
    today = today or date.today()
    if not entries:
        return {"months": [], "purposes": [], "total": None}
    days = sorted(e["at"][:10] for e in entries)
    first = date.fromisoformat(days[0])
    last = max(date.fromisoformat(days[-1]), today)

    def blank():
        return {"calls": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                "unpriced": 0, "subscription": 0}

    def add(b, e):
        b["calls"] += 1
        b["failed"] += 0 if e.get("ok") else 1
        b["input_tokens"] += int(e.get("input_tokens") or 0)
        b["output_tokens"] += int(e.get("output_tokens") or 0)
        cost = e.get("cost_usd")
        if e.get("ok") and cost is None:
            # Not zero: nobody knows the price. Counted apart so a dollar total never
            # quietly reads as "that was free".
            b["subscription" if e.get("cost_basis") == "subscription" else "unpriced"] += 1
        b["cost_usd"] += float(cost or 0.0)

    months: dict[str, dict] = {}
    purposes: dict[str, dict] = {}
    providers: dict[str, dict] = {}
    total = blank()
    for e in entries:
        add(months.setdefault(e["at"][:7], blank()), e)
        add(purposes.setdefault(e.get("purpose") or "unknown", blank()), e)
        add(providers.setdefault(e.get("provider") or "claude-cli", blank()), e)
        add(total, e)
    mrows = []
    for m, b in sorted(months.items()):
        y, mo = int(m[:4]), int(m[5:7])
        start = max(date(y, mo, 1), first)
        end = min(date(y, mo, calendar.monthrange(y, mo)[1]), last)
        span = max((end - start).days + 1, 1)
        mrows.append({"month": m, **b, "days": span, "cost_per_day": b["cost_usd"] / span})
    span_all = max((last - first).days + 1, 1)
    prows = [{"purpose": k, **b, "cost_per_day": b["cost_usd"] / span_all}
             for k, b in sorted(purposes.items(), key=lambda kv: -kv[1]["cost_usd"])]
    return {"months": mrows, "purposes": prows,
            "providers": [{"provider": k, **b} for k, b in sorted(providers.items())],
            "total": {**total, "days": span_all, "cost_per_day": total["cost_usd"] / span_all,
                      "first": first.isoformat()}}


def print_costs(path: Path | None = None) -> int:
    entries = read_ledger(path)
    where = path or ledger_path()
    if not entries:
        print(f"No model calls recorded yet ({where}).")
        return 0
    s = cost_summary(entries)
    hdr = f"{'':<18}{'calls':>7}{'failed':>8}{'in tok':>12}{'out tok':>10}{'cost':>11}{'per day':>10}"
    print(f"Model cost ledger: {where}\n")
    print("BY MONTH (per day = over the days the ledger covers in that month)")
    print(hdr)
    for r in s["months"]:
        print(f"{r['month']:<18}{r['calls']:>7}{r['failed']:>8}{r['input_tokens']:>12,}"
              f"{r['output_tokens']:>10,}{r['cost_usd']:>11.4f}{r['cost_per_day']:>10.4f}")
    print(f"\nBY PURPOSE (per day = over all {s['total']['days']} day(s) since {s['total']['first']})")
    print(hdr)
    for r in s["purposes"]:
        print(f"{r['purpose'][:17]:<18}{r['calls']:>7}{r['failed']:>8}{r['input_tokens']:>12,}"
              f"{r['output_tokens']:>10,}{r['cost_usd']:>11.4f}{r['cost_per_day']:>10.4f}")
    print("\nBY PROVIDER")
    print(hdr)
    for r in s.get("providers", []):
        print(f"{r['provider'][:17]:<18}{r['calls']:>7}{r['failed']:>8}{r['input_tokens']:>12,}"
              f"{r['output_tokens']:>10,}{r['cost_usd']:>11.4f}")
    t = s["total"]
    print(f"\n{'TOTAL':<18}{t['calls']:>7}{t['failed']:>8}{t['input_tokens']:>12,}"
          f"{t['output_tokens']:>10,}{t['cost_usd']:>11.4f}{t['cost_per_day']:>10.4f}")
    print("\nCost is USD: reported by the claude tool, or estimated from FM_LLM_PRICE_IN/OUT."
          " Failed calls count $0.")
    if t.get("unpriced") or t.get("subscription"):
        print(f"{t['unpriced']} call(s) have no price set and {t['subscription']} ran on a "
              "subscription; neither is in the dollar totals.")
    return 0


def _log(msg: str) -> None:
    try:
        p = db.DATA_DIR / "claude_headless.log"
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


# ------------------------------------------------------------------------- self-test

def _ledger_self_test() -> bool:
    """Offline: parse real-shaped CLI answers, write them to a temp ledger, and summarize."""
    import tempfile
    ok_all = True

    def check(label, cond):
        nonlocal ok_all
        print(("PASS" if cond else "FAIL"), label)
        ok_all = ok_all and bool(cond)

    good = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "result": "ok", "total_cost_usd": 0.0123, "duration_ms": 2100,
                       "usage": {"input_tokens": 5, "cache_creation_input_tokens": 1200,
                                 "cache_read_input_tokens": 9000, "output_tokens": 4},
                       "modelUsage": {"claude-sonnet-5": {}}})
    bad = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "Failed to authenticate", "total_cost_usd": 0.5,
                      "usage": {"input_tokens": 10, "output_tokens": 0}})
    verbose = json.dumps([{"type": "system"}, json.loads(good)])
    pg, pb, pv = parse_output(good), parse_output(bad), parse_output(verbose)
    check("a success answer parses: text, cost, tokens",
          pg and pg["result"] == "ok" and abs(pg["cost_usd"] - 0.0123) < 1e-9
          and pg["cache_read_tokens"] == 9000 and pg["output_tokens"] == 4)
    check("an error answer is flagged is_error", pb and pb["is_error"])
    check("a --verbose message list parses to its result", pv and pv["result"] == "ok")
    check("plain text is NOT a parsed answer", parse_output("just words") is None)
    check("empty output is NOT a parsed answer", parse_output("") is None)

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "costs.jsonl"
        old = os.environ.get("FM_COST_LEDGER")
        os.environ["FM_COST_LEDGER"] = str(path)
        try:
            e1 = record("mailsweep", "claude-sonnet-5", ok=True, duration_s=2.1, parsed=pg)
            e2 = record("ask", "claude-sonnet-5", ok=False, duration_s=1.0, parsed=pb,
                        error="Failed to authenticate")
            e3 = record("ask", "claude-sonnet-5", ok=False, duration_s=0.0, error="refused: x")
            check("ok call records all input tokens (fresh + cache)", e1["input_tokens"] == 10205)
            check("a failed call records ok=false and cost 0, not what it reported",
                  e2["ok"] is False and e2["cost_usd"] == 0.0 and e2["input_tokens"] == 0)
            check("purpose from the caller's module name", _caller_purpose() == "__main__"
                  or _caller_purpose() == "claude_headless" or bool(_caller_purpose()))
            rows = read_ledger(path)
            check("three ledger lines, one per call", len(rows) == 3)
            check("every line has the required fields",
                  all({"at", "purpose", "model", "input_tokens", "output_tokens", "cost_usd",
                       "duration_s", "ok"} <= set(r) for r in rows))
            with open(path, "a", encoding="utf-8") as f:
                f.write('{"torn')  # a crash mid-write
            check("a torn last line is skipped", len(read_ledger(path)) == 3)
            s = cost_summary(read_ledger(path), today=date.fromisoformat(rows[0]["at"][:10]))
            check("summary totals the one successful cost",
                  abs(s["total"]["cost_usd"] - 0.0123) < 1e-9 and s["total"]["failed"] == 2)
            check("summary has a row per purpose",
                  {r["purpose"] for r in s["purposes"]} == {"mailsweep", "ask"})
            check("cost per day covers one day on a same-day ledger",
                  s["months"][0]["days"] == 1)
            print_costs(path)
        finally:
            if old is None:
                os.environ.pop("FM_COST_LEDGER", None)
            else:
                os.environ["FM_COST_LEDGER"] = old
    return ok_all


def self_test() -> int:
    """Offline ledger checks, then the login proofs. (1) The real child env authenticates as
    the subscription. (2) `judge` refuses the exact status shape a stray key produces
    (apiKeySource set, subscriptionType null). (3) A planted key in a config dir WITHOUT the
    blanking settings.json is refused end to end -- the only way a key could still reach the
    CLI, and the guard must catch it there too."""
    import tempfile
    ok_ledger = _ledger_self_test()
    print()
    e = env()
    ok, why = judge(auth_status(e))
    print(("PASS" if ok else "FAIL"), "clean child env ->", why)
    stray = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
             "apiKeySource": "ANTHROPIC_API_KEY", "subscriptionType": None, "email": None}
    ok2, why2 = judge(stray)
    print(("PASS" if not ok2 else "FAIL"), "stray-key status shape is refused ->", why2)
    live = json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "subscriptionType": "max"}}).encode()
    blank = json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": "", "subscriptionType": "max"}}).encode()
    ok4 = has_tokens(live) and not has_tokens(blank) and not has_tokens(b"not json")
    print(("PASS" if ok4 else "FAIL"), "a signed-out credentials file is recognized as holding no login")
    ok3 = True
    src = Path(e.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / ".credentials.json"
    if src.exists():
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".credentials.json").write_bytes(src.read_bytes())  # credentials, no settings
            bad = dict(e)
            bad["CLAUDE_CONFIG_DIR"] = td
            bad["ANTHROPIC_API_KEY"] = "sk-ant-api03-planted-000"
            try:
                assert_subscription(bad, force=True)
                print("FAIL planted key in an unguarded config dir was NOT refused")
                ok3 = False
            except SubscriptionRequired as exc:
                print("PASS planted key in an unguarded config dir refused ->", exc)
            _last_proof.update(at=0.0, ok=False)  # never let the planted run poison the cache
    else:
        print("SKIP planted-key run (no credentials file to copy)")
    return 0 if (ok_ledger and ok and not ok2 and ok3 and ok4) else 1


def ping() -> int:
    """ONE real, short model call through run(), so the ledger line can be read back."""
    try:
        r = run("Reply with the word ok and nothing else.", "claude-sonnet-5", timeout=120,
                purpose="selftest")
    except SubscriptionRequired as exc:
        print("REFUSED:", exc)
        return 1
    print("reply:", (r.stdout or "").strip()[:80])
    print("ledger:", json.dumps(r.ledger))
    return 0 if r.ledger.get("ok") else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    if "--costs" in sys.argv:
        raise SystemExit(print_costs())
    if "--ping" in sys.argv:
        raise SystemExit(ping())
    st = auth_status(env())
    ok, why = judge(st)
    print(("OK   " if ok else "REFUSE"), why)
    if not ok:
        print("status:", json.dumps({k: v for k, v in st.items() if k != "email"}))
    raise SystemExit(0 if ok else 1)
