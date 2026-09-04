#!/usr/bin/env python
"""probe_lifecycle.py: 实测 FC E2B 后端对 Pause/Resume 与 Snapshot 的支持情况。

背景: e2b SDK 2.24.0 已提供 Sandbox.pause() / connect()(自动 resume) /
create_snapshot() / list_snapshots() / delete_snapshot(), 但**服务端**是否实现
按后端(micro / rund)而异 —— 未实现时通常是 4xx/5xx(501/404/400)。上层接入前必须
先用本脚本对每个后端实测一遍, 不要凭 SDK 有方法就宣称能力可用
(平台能力声明必须基于实测数据)。

探测项(每项独立记录 PASS / FAIL, 失败时打印异常类型与消息):
  1. create        : 建沙箱, 写 marker 文件 + 起一个每秒追加时间戳的 tick 进程
  2. pause         : Sandbox.pause(sandbox_id)  -> 之后 get_info 看 state
  3. resume        : Sandbox.connect(sandbox_id) -> marker 是否还在 / tick 进程
                     是否还活着 / tick 断档多少秒(判断"冻结续跑"还是"重启")
  4. snapshot      : create_snapshot() -> Sandbox.create(template=snapshot_id)
                     新沙箱里 marker 是否存在(判断快照是否真带运行态文件系统)
  5. lifecycle     : create(lifecycle={"on_timeout":"pause","auto_resume":True})
                     服务端是否接受超时自动暂停语义(--with-lifecycle 才跑)

用法(必须先按后端加载对应 .env, 且用 harbor 自己的解释器):
  cd osworld-harbor-demo
  set -a; . ./.env; set +a          # rund = 杭州
  ~/.local/share/uv/tools/harbor/bin/python scripts/probe_lifecycle.py \
      --backend rund --template "$RUND_TEMPLATE"

  set -a; . ./.env.micro; set +a    # micro = 上海
  ~/.local/share/uv/tools/harbor/bin/python scripts/probe_lifecycle.py \
      --backend micro --template "$MICRO_TEMPLATE"
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

MARKER = "/tmp/probe_marker"
TICK = "/tmp/probe_tick"
TICKER = (
    f"nohup setsid bash -c 'while true; do date +%s >> {TICK}; sleep 1; done' "
    ">/dev/null 2>&1 < /dev/null & echo $!"
)


# ------------------------------------------------------------------ report ---

class Report:
    """收集每个探测项的结果, 最后打印一张能直接贴进文档的表。"""

    def __init__(self, backend: str) -> None:
        self.backend = backend
        self.rows: List[Tuple[str, str, str]] = []

    def add(self, item: str, ok: Optional[bool], detail: str = "") -> None:
        status = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        self.rows.append((item, status, detail.replace("\n", " ")[:160]))
        print(f"[{status}] {item}: {detail}", flush=True)

    def dump(self) -> None:
        print(f"\n===== 探测结果: backend={self.backend} =====", flush=True)
        width = max(len(r[0]) for r in self.rows) if self.rows else 10
        for item, status, detail in self.rows:
            print(f"  {item.ljust(width)}  {status.ljust(4)}  {detail}",
                  flush=True)


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


# -------------------------------------------------------------- api params ---

class Backend:
    """屏蔽 micro / rund 在"沙箱怎么来"和"管理请求带什么头"上的差异。

    micro 的 X-E2B-Template-* 头必须挂在**每个**管理 API 上(含 pause/connect/
    snapshot), 因此这里每次调用都重新生成一份 params(内含新的 X-Request-Id)。
    """

    def __init__(self, backend: str) -> None:
        self.backend = backend
        self._cfg: Any = None
        if backend == "micro":
            import micro_e2b

            self._micro = micro_e2b
            micro_e2b.apply_micro_patches()
            self._cfg = micro_e2b.get_config_from_env()
        else:
            self._micro = None

    def params(self) -> Dict[str, Any]:
        if self.backend == "micro":
            return self._micro.get_api_params(self._cfg)
        p: Dict[str, Any] = {"api_key": os.environ.get("E2B_API_KEY")}
        if os.environ.get("E2B_API_URL"):
            p["api_url"] = os.environ["E2B_API_URL"]
        if os.environ.get("E2B_DOMAIN"):
            p["domain"] = os.environ["E2B_DOMAIN"]
        return p

    def create(self, template: str, timeout: int, **extra: Any) -> Any:
        from e2b import Sandbox

        return Sandbox.create(template=template, timeout=timeout,
                              **extra, **self.params())


# ------------------------------------------------------------------ probes ---

def _run(sbx: Any, cmd: str, timeout: int = 60) -> Tuple[int, str]:
    """exec 一条命令; commands.run 在非 0 退出码时会抛异常, 这里统一收敛成返回值。"""
    try:
        r = sbx.commands.run(cmd, user="root", timeout=timeout)
        return r.exit_code, (r.stdout or r.stderr or "").strip()
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "exit_code", None)
        out = getattr(e, "stdout", None) or getattr(e, "stderr", None) or str(e)
        return int(code) if code is not None else -1, str(out).strip()


def plant_state(sbx: Any) -> str:
    """在沙箱里埋一份"运行态": marker 文件 + 每秒写一行的后台 tick 进程。"""
    _run(sbx, f"date +%s > {MARKER}; rm -f {TICK}")
    _, pid = _run(sbx, TICKER)
    time.sleep(3)
    _, marker = _run(sbx, f"cat {MARKER}")
    print(f"  planted marker={marker} ticker_pid={pid}", flush=True)
    return marker


def check_state(sbx: Any, marker: str) -> str:
    """resume/快照恢复后校验运行态: 文件是否还在 + tick 进程是否续跑。"""
    _, got = _run(sbx, f"cat {MARKER} 2>/dev/null || echo MISSING")
    # probe_ti[c]k: 避免 pgrep 匹配到承载本条命令的 shell 自身
    _, alive = _run(sbx, "pgrep -fc 'probe_ti[c]k' 2>/dev/null | head -1")
    _, gap = _run(
        sbx,
        f"if [ -f {TICK} ]; then tail -1 {TICK}; else echo 0; fi",
    )
    same = "same" if got == marker else f"DIFFERENT(got={got})"
    lag = "?"
    try:
        lag = str(int(time.time()) - int(gap))
    except ValueError:
        pass
    return (f"marker={same} ticker_procs={alive} "
            f"last_tick_age={lag}s")


def probe_pause_resume(be: Backend, sbx: Any, rep: Report, timeout: int) -> None:
    from e2b import Sandbox

    marker = plant_state(sbx)
    sid = sbx.sandbox_id

    try:
        Sandbox.pause(sid, **be.params())
    except Exception as e:  # noqa: BLE001
        rep.add("pause", False, _err(e))
        rep.add("resume", None, "pause 未成功, 跳过")
        return
    try:
        info = Sandbox.get_info(sandbox_id=sid, **be.params())
        state = str(getattr(info, "state", "?"))
    except Exception as e:  # noqa: BLE001
        state = f"get_info 失败 {_err(e)}"
    rep.add("pause", True, f"paused sandbox_id={sid} state={state}")

    time.sleep(5)
    try:
        resumed = Sandbox.connect(sid, timeout=timeout, **be.params())
        detail = check_state(resumed, marker)
        rep.add("resume", True, f"sandbox_id={resumed.sandbox_id} {detail}")
    except Exception as e:  # noqa: BLE001
        rep.add("resume", False, _err(e))


def _create_snapshot(be: Backend, sid: str, name: str) -> Tuple[str, str]:
    """建快照, 返回 (snapshot_id, 说明)。

    FC 的 201 响应体没有 names 字段, 官方 SDK 的 SnapshotInfo.from_dict 会
    KeyError: 'names' —— 快照其实已经建了。因此 SDK 路径失败后回落到原生
    HTTP 宽容解析, 避免把"SDK 解析不了"误判成"服务端不支持"。
    """
    from e2b import Sandbox

    try:
        snap = Sandbox.create_snapshot(sid, name=name, **be.params())
        return snap.snapshot_id, f"via=SDK names={snap.names}"
    except Exception as e:  # noqa: BLE001
        p = be.params()
        api_url = p.get("api_url") or os.environ.get("E2B_API_URL")
        headers = dict(p.get("headers") or {})
        headers.update({"X-API-Key": p["api_key"], "Accept": "application/json"})
        r = httpx.post(f"{api_url}/sandboxes/{sid}/snapshots", headers=headers,
                       json={"name": name}, timeout=300)
        if r.status_code >= 300:
            raise RuntimeError(
                f"SDK: {_err(e)} | raw HTTP {r.status_code}: {r.text[:200]}"
            ) from e
        body = r.json()
        return body["snapshotID"], (f"via=rawHTTP({r.status_code}) "
                                    f"SDK解析失败={_err(e)} body={body}")


def probe_snapshot(be: Backend, sbx: Any, rep: Report, timeout: int,
                   keep: bool) -> None:
    from e2b import Sandbox

    marker = plant_state(sbx)
    sid = sbx.sandbox_id
    name = f"probe-snap-{int(time.time())}"
    try:
        snapshot_id, how = _create_snapshot(be, sid, name)
    except Exception as e:  # noqa: BLE001
        rep.add("snapshot.create", False, _err(e))
        rep.add("snapshot.restore", None, "无快照, 跳过")
        return
    rep.add("snapshot.create", True, f"snapshot_id={snapshot_id} {how}")

    # 快照可能有短暂的可见性延迟(同模板传播延迟), 失败重试几次再定性
    restored = None
    last = ""
    for attempt in range(1, 4):
        try:
            restored = be.create(snapshot_id, timeout)
            detail = check_state(restored, marker)
            rep.add("snapshot.restore", True,
                    f"new_sandbox={restored.sandbox_id} attempt={attempt} "
                    f"{detail}")
            break
        except Exception as e:  # noqa: BLE001
            last = _err(e)
            print(f"  restore attempt {attempt}/3 失败: {last}", flush=True)
            time.sleep(15)
    else:
        rep.add("snapshot.restore", False, f"3 次重试均失败: {last}")
    if restored is not None and not keep:
        try:
            restored.kill(**be.params())
        except Exception:  # noqa: BLE001
            pass

    try:
        paginator = Sandbox.list_snapshots(limit=10, **be.params())
        items = paginator.next_items() if paginator.has_next else []
        rep.add("snapshot.list", True, f"count={len(items)}")
    except Exception as e:  # noqa: BLE001
        rep.add("snapshot.list", False, _err(e))

    if not keep:
        try:
            ok = Sandbox.delete_snapshot(snapshot_id, **be.params())
            rep.add("snapshot.delete", bool(ok), f"deleted={ok}")
        except Exception as e:  # noqa: BLE001
            rep.add("snapshot.delete", False, _err(e))


def probe_lifecycle(be: Backend, template: str, rep: Report, timeout: int,
                    keep: bool) -> None:
    """on_timeout=pause + auto_resume: 服务端是否接受超时自动暂停语义。"""
    sbx = None
    try:
        sbx = be.create(
            template, timeout,
            lifecycle={"on_timeout": "pause", "auto_resume": True},
        )
        info = sbx.get_info(**be.params())
        rep.add("lifecycle.pause_on_timeout", True,
                f"sandbox_id={sbx.sandbox_id} lifecycle="
                f"{getattr(info, 'lifecycle', None)}")
    except Exception as e:  # noqa: BLE001
        rep.add("lifecycle.pause_on_timeout", False, _err(e))
    finally:
        if sbx is not None and not keep:
            try:
                sbx.kill(**be.params())
            except Exception:  # noqa: BLE001
                pass


# -------------------------------------------------------------------- main ---

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", choices=("rund", "micro"), required=True)
    ap.add_argument("--template", required=True,
                    help="重新 build 后得到的预建模板名")
    ap.add_argument("--timeout", type=int, default=900,
                    help="沙箱存活秒数(默认 900)")
    ap.add_argument("--keep", action="store_true",
                    help="保留沙箱与快照(排障用)")
    ap.add_argument("--skip-pause", action="store_true")
    ap.add_argument("--skip-snapshot", action="store_true")
    ap.add_argument("--with-lifecycle", action="store_true",
                    help="额外探测 on_timeout=pause / auto_resume")
    args = ap.parse_args()

    if not os.environ.get("E2B_API_KEY"):
        print("缺少 E2B_API_KEY: 先 set -a; . ./.env(.micro); set +a",
              file=sys.stderr)
        return 2

    template = args.template
    rep = Report(args.backend)
    be = Backend(args.backend)

    import importlib.metadata as md
    print(f"e2b SDK={md.version('e2b')} backend={args.backend} "
          f"template={template} api_url={os.environ.get('E2B_API_URL')}",
          flush=True)

    # pause/resume 与 snapshot 各用一个独立沙箱: pause 失败会污染后续状态。
    for name, fn in (
        ("pause/resume", None if args.skip_pause else probe_pause_resume),
        ("snapshot", None if args.skip_snapshot else probe_snapshot),
    ):
        if fn is None:
            rep.add(name, None, "--skip 跳过")
            continue
        sbx = None
        try:
            t0 = time.time()
            sbx = be.create(template, args.timeout)
            rep.add(f"create({name})", True,
                    f"sandbox_id={sbx.sandbox_id} {time.time() - t0:.1f}s")
        except Exception as e:  # noqa: BLE001
            rep.add(f"create({name})", False, _err(e))
            traceback.print_exc()
            continue
        try:
            if fn is probe_pause_resume:
                fn(be, sbx, rep, args.timeout)
            else:
                fn(be, sbx, rep, args.timeout, args.keep)
        finally:
            if not args.keep:
                try:
                    sbx.kill(**be.params())
                except Exception as e:  # noqa: BLE001
                    print(f"  kill 失败(可忽略): {_err(e)}", flush=True)

    if args.with_lifecycle:
        probe_lifecycle(be, template, rep, args.timeout, args.keep)

    rep.dump()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
