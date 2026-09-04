#!/usr/bin/env python3
"""test_pause_integration.py: 验证 RundEnvironment 的深休眠接入。

不走 harbor 的完整装配(BaseEnvironment.__init__ 需要 trial_paths / task_env_config
等一堆模型), 用 __new__ + 手工填字段直接测真实方法, 覆盖四条路径:
  1. pause_on_idle=True 的 create 路径(服务端是否接受 lifecycle)
  2. 显式 pause() / resume()
  3. 透明唤醒: 绕过 env 直接把沙箱 pause 掉, 再调 env.exec()/download_file(),
     _call_with_resume 应当自动唤醒并成功(这是接入的核心价值)
  4. 参数校验: pause_on_idle=True 且 sandbox_timeout_sec < 300 必须早报错

用法:
  set -a; . ./.env; set +a
  ~/.local/share/uv/tools/harbor/bin/python scripts/test_pause_integration.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from rund_environment import RundEnvironment  # noqa: E402

TEMPLATE = os.environ.get("RUND_TEMPLATE")
logging.basicConfig(level=logging.INFO, format="    [%(levelname)s] %(message)s")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          f"{'  ' + detail if detail else ''}", flush=True)


def _detail(r) -> str:
    """把 ExecResult 的三个字段都列出来。

    只打 stdout 时失败详情是空字串, 分不出“文件不存在(冷实例)”还是
    “命令因其他原因失败”, 而这两者的排查方向完全不同。
    """
    return (f"rc={r.return_code} stdout={r.stdout.strip()!r} "
            f"stderr={r.stderr.strip()[:120]!r}")


def make_env(pause_on_idle: bool | str, timeout: int = 600) -> RundEnvironment:
    """绕过 harbor 装配造一个可用的 env 实例(只填被测方法用到的字段)。"""
    env = RundEnvironment.__new__(RundEnvironment)
    env._template = TEMPLATE
    env._image = None
    env._api_key = os.environ["E2B_API_KEY"]
    env._api_url = os.environ.get("E2B_API_URL")
    env._domain = os.environ.get("E2B_DOMAIN")
    env._cpu, env._mem = 4, 8192
    env._sbx_timeout = timeout
    env._default_user = "root"
    env._pause_on_idle = str(pause_on_idle).lower() in ("1", "true", "yes")
    env._last_ok_ts = 0.0
    env._sbx = None
    env._hb_task = None
    env.logger = logging.getLogger("rund-test")
    env.environment_name = "pause-test"
    return env


async def main() -> int:
    if not TEMPLATE:
        raise RuntimeError("RUND_TEMPLATE 未设置；请先重新 build rund 模板")
    from e2b import Sandbox

    # --- 4) 参数校验先测, 不花钱
    print("\n=== 4) 参数校验: pause_on_idle + timeout<300 应当早报错")
    bad = make_env(True, timeout=120)
    try:
        bad._validate_definition()
        check("timeout<300 被拒", False, "居然通过了")
    except ValueError as e:
        check("timeout<300 被拒", "must be >= 300" in str(e) or ">= 300" in str(e),
              str(e)[:80])

    env = make_env(True, timeout=600)
    env._validate_definition()

    print("\n=== 1) create with lifecycle(on_timeout=pause, auto_resume)")
    env._sbx = await asyncio.to_thread(env._create_sandbox_blocking)
    sid = env._sbx.sandbox_id
    check("create 成功", True, f"sandbox_id={sid}")
    info = await asyncio.to_thread(env._sbx.get_info)
    check("lifecycle 生效", bool(getattr(info, "lifecycle", None)),
          str(getattr(info, "lifecycle", None)))

    r = await env.exec("echo golden > /tmp/state.txt; cat /tmp/state.txt")
    check("exec 正常", r.return_code == 0 and "golden" in r.stdout,
          r.stdout.strip())

    print("\n=== 2) 显式 pause() / resume()")
    await env.pause()
    state = str(getattr(await asyncio.to_thread(env._sbx.get_info), "state", ""))
    check("pause 后 state=paused", "paused" in state.lower(), state)
    await env.resume()
    r = await env.exec("cat /tmp/state.txt")
    check("resume 后状态保留", "golden" in r.stdout, _detail(r))

    print("\n=== 3) 透明唤醒(绕过 env 直接 pause, 再走 env 的 API)")
    opts = {"api_key": env._api_key, **env._opts()}
    Sandbox.pause(sid, **opts)
    state = str(getattr(Sandbox.get_info(sandbox_id=sid, **opts), "state", ""))
    check("已在 env 背后暂停", "paused" in state.lower(), state)

    # 本例模拟的是平台 idle-pause(不经 env.pause(), 所以不会置
    # _pause_probe_forced)。而平台要空闲 >=300s 才暂停, 因此真实场景下
    # _last_ok_ts 必然已过期、事前探测必然触发; 而本脚本是刚调用完 2 秒就
    # 暂停, 造出了平台根本产生不了的状态。这里把“已长时间空闲”补上,
    # 是让用例向现实对齐(而非放水): 否则 exec 会被冷实例静默服务
    # (exit_code=1, 无异常), 测的是一个不可达的场景。
    # 残留局限(已知并接受): 若外部在活动后 240s 内把沙箱暂停, exec 仍可能
    # 静默拿到空环境 —— 要堆除得给每个 exec 前都加一次管理面探测, 热路径
    # 多一个 round trip, 不值。
    env._last_ok_ts = 0.0

    r = await env.exec("cat /tmp/state.txt")
    check("exec 自动唤醒并成功", r.return_code == 0 and "golden" in r.stdout,
          _detail(r))

    Sandbox.pause(sid, **opts)
    # 同上: 还原“已长时间空闲”这个真实前置条件。
    env._last_ok_ts = 0.0
    dst = Path("/tmp/pause_test_download.txt")
    try:
        await env.download_file("/tmp/state.txt", dst)
        check("download_file 自动唤醒并成功", dst.read_text().strip() == "golden",
              dst.read_text().strip())
    except Exception as e:  # noqa: BLE001
        check("download_file 自动唤醒并成功", False, f"{type(e).__name__}: {e}"[:100])

    print("\n=== 清理")
    await env.stop(delete=True)

    print(f"\n===== 结果: PASS {len(PASS)} / FAIL {len(FAIL)}")
    for f in FAIL:
        print(f"  未通过: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
