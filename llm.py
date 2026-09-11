"""Which model answers, and what every answer cost.

Every model call in the app goes through complete(): the mail briefing, reading a
screenshot, Ask, and the optional extraction pass. The household picks the provider in
.env, and nothing else in the code names one.

    FM_LLM_PROVIDER   claude-cli | codex-cli | anthropic | openai | gemini
    FM_LLM_MODEL      optional; each provider has a default (DEFAULT_MODELS)
    FM_LLM_BASE_URL   openai only: any OpenAI-compatible endpoint (Ollama, LM Studio, OpenRouter)
    FM_LLM_PRICE_IN, FM_LLM_PRICE_OUT   USD per million tokens, to estimate API spend

Subscriptions first. With FM_LLM_PROVIDER unset, the app uses a signed-in `claude` or
`codex` command-line tool (a Claude or ChatGPT plan), and never an API key it happens to
find in the environment. An API provider runs only when FM_LLM_PROVIDER names it, so a key
left in a shell profile can't start billing on its own.

    python llm.py --which       # the provider and model this machine would use
    python llm.py --check       # one short live call through it
    python llm.py --costs       # spend by month and purpose
    python llm.py --self-test   # offline
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

import _env  # noqa: F401
import claude_headless

PROVIDERS = ("claude-cli", "codex-cli", "anthropic", "openai", "gemini")
# Checked against each provider's own model list on 2026-09-10, never guessed. codex-cli
# leaves it to the codex tool's own default unless FM_LLM_MODEL says otherwise.
DEFAULT_MODELS = {"claude-cli": "claude-sonnet-5", "anthropic": "claude-sonnet-5",
                  "openai": "gpt-5.4-mini", "gemini": "gemini-3.5-flash", "codex-cli": ""}
# The app's own variable wins, so a key meant for this app never has to live under a name
# every other tool on the machine also reads.
KEY_VARS = {"anthropic": ("FM_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
            "openai": ("FM_OPENAI_API_KEY", "OPENAI_API_KEY"),
            "gemini": ("FM_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")}
SYSTEM = claude_headless.LEAN_SYSTEM_PROMPT
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class ProviderError(RuntimeError):
    """The configuration names something the app can't use."""


@dataclass
class Result:
    text: str
    ok: bool
    error: str
    provider: str
    model: str
    ledger: dict = field(default_factory=dict)


# ------------------------------------------------------------------ which provider, which model

def provider() -> str:
    p = (os.environ.get("FM_LLM_PROVIDER") or "").strip().lower()
    if p:
        if p not in PROVIDERS:
            raise ProviderError(f"FM_LLM_PROVIDER={p!r} isn't one of: {', '.join(PROVIDERS)}")
        return p
    if shutil.which("claude"):
        return "claude-cli"
    if shutil.which("codex"):
        return "codex-cli"
    return "none"


def model(p: str | None = None) -> str:
    p = p or provider()
    set_model = (os.environ.get("FM_LLM_MODEL") or "").strip()
    if set_model:
        return set_model
    if p == "openai" and os.environ.get("FM_LLM_BASE_URL"):
        return ""  # another server's model names aren't OpenAI's; FM_LLM_MODEL must say
    return DEFAULT_MODELS.get(p, "")


def describe() -> str:
    p = provider()
    return f"{p}:{model(p) or 'default'}"


def _key(p: str) -> str:
    for var in KEY_VARS[p]:
        v = (os.environ.get(var) or "").strip()
        if v:
            return v
    return ""


def _price(inp: int, out: int) -> tuple[float | None, str]:
    """Estimated USD from the household's own price settings, or (None, 'unpriced'). The app
    ships no price table: a stale price quoted as fact is worse than an honest blank."""
    try:
        pin = float(os.environ["FM_LLM_PRICE_IN"])
        pout = float(os.environ["FM_LLM_PRICE_OUT"])
    except (KeyError, ValueError):
        return None, "unpriced"
    return round((inp * pin + out * pout) / 1_000_000, 6), "estimated"


def _caller_purpose() -> str:
    f = sys._getframe(1)
    while f is not None:
        mod = f.f_globals.get("__name__", "")
        if mod and mod not in (__name__, "claude_headless"):
            return mod.rsplit(".", 1)[-1]
        f = f.f_back
    return "unknown"


def _image(path) -> tuple[str, str]:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return mime, base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _record(purpose: str, p: str, m: str, t0: float, *, ok: bool, inp: int = 0, out: int = 0,
            cached: int = 0, cost: float | None = None, basis: str | None = None,
            error: str | None = None) -> dict:
    return claude_headless.record(
        purpose, m or "default", ok=ok, duration_s=time.monotonic() - t0,
        parsed={"input_tokens": max(inp - cached, 0), "cache_read_tokens": cached,
                "output_tokens": out, "cost_usd": cost},
        error=error, provider=p, cost_basis=basis)


def _fail(purpose: str, p: str, m: str, t0: float, why: str) -> Result:
    return Result("", False, why, p, m, _record(purpose, p, m, t0, ok=False, error=why))


# ------------------------------------------------------------------------------ the one entry

def complete(prompt: str, purpose: str | None = None, timeout: int = 180,
             image: str | Path | None = None) -> Result:
    """Ask the configured model. Never raises for a model problem: a failed call comes back
    with ok=False and a reason a person can act on, and is recorded in the ledger at $0."""
    purpose = purpose or _caller_purpose()
    if os.environ.get("FM_NO_MODEL") == "1" or os.environ.get("FM_NO_CLAUDE") == "1":
        return Result("", False, "model calls are switched off (FM_NO_MODEL=1)", "off", "")
    t0 = time.monotonic()
    try:
        p = provider()
    except ProviderError as exc:
        return Result("", False, str(exc), "misconfigured", "")
    m = model(p)
    if p == "none":
        return Result("", False, "no model provider: sign in to the claude or codex command-line "
                      "tool, or set FM_LLM_PROVIDER and its API key in .env", "none", "")
    fn = {"claude-cli": _claude_cli, "codex-cli": _codex_cli, "anthropic": _anthropic,
          "openai": _openai, "gemini": _gemini}[p]
    try:
        return fn(prompt, m, timeout, image, purpose, t0)
    except subprocess.TimeoutExpired:
        return _fail(purpose, p, m, t0, f"no answer within {timeout}s")
    except requests.Timeout:
        return _fail(purpose, p, m, t0, f"no answer within {timeout}s")
    except requests.RequestException as exc:
        return _fail(purpose, p, m, t0, f"couldn't reach {p}: {type(exc).__name__}")
    except FileNotFoundError as exc:
        return _fail(purpose, p, m, t0, f"not installed: {exc}")


def preflight() -> tuple[bool, str]:
    """Can a call go out right now? Cheap enough to run before a batch (the scan uses it)."""
    try:
        p = provider()
    except ProviderError as exc:
        return False, str(exc)
    if p == "none":
        return False, "no model provider configured"
    if p == "claude-cli":
        try:
            return True, claude_headless.assert_subscription(claude_headless.env())
        except Exception as exc:
            return False, f"claude: {exc}"
    if p == "codex-cli":
        ok, why = _codex_login(_codex_env())
        return ok, why
    if p == "openai" and os.environ.get("FM_LLM_BASE_URL"):
        return bool(model(p)), (f"{os.environ['FM_LLM_BASE_URL']} with {model(p)}"
                                if model(p) else "FM_LLM_BASE_URL is set but FM_LLM_MODEL isn't")
    return (True, f"{p} key found") if _key(p) else (False, f"no API key: set {KEY_VARS[p][0]} in .env")


# ------------------------------------------------------------------------------ the providers

def _claude_cli(prompt, m, timeout, image, purpose, t0) -> Result:
    tools, extra = "", []
    if image:
        prompt = f"{prompt}\n\nThe image is the file at this path. Open it with your Read tool:\n{image}"
        tools, extra = "Read", ["--allowedTools", "Read"]
    try:
        r = claude_headless.run(prompt, m, timeout=timeout, purpose=purpose, tools=tools, extra_args=extra)
    except claude_headless.SubscriptionRequired as exc:
        return Result("", False, f"claude isn't signed in on a subscription here: {exc}", "claude-cli", m)
    out = (r.stdout or "").strip()
    low = out.lower()
    if "credit balance is too low" in low:
        err = "claude: credit balance too low (an ANTHROPIC_API_KEY is overriding the login)"
    elif "failed to authenticate" in low or "oauth session expired" in low:
        err = "the Claude login on this machine has expired: run `claude login`"
    elif r.returncode != 0 or not out:
        err = f"claude exit {r.returncode}: {((r.stderr or '') or out or 'empty output').strip()[:200]}"
    else:
        return Result(out, True, "", "claude-cli", m, getattr(r, "ledger", {}))
    return Result("", False, err, "claude-cli", m, getattr(r, "ledger", {}))


_CODEX_LOGIN: tuple[bool, str] | None = None


def _codex_env() -> dict:
    """The child env without any OpenAI key, so codex answers on the ChatGPT plan it's signed
    in with instead of billing a key -- the same guarantee claude_headless gives for Claude."""
    return {k: v for k, v in os.environ.items()
            if not k.upper().startswith(("OPENAI_API_KEY", "FM_OPENAI_API_KEY"))}


def _codex_login(env: dict) -> tuple[bool, str]:
    global _CODEX_LOGIN
    if _CODEX_LOGIN is None:
        exe = shutil.which("codex")
        if not exe:
            _CODEX_LOGIN = (False, "the codex command-line tool isn't installed")
        else:
            try:
                s = subprocess.run([exe, "login", "status"], capture_output=True, text=True,
                                   timeout=40, env=env, encoding="utf-8")
                text = ((s.stdout or "") + (s.stderr or "")).strip()
            except Exception as exc:
                text = f"{type(exc).__name__}: {exc}"
            ok = "chatgpt" in text.lower()
            _CODEX_LOGIN = (ok, text[:160] if ok else
                            f"codex isn't signed in with a ChatGPT plan ({text[:120] or 'no answer'}); "
                            "run `codex login`")
    return _CODEX_LOGIN


def parse_codex(stdout: str) -> dict:
    """`codex exec --json` prints one event per line. The answer is the last agent_message,
    the tokens ride on turn.completed, and an error or turn.failed event means no answer."""
    text, usage, error = "", {}, ""
    for line in (stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        item = ev.get("item") or {}
        if t == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text") or text
        elif t == "turn.completed":
            usage = ev.get("usage") or {}
        elif t in ("error", "turn.failed"):
            error = (ev.get("message") or (ev.get("error") or {}).get("message") or t)[:200]
    return {"text": text.strip(), "usage": usage, "error": error}


def _codex_cli(prompt, m, timeout, image, purpose, t0) -> Result:
    exe = shutil.which("codex")
    if not exe:
        return _fail(purpose, "codex-cli", m, t0, "the codex command-line tool isn't installed")
    env = _codex_env()
    ok, why = _codex_login(env)
    if not ok:
        return _fail(purpose, "codex-cli", m, t0, why)
    # An empty working folder: codex reads an AGENTS.md from where it runs, and this repo's
    # is a setup script, not instructions for a summary.
    with tempfile.TemporaryDirectory() as work:
        args = [exe, "exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only",
                "--ephemeral", "--ignore-user-config", "-C", work]
        if m:
            args += ["-m", m]
        if image:
            args += ["-i", str(image)]
        args.append("-")
        r = subprocess.run(args, input=SYSTEM + "\n\n" + prompt, capture_output=True, text=True,
                           encoding="utf-8", timeout=timeout, env=env)
    parsed = parse_codex(r.stdout)
    if r.returncode != 0 or parsed["error"] or not parsed["text"]:
        why = parsed["error"] or (r.stderr or "").strip()[-200:] or f"codex exit {r.returncode}, no answer"
        return _fail(purpose, "codex-cli", m, t0, why)
    u = parsed["usage"]
    inp = int(u.get("input_tokens") or 0)
    out = int(u.get("output_tokens") or 0) + int(u.get("reasoning_output_tokens") or 0)
    entry = _record(purpose, "codex-cli", m, t0, ok=True, inp=inp, out=out,
                    cached=int(u.get("cached_input_tokens") or 0), cost=None, basis="subscription")
    return Result(parsed["text"], True, "", "codex-cli", m, entry)


def _post(url: str, body: dict, headers: dict, timeout: int) -> tuple[dict | None, str]:
    r = requests.post(url, json=body, headers={"content-type": "application/json", **headers},
                      timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code != 200:
        msg = ""
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]  # some endpoints wrap the error object in a list
        if isinstance(data, dict):
            err = data.get("error")
            msg = err.get("message") if isinstance(err, dict) else (err or "")
        return None, f"HTTP {r.status_code}: {(msg or r.text or '')[:200]}"
    if not isinstance(data, dict):
        return None, "the reply wasn't JSON"
    return data, ""


def _anthropic(prompt, m, timeout, image, purpose, t0) -> Result:
    key = _key("anthropic")
    if not key:
        return _fail(purpose, "anthropic", m, t0, "no API key: set FM_ANTHROPIC_API_KEY in .env")
    content = []
    if image:
        mime, b64 = _image(image)
        content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
    content.append({"type": "text", "text": prompt})
    body = {"model": m, "max_tokens": 8000, "system": SYSTEM,
            "messages": [{"role": "user", "content": content}]}
    data, err = _post(ANTHROPIC_URL, body, {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout)
    if err:
        return _fail(purpose, "anthropic", m, t0, f"anthropic {err}")
    text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text").strip()
    if not text:
        return _fail(purpose, "anthropic", m, t0, f"anthropic returned no text (stop: {data.get('stop_reason')})")
    u = data.get("usage") or {}
    cached = int(u.get("cache_read_input_tokens") or 0)
    inp = int(u.get("input_tokens") or 0) + cached + int(u.get("cache_creation_input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    cost, basis = _price(inp, out)
    return Result(text, True, "", "anthropic", m,
                  _record(purpose, "anthropic", m, t0, ok=True, inp=inp, out=out, cached=cached,
                          cost=cost, basis=basis))


def _openai(prompt, m, timeout, image, purpose, t0) -> Result:
    base = (os.environ.get("FM_LLM_BASE_URL") or OPENAI_URL).rstrip("/")
    key = _key("openai")
    if not m:
        return _fail(purpose, "openai", m, t0, "FM_LLM_BASE_URL is set, so FM_LLM_MODEL must name its model")
    if not key and base == OPENAI_URL:
        return _fail(purpose, "openai", m, t0, "no API key: set FM_OPENAI_API_KEY in .env")
    user = [{"type": "text", "text": prompt}]
    if image:
        mime, b64 = _image(image)
        user.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    body = {"model": m, "messages": [{"role": "system", "content": SYSTEM},
                                     {"role": "user", "content": user}]}
    data, err = _post(base + "/chat/completions", body,
                      {"Authorization": f"Bearer {key}"} if key else {}, timeout)
    if err:
        return _fail(purpose, "openai", m, t0, f"openai {err}")
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return _fail(purpose, "openai", m, t0, "openai reply had no choices")
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    text = (content or "").strip()
    if not text:
        return _fail(purpose, "openai", m, t0, f"openai returned no text ({msg.get('refusal') or 'empty'})")
    u = data.get("usage") or {}
    inp = int(u.get("prompt_tokens") or 0)
    out = int(u.get("completion_tokens") or 0)
    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    cost, basis = _price(inp, out)
    return Result(text, True, "", "openai", m,
                  _record(purpose, "openai", m, t0, ok=True, inp=inp, out=out, cached=cached,
                          cost=cost, basis=basis))


def _gemini(prompt, m, timeout, image, purpose, t0) -> Result:
    key = _key("gemini")
    if not key:
        return _fail(purpose, "gemini", m, t0, "no API key: set FM_GEMINI_API_KEY in .env")
    parts = [{"text": prompt}]
    if image:
        mime, b64 = _image(image)
        parts.append({"inlineData": {"mimeType": mime, "data": b64}})
    body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": parts}]}
    # The key rides in a header, never the URL: a URL ends up in logs and proxies.
    data, err = _post(GEMINI_URL.format(model=m), body, {"x-goog-api-key": key}, timeout)
    if err:
        return _fail(purpose, "gemini", m, t0, f"gemini {err}")
    cands = data.get("candidates") or []
    if not cands:
        block = (data.get("promptFeedback") or {}).get("blockReason") or "no candidates"
        return _fail(purpose, "gemini", m, t0, f"gemini returned no answer ({block})")
    text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts") or []
                   if not p.get("thought")).strip()
    if not text:
        return _fail(purpose, "gemini", m, t0, f"gemini returned no text ({cands[0].get('finishReason')})")
    u = data.get("usageMetadata") or {}
    inp = int(u.get("promptTokenCount") or 0)
    out = int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0)
    cached = int(u.get("cachedContentTokenCount") or 0)
    cost, basis = _price(inp, out)
    return Result(text, True, "", "gemini", m,
                  _record(purpose, "gemini", m, t0, ok=True, inp=inp, out=out, cached=cached,
                          cost=cost, basis=basis))


# ------------------------------------------------------------------------------ self-test

class _FakeResponse:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _self_test() -> int:
    import contextlib
    failures = 0
    passed = 0

    def check(label, ok):
        nonlocal failures, passed
        print(("PASS " if ok else "FAIL ") + label)
        passed += 1 if ok else 0
        failures += 0 if ok else 1

    tmp = tempfile.mkdtemp()
    ledger = Path(tmp) / "costs.jsonl"
    saved = {k: os.environ.get(k) for k in ("FM_LLM_PROVIDER", "FM_LLM_MODEL", "FM_LLM_BASE_URL",
                                            "FM_LLM_PRICE_IN", "FM_LLM_PRICE_OUT", "FM_COST_LEDGER",
                                            "FM_ANTHROPIC_API_KEY", "FM_OPENAI_API_KEY",
                                            "FM_GEMINI_API_KEY", "OPENAI_API_KEY", "FM_NO_MODEL",
                                            "FM_NO_CLAUDE")}
    for k in saved:
        os.environ.pop(k, None)
    os.environ["FM_COST_LEDGER"] = str(ledger)
    real_post, real_which = requests.post, shutil.which
    calls = []

    def fake_post(status, payload):
        def _p(url, json=None, headers=None, timeout=None):
            calls.append({"url": url, "json": json, "headers": headers or {}})
            return _FakeResponse(status, payload)
        return _p

    def last_ledger():
        return claude_headless.read_ledger(ledger)[-1]

    try:
        # Which provider
        shutil.which = lambda name: None
        os.environ["OPENAI_API_KEY"] = "sk-test"
        check("unset + no CLI tools -> none, even with an OpenAI key in the environment",
              provider() == "none")
        shutil.which = lambda name: "/bin/codex" if name == "codex" else None
        check("unset + codex installed -> codex-cli", provider() == "codex-cli")
        shutil.which = lambda name: "/bin/" + name
        check("unset + claude installed -> claude-cli first", provider() == "claude-cli")
        os.environ["FM_LLM_PROVIDER"] = "gpt"
        try:
            provider()
            check("an unknown provider is refused by name", False)
        except ProviderError as exc:
            check("an unknown provider is refused by name", "gpt" in str(exc))
        os.environ["FM_LLM_PROVIDER"] = "gemini"
        check("gemini default model", model() == "gemini-3.5-flash")
        os.environ["FM_LLM_MODEL"] = "gemini-2.5-flash"
        check("FM_LLM_MODEL overrides the default", model() == "gemini-2.5-flash")
        os.environ.pop("FM_LLM_MODEL")

        # Pricing
        check("no prices set -> unpriced, not zero", _price(1000, 100) == (None, "unpriced"))
        os.environ["FM_LLM_PRICE_IN"], os.environ["FM_LLM_PRICE_OUT"] = "2", "8"
        check("prices set -> estimated", _price(1_000_000, 500_000) == (6.0, "estimated"))
        os.environ.pop("FM_LLM_PRICE_IN"); os.environ.pop("FM_LLM_PRICE_OUT")

        img = Path(tmp) / "note.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake")

        # Anthropic
        os.environ["FM_LLM_PROVIDER"] = "anthropic"
        r = complete("hi", purpose="t")
        check("anthropic without a key fails by name, costs 0",
              not r.ok and "FM_ANTHROPIC_API_KEY" in r.error and last_ledger()["cost_usd"] == 0.0)
        os.environ["FM_ANTHROPIC_API_KEY"] = "ak-test"
        requests.post = fake_post(200, {"content": [{"type": "text", "text": "hello"}],
                                        "usage": {"input_tokens": 50, "cache_read_input_tokens": 10,
                                                  "output_tokens": 7}, "stop_reason": "end_turn"})
        r = complete("hi", purpose="t", image=img)
        c = calls[-1]
        blocks = c["json"]["messages"][0]["content"]
        e = last_ledger()
        check("anthropic answers", r.ok and r.text == "hello" and r.provider == "anthropic")
        check("anthropic sends the key in a header and the image as base64",
              c["headers"].get("x-api-key") == "ak-test" and blocks[0]["type"] == "image"
              and blocks[0]["source"]["type"] == "base64")
        check("anthropic ledger: provider, tokens incl. cache, unpriced",
              e["provider"] == "anthropic" and e["input_tokens"] == 60 and e["output_tokens"] == 7
              and e["cost_usd"] is None and e["cost_basis"] == "unpriced")

        # OpenAI
        os.environ["FM_LLM_PROVIDER"] = "openai"
        os.environ["FM_OPENAI_API_KEY"] = "ok-test"
        requests.post = fake_post(200, {"choices": [{"message": {"content": "hey"}}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                                  "prompt_tokens_details": {"cached_tokens": 40}}})
        r = complete("hi", purpose="t", image=img)
        c = calls[-1]
        e = last_ledger()
        check("openai answers on the default model",
              r.ok and r.text == "hey" and c["json"]["model"] == "gpt-5.4-mini"
              and c["url"].endswith("/chat/completions"))
        check("openai sends the image as a data URL",
              c["json"]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        check("openai ledger splits cached tokens",
              e["input_tokens"] == 100 and e["cache_read_tokens"] == 40 and e["output_tokens"] == 20)
        requests.post = fake_post(429, {"error": {"message": "Rate limit reached"}})
        r = complete("hi", purpose="t")
        e = last_ledger()
        check("an HTTP error is a failed call with the reason, at $0",
              not r.ok and "429" in r.error and "Rate limit" in r.error and e["cost_usd"] == 0.0
              and e["ok"] is False)
        os.environ["FM_LLM_BASE_URL"] = "http://localhost:11434/v1"
        r = complete("hi", purpose="t")
        check("a custom endpoint without FM_LLM_MODEL is refused by name",
              not r.ok and "FM_LLM_MODEL" in r.error)
        os.environ["FM_LLM_MODEL"] = "llama3.2"
        os.environ.pop("FM_OPENAI_API_KEY")
        os.environ.pop("OPENAI_API_KEY")
        requests.post = fake_post(200, {"choices": [{"message": {"content": "local"}}], "usage": {}})
        r = complete("hi", purpose="t")
        check("a local endpoint needs no key", r.ok and r.text == "local"
              and calls[-1]["url"] == "http://localhost:11434/v1/chat/completions"
              and "Authorization" not in calls[-1]["headers"])
        os.environ.pop("FM_LLM_BASE_URL"); os.environ.pop("FM_LLM_MODEL")

        # Gemini
        os.environ["FM_LLM_PROVIDER"] = "gemini"
        os.environ["FM_GEMINI_API_KEY"] = "gk-test"
        requests.post = fake_post(200, {"candidates": [{"content": {"parts": [
            {"text": "thinking...", "thought": True}, {"text": "yo"}]}}],
            "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 5, "thoughtsTokenCount": 3}})
        r = complete("hi", purpose="t", image=img)
        c = calls[-1]
        e = last_ledger()
        check("gemini answers and drops the thought parts", r.ok and r.text == "yo")
        check("gemini key is a header, never in the URL",
              c["headers"].get("x-goog-api-key") == "gk-test" and "gk-test" not in c["url"])
        check("gemini counts thinking tokens as output", e["output_tokens"] == 8)
        requests.post = fake_post(200, {"promptFeedback": {"blockReason": "SAFETY"}})
        r = complete("hi", purpose="t")
        check("a blocked gemini prompt fails by name", not r.ok and "SAFETY" in r.error)

        # Codex
        sample = ('{"type":"thread.started","thread_id":"x"}\n{"type":"turn.started"}\n'
                  '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"ok"}}\n'
                  '{"type":"turn.completed","usage":{"input_tokens":14949,"cached_input_tokens":11776,'
                  '"output_tokens":5,"reasoning_output_tokens":0}}')
        pc = parse_codex(sample)
        check("codex events -> the answer and its tokens",
              pc["text"] == "ok" and pc["usage"]["input_tokens"] == 14949 and not pc["error"])
        check("a codex error event is an error",
              parse_codex('{"type":"turn.failed","error":{"message":"usage limit"}}')["error"] == "usage limit")
        os.environ["OPENAI_API_KEY"] = "sk-x"
        check("codex runs without any OpenAI key in its environment",
              not any(k.upper().startswith(("OPENAI_API_KEY", "FM_OPENAI_API_KEY")) for k in _codex_env()))

        # Off switch
        os.environ["FM_NO_MODEL"] = "1"
        r = complete("hi")
        check("FM_NO_MODEL=1 stops every call", not r.ok and "FM_NO_MODEL" in r.error)
    finally:
        requests.post, shutil.which = real_post, real_which
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        with contextlib.suppress(Exception):
            shutil.rmtree(tmp)
    print(f"\n{passed} passed, {failures} failed")
    return 1 if failures else 0


def _main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()
    if "--costs" in sys.argv:
        return claude_headless.print_costs()
    if "--which" in sys.argv or "--check" in sys.argv:
        ok, why = preflight()
        print(f"provider: {describe()}  |  ready: {'yes' if ok else 'no'} ({why})")
        if "--check" in sys.argv and ok:
            r = complete("Reply with the single word ok.", purpose="check", timeout=120)
            e = r.ledger or {}
            print(f"answer: {r.text!r}" if r.ok else f"failed: {r.error}")
            print(f"tokens in/out: {e.get('input_tokens')}/{e.get('output_tokens')}  "
                  f"cost: {e.get('cost_usd')} ({e.get('cost_basis')})")
            return 0 if r.ok else 1
        return 0 if ok else 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
