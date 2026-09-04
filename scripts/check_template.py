"""check_template.py: query whether an e2b/FC template is registered AND usable.

Two independent checks (today's lesson: "build ready" != "usable"):
  1. server-side alias lookup  GET /templates/aliases/{name}  -> registered?
  2. ground-truth probe        Sandbox.create + kill          -> usable NOW?
Check 2 is authoritative: after a fresh build the alias may resolve while
create still 404s for a few minutes (control-plane propagation lag).

Usage:
  set -a; . ./.env; set +a              # E2B_API_KEY / E2B_API_URL / E2B_DOMAIN
  python check_template.py <template-name>            # one-shot status
  python check_template.py <template-name> --wait     # poll until usable (10 min cap)

Exit code: 0 = usable, 1 = not usable.
"""
import os
import sys
import time

from e2b import Sandbox
from e2b.api import ApiClient
from e2b.api.client.api.templates import get_templates_aliases_alias
from e2b.connection_config import ConnectionConfig


def _opts() -> dict:
    o = {}
    if os.environ.get("E2B_API_URL"):
        o["api_url"] = os.environ["E2B_API_URL"]
    if os.environ.get("E2B_DOMAIN"):
        o["domain"] = os.environ["E2B_DOMAIN"]
    return o


def check_alias(name: str) -> str:
    """Server-side registration check. Returns a status string."""
    config = ConnectionConfig(api_key=os.environ["E2B_API_KEY"], **_opts())
    with ApiClient(config) as client:
        res = get_templates_aliases_alias.sync_detailed(alias=name, client=client)
    if res.status_code == 200:
        tid = getattr(res.parsed, "template_id", None)
        return f"REGISTERED (template_id={tid})"
    if res.status_code == 404:
        return "NOT REGISTERED (alias 404)"
    if res.status_code == 403:
        return "REGISTERED (owned by another key, 403)"
    return f"UNKNOWN (HTTP {res.status_code})"


def probe_create(name: str) -> tuple[bool, str]:
    """Ground truth: can we create a sandbox from this template right now?"""
    t = time.time()
    try:
        sbx = Sandbox.create(
            template=name, api_key=os.environ["E2B_API_KEY"], timeout=120, **_opts()
        )
        sid = sbx.sandbox_id
        sbx.kill()
        return True, f"USABLE (create {time.time() - t:.1f}s, probe sandbox {sid} killed)"
    except Exception as exc:
        return False, f"NOT USABLE ({type(exc).__name__}: {str(exc)[:120]})"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    name = sys.argv[1]
    wait = "--wait" in sys.argv[2:]

    print(f"[alias ] {check_alias(name)}", flush=True)
    ok, msg = probe_create(name)
    print(f"[create] {msg}", flush=True)

    if ok or not wait:
        return 0 if ok else 1

    # --wait: poll until usable; covers the post-build propagation window.
    deadline = time.time() + 600
    interval = 15
    while time.time() < deadline:
        print(f"[wait  ] retrying in {interval}s ...", flush=True)
        time.sleep(interval)
        ok, msg = probe_create(name)
        print(f"[create] {msg}", flush=True)
        if ok:
            return 0
    print("[wait  ] gave up after 10 min", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
