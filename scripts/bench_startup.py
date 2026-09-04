"""bench_startup.py: 对比 micro vs rund 沙箱的分段冷启动耗时。

只测与 LLM/agent 无关的基础设施段（可公平对比、可复现）：
  T1 create   : Sandbox.create 返回（沙箱实例就绪）
  T2 desktop  : entrypoint.sh 起 OSWorld，/health 返回 200（桌面+Flask 就绪）
  T3 depcheck : verifier 依赖探测（import 全部命中即 0，未命中则触发 pip 不计入）
用同一个 rund 模板镜像跑两侧不现实（micro 是另一套镜像/region），因此各用各的
上海 micro / 杭州 rund 现成模板，测的是"两条线各自把一个沙箱拉到可跑任务"的墙钟。

用法: python scripts/bench_startup.py micro   |   python scripts/bench_startup.py rund
"""
from __future__ import annotations
import os, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from dotenv import load_dotenv


def _wait_health(sbx, port, label, timeout=300):
    t = time.time()
    # rund 镜像需手动跑 entrypoint.sh; micro 上海镜像 OSWorld 由 systemd 自启,
    # 不能再跑 entrypoint (会抢端口), 直接轮询 /screenshot
    if label != "micro":
        sbx.commands.run(
            "nohup /usr/local/bin/entrypoint.sh >/tmp/ep.log 2>&1 < /dev/null &",
            user="root", envs={"FLASK_PORT": str(port)}, background=True, timeout=10)
    while time.time() - t < timeout:
        r = sbx.commands.run(
            f"curl -s -o /dev/null -w '%{{http_code}}' "
            f"http://127.0.0.1:{port}/health || "
            f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{port}/screenshot || echo 000",
            timeout=15)
        if (r.stdout or "").strip() == "200":
            return time.time() - t
        time.sleep(3)
    return -1


def bench_rund():
    load_dotenv(REPO / ".env", override=True)
    from e2b import Sandbox
    template = os.environ.get("RUND_TEMPLATE")
    if not template:
        raise RuntimeError("RUND_TEMPLATE 未设置；请先重新 build rund 模板")
    opts = {"api_key": os.environ["E2B_API_KEY"],
            "api_url": os.environ.get("E2B_API_URL"),
            "domain": os.environ.get("E2B_DOMAIN")}
    t0 = time.time(); sbx = None
    for w in (0, 5, 15):
        time.sleep(w)
        try:
            sbx = Sandbox.create(template=template, timeout=1800, **opts); break
        except Exception as e:
            print(f"  create retry: {type(e).__name__}", flush=True)
    t_create = time.time() - t0
    print(f"[rund] T1 create   = {t_create:.1f}s  ({sbx.sandbox_id})", flush=True)
    t_desktop = _wait_health(sbx, 8081, "rund")
    print(f"[rund] T2 desktop  = {t_desktop:.1f}s", flush=True)
    sbx.kill()
    return t_create, t_desktop


def bench_micro():
    load_dotenv(REPO / ".env.micro", override=True)
    import micro_e2b
    template = os.environ.get("MICRO_TEMPLATE")
    if not template:
        raise RuntimeError("MICRO_TEMPLATE 未设置；请先重新 build micro 模板")
    micro_e2b.apply_micro_patches()
    cfg = micro_e2b.get_config_from_env()
    t0 = time.time()
    sbx = micro_e2b.create_micro_sandbox(cfg, template, timeout=900)
    t_create = time.time() - t0
    print(f"[micro] T1 create   = {t_create:.1f}s  ({sbx.sandbox_id})", flush=True)
    t_desktop = _wait_health(sbx, 5000, "micro")
    print(f"[micro] T2 desktop  = {t_desktop:.1f}s", flush=True)
    micro_e2b.cleanup_sandbox(cfg, sbx)
    return t_create, t_desktop


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "rund"
    r = bench_micro() if which == "micro" else bench_rund()
    print(f"\n=== {which}: create={r[0]:.1f}s desktop={r[1]:.1f}s total_infra={sum(r):.1f}s ===")
