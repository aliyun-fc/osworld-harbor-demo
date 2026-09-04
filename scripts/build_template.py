"""build_template.py: one-shot "build -> verify -> READY" for e2b/FC templates.

Follows the demo pattern (osworld-e2b-demo/build.py): a single script that
builds the template and immediately proves it usable, so the daily workflow
is just:  ① python build_template.py <name>   ② harbor run --template=<name>

What it adds over the old e_step1/check_template split:
  - load_dotenv(): no `set -a; . ./.env; set +a` incantation needed
  - on_build_logs: build progress line by line (no 14-min black box)
  - probe by template_id right after build (dodges alias propagation lag)
  - then poll create-by-NAME until usable — harbor passes the name, so we
    only print READY when the name actually resolves (today's 404 lesson)

Usage:
  python build_template.py <new-template-name> [--image <registry/image:tag>]
  # recommended for the long build:  setsid nohup python build_template.py ... &

Exit code: 0 = template usable by name, 1 = failed.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from e2b import Sandbox, Template

DEFAULT_IMAGE = os.environ.get("E2B_TEMPLATE_IMAGE", "")  # set in .env, region-bound
ENV_FILE_FALLBACK = str(Path(__file__).resolve().parent.parent / ".env")  # repo root .env


def _conn_opts() -> dict:
    o = {}
    if os.environ.get("E2B_API_URL"):
        o["api_url"] = os.environ["E2B_API_URL"]
    if os.environ.get("E2B_DOMAIN"):
        o["domain"] = os.environ["E2B_DOMAIN"]
    return o


def _probe(template_ref: str, api_key: str, label: str) -> bool:
    """Ground truth: create + kill a sandbox from the template reference."""
    t = time.time()
    try:
        sbx = Sandbox.create(
            template=template_ref, api_key=api_key, timeout=120, **_conn_opts()
        )
        sid = sbx.sandbox_id
        sbx.kill()
        print(f"[probe] {label}: OK in {time.time() - t:.1f}s ({sid} killed)", flush=True)
        return True
    except Exception as exc:
        print(f"[probe] {label}: FAIL {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="template name (regional resource, pick a new one per region)")
    ap.add_argument("--image", default=os.environ.get("E2B_TEMPLATE_IMAGE") or DEFAULT_IMAGE,
                    help="source image (VPC registry, same region as E2B endpoint)")
    ap.add_argument("--cpu", type=int, default=4)
    ap.add_argument("--mem", type=int, default=8192)
    ap.add_argument("--run-cmd", action="append", default=[],
                    help="extra build step(s) run inside the image "
                         "(e.g. bake verifier deps via pip); repeatable")
    args = ap.parse_args()

    # .env from cwd first (demo convention), fall back to the demo dir.
    load_dotenv()
    if not os.environ.get("E2B_API_KEY"):
        load_dotenv(ENV_FILE_FALLBACK)
    api_key = os.environ.get("E2B_API_KEY", "")
    if not api_key:
        print("ERROR: E2B_API_KEY not set (checked cwd .env and repo .env)")
        return 1
    # --image default is evaluated before load_dotenv; backfill from .env here.
    args.image = args.image or os.environ.get("E2B_TEMPLATE_IMAGE", "")
    if not args.image:
        print("ERROR: no image: set E2B_TEMPLATE_IMAGE in .env or pass --image")
        return 1

    print(f"endpoint: {os.environ.get('E2B_DOMAIN')}", flush=True)
    print(f"image:    {args.image}", flush=True)
    print(f"name:     {args.name}  cpu={args.cpu} mem={args.mem}MB", flush=True)

    t = time.time()
    tpl = Template().from_image(image=args.image)
    for cmd in args.run_cmd:
        print(f"run_cmd:  {cmd}", flush=True)
        tpl = tpl.run_cmd(cmd)
    info = Template.build(
        tpl,
        args.name,
        api_key=api_key,
        cpu_count=args.cpu,
        memory_mb=args.mem,
        on_build_logs=lambda e: print(f"[build] {e}", flush=True),
        **_conn_opts(),
    )
    print(f"[build] done in {time.time() - t:.0f}s "
          f"template_id={info.template_id} build_id={info.build_id}", flush=True)

    # 1) by-id probe: proves the build artifact itself works (no alias involved).
    if not _probe(info.template_id, api_key, f"by-id {info.template_id}"):
        print("BUILD ARTIFACT BROKEN — do not use this template")
        return 1

    # 2) by-name probe with wait: harbor passes the NAME, and alias resolution
    #    can lag minutes behind build completion (propagation window).
    deadline = time.time() + 600
    while True:
        if _probe(args.name, api_key, f"by-name {args.name}"):
            print(f"READY template={args.name} template_id={info.template_id}", flush=True)
            return 0
        if time.time() > deadline:
            print("NAME STILL NOT RESOLVING after 10 min — check alias/propagation")
            return 1
        print("[wait ] name not usable yet, retrying in 15s ...", flush=True)
        time.sleep(15)


if __name__ == "__main__":
    sys.exit(main())
