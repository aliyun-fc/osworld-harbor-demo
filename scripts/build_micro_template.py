"""build_micro_template.py: one-shot "micro build -> verify -> READY".

micro 版的 scripts/build_template.py：用 micro build 模式（X-E2B-Template-
Build-Mode: micro + Alpha headers + 源端 VPC + httpx 补丁）构建模板，然后
立刻用 Sandbox.create 探针验证（先 by-id 再 by-name），全绿才打 READY。
日常工作流仍是两步：
  ① python scripts/build_micro_template.py <name>
  ② MICRO_TEMPLATE=<name> BACKEND=micro bash scripts/run_score.sh

环境配置从 .env.micro 读（E2B_* 三件套 + E2E_MICRO_* micro 参数），
区域一致性硬约束同 rund：key / endpoint / 镜像必须同 region。

Usage:
  python scripts/build_micro_template.py <new-template-name> [--image <registry/image:tag>]
  # 长构建建议:  setsid nohup python scripts/build_micro_template.py ... &

Exit code: 0 = template usable by name, 1 = failed.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import micro_e2b  # noqa: E402


def _load_env() -> None:
    """.env.micro 优先（cwd → repo 根），回退 .env。override=True 盖掉残留。"""
    for cand in (Path.cwd() / ".env.micro", REPO / ".env.micro",
                 Path.cwd() / ".env", REPO / ".env"):
        if cand.exists():
            load_dotenv(cand, override=True)
            print(f"[env] loaded {cand}", flush=True)
            return
    print("[env] no .env.micro/.env found, relying on process env", flush=True)


def _probe(cfg: micro_e2b.MicroConfig, template_ref: str, label: str) -> bool:
    """Ground truth: create + kill a micro sandbox from the template ref.
    get_api_params 会自动带上 micro headers"""
    from e2b import Sandbox

    t = time.time()
    try:
        sbx = Sandbox.create(template=template_ref, timeout=120,
                             **micro_e2b.get_api_params(cfg))
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
    ap.add_argument("--image", default=None,
                    help="source image (VPC registry, same region; default E2E_MICRO_UBUNTU_IMAGE)")
    ap.add_argument("--os-type", default="linux-amd64", help="micro OS type")
    ap.add_argument("--port", type=int, default=49983, help="micro 数据面端口 (build header)")
    ap.add_argument("--entrypoint", default='["sleep", "infinity"]',
                    help='容器 entrypoint JSON array 字符串 (裸 ubuntu 用 sleep infinity)')
    ap.add_argument("--verbose-http", action="store_true",
                    help="打印所有 E2B HTTP 请求/响应 (联调 header 用)")
    args = ap.parse_args()

    _load_env()
    cfg = micro_e2b.get_config_from_env()
    if not cfg.api_key:
        print("ERROR: E2B_API_KEY not set (checked .env.micro/.env)")
        return 1
    image = args.image or cfg.ubuntu_image
    if not image:
        print("ERROR: no image: set E2E_MICRO_UBUNTU_IMAGE in .env.micro or pass --image")
        return 1

    # micro 必需补丁: 禁 keepalive + 数据面 http (探针 create 也依赖)
    micro_e2b.apply_micro_patches()
    if args.verbose_http:
        micro_e2b.patch_http_logging()

    print(f"endpoint: {cfg.domain}", flush=True)
    print(f"image:    {image}", flush=True)
    print(f"name:     {args.name}  cpu={cfg.cpu} mem={cfg.memory}MB "
          f"disk={cfg.disk_size}MB os_type={args.os_type}", flush=True)
    print(f"vpc:      {cfg.vpc_id if cfg.source_vpc else '(disabled)'}", flush=True)

    t = time.time()
    info = micro_e2b.build_micro_template(
        cfg,
        args.name,
        image,
        os_type=args.os_type,
        port=args.port,
        # entrypoint 经构建头传入（对齐 headers= 口径）
        headers={
            "X-E2B-Template-Alpha-Micro-Entrypoint": args.entrypoint,
        } if args.entrypoint else None,
        on_build_logs=lambda e: print(f"[build] {e}", flush=True),
    )
    print(f"[build] done in {time.time() - t:.0f}s "
          f"template_id={info.template_id} build_id={info.build_id}", flush=True)

    # 1) by-id probe: proves the build artifact itself works (no alias involved).
    if not _probe(cfg, info.template_id, f"by-id {info.template_id}"):
        print("BUILD ARTIFACT BROKEN — do not use this template")
        return 1

    # 2) by-name probe with wait: harbor passes the NAME, and alias resolution
    #    can lag minutes behind build completion (propagation window).
    deadline = time.time() + 600
    while True:
        if _probe(cfg, args.name, f"by-name {args.name}"):
            print(f"READY template={args.name} template_id={info.template_id}", flush=True)
            return 0
        if time.time() > deadline:
            print("NAME STILL NOT RESOLVING after 10 min — check alias/propagation")
            return 1
        print("[wait ] name not usable yet, retrying in 15s ...", flush=True)
        time.sleep(15)


if __name__ == "__main__":
    sys.exit(main())
