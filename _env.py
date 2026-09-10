"""Load this project's own .env (secrets) — the one place they live.

Secrets don't belong in the user environment: anything spawned there inherits them (a
headless `claude -p` would bill an API key it finds, for instance). Import this module
first in every entry point that reads a secret; an already-set variable wins
(override=False), so a wrapper that loaded the file earlier is respected and a test can
inject its own value.
"""
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # fail loudly rather than run without the secrets
    raise SystemExit("python-dotenv missing: python -m pip install python-dotenv")

ENV_FILE = Path(__file__).with_name(".env")
load_dotenv(ENV_FILE, override=False)
