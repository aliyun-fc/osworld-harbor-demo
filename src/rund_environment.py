"""RundEnvironment: Harbor BaseEnvironment adapter over e2b/FC (rund runtime).

Maps Harbor's minimal backend contract (start/stop + exec + upload/download)
onto the e2b Python SDK, exactly mirroring the proven posture in
osworld-e2b-demo/osworld_e2b.py (Sandbox.create + commands.run + files.*).

Register with Harbor WITHOUT touching harbor source:
  harbor run --env /abs/path/rund_environment.py:RundEnvironment \
             --environment-kwarg template=<prebuilt_template_name>   # skip build
  (or --environment-kwarg image=<registry/image:tag> to build a template first)

Key knobs (via --environment-kwarg or env):
  template   : reuse an existing e2b template (skips the v3 build API entirely)
  image      : image to build a template from (fallback when no template)
  pause_on_idle : 空闲超时后暂停而非销毁（默认 false）；要求
               sandbox_timeout_sec >= 300。恢复由环境适配器处理。
  E2B_API_KEY / E2B_API_URL / E2B_DOMAIN : from env (loaded from .env)

pause_on_idle 主要用于需要跨阶段保留状态的外部编排；显式接口见
pause()/resume()。
"""
from __future__ import annotations

import asyncio
import io
import os
import shlex
import tarfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.trial.paths import EnvironmentPaths


class _StaleResumeInstanceError(RuntimeError):
    """A guarded request was routed to an instance without the current snapshot."""


class RundEnvironment(BaseEnvironment):
    # log prefix; subclasses (e.g. MicroEnvironment) override to keep the
    # grep-able "<prefix> sandbox created sandbox_id=" shape per backend
    _log_prefix = "rund"

    # env defaults merged into every exec (caller's env wins). osworld-verified
    # 的 tests/test.sh 默认连 VM_NET_IP=172.30.0.2 (verifier 容器+VM 分离拓扑),
    # 我们是单沙箱拓扑: OSWorld 恒在 localhost, rund 镜像 Flask 在 8081。
    _exec_env_defaults: dict[str, str] = {
        "VM_NET_IP": "127.0.0.1",
        "OSWORLD_SERVER_PORT": "8081",
    }

    def __init__(
        self,
        *args: Any,
        template: str | None = None,
        image: str | None = None,
        api_key: str | None = None,
        api_url: str | None = None,
        domain: str | None = None,
        cpu_count: int = 2,
        memory_mb: int = 2048,
        sandbox_timeout_sec: int = 900,
        default_user: str = "root",
        pause_on_idle: bool | str = False,
        **kwargs: Any,
    ) -> None:
        # Set our attrs BEFORE super().__init__(): BaseEnvironment.__init__
        # calls self._validate_definition() at its tail, which reads them.
        _tec = kwargs.get("task_env_config")
        self._template = template or os.environ.get("RUND_TEMPLATE")
        self._image = (
            image
            or os.environ.get("RUND_IMAGE")
            or getattr(_tec, "docker_image", None)
        )
        self._api_key = api_key or os.environ.get("E2B_API_KEY")
        self._api_url = api_url or os.environ.get("E2B_API_URL")
        self._domain = domain or os.environ.get("E2B_DOMAIN")
        self._cpu = int(cpu_count)
        self._mem = int(memory_mb)
        self._sbx_timeout = int(sandbox_timeout_sec)
        self._default_user = default_user
        # Harbor CLI passes --environment-kwarg values as strings.
        self._pause_on_idle = str(pause_on_idle).lower() in ("1", "true", "yes")
        self._last_ok_ts = time.time()
        self._pause_probe_forced = False
        self._resume_sentinel_token: str | None = None
        self._sbx: Any = None
        self._hb_task: Any = None
        super().__init__(*args, **kwargs)

    # --- identity / validation ---
    @staticmethod
    def type() -> str:
        return "rund"

    def _validate_definition(self) -> None:
        if not self._api_key:
            raise ValueError("RundEnvironment requires E2B_API_KEY")
        if not (self._template or self._image):
            raise ValueError("RundEnvironment requires 'template' or 'image'")
        # The lifecycle API requires at least a 300-second idle timeout.
        if self._pause_on_idle and self._sbx_timeout < 300:
            raise ValueError(
                "pause_on_idle=true requires sandbox_timeout_sec >= 300 "
                f"(got {self._sbx_timeout})"
            )

    def _opts(self) -> dict[str, str]:
        o: dict[str, str] = {}
        if self._api_url:
            o["api_url"] = self._api_url
        if self._domain:
            o["domain"] = self._domain
        return o

    # --- lifecycle ---
    def _create_sandbox_blocking(self) -> Any:
        """Build (if needed) + create the e2b sandbox. Runs in a worker thread.
        Subclasses override this hook to swap the sandbox provisioning path
        (e.g. MicroEnvironment: transport patches + micro build mode).

        The transport patch avoids gateway HTTP/2/keepalive failures. The
        micro-only HTTP URL rewrite must not be applied to this backend.
        """
        import micro_e2b
        from e2b import Sandbox, Template

        micro_e2b.patch_disable_keepalive()

        template = self._template
        if not template:
            name = f"rund-{self.environment_name}-{int(time.time())}"
            info = Template.build(
                Template().from_image(image=self._image),
                name,
                api_key=self._api_key,
                cpu_count=self._cpu,
                memory_mb=self._mem,
                **self._opts(),
            )
            template = info.name
        extra: dict[str, Any] = {}
        if self._pause_on_idle:
            extra["lifecycle"] = {"on_timeout": "pause", "auto_resume": True}
        sbx = Sandbox.create(
            template=template,
            api_key=self._api_key,
            timeout=self._sbx_timeout,
            **extra,
            **self._opts(),
        )
        # Seed the generation marker before the first automatic pause.
        if self._pause_on_idle:
            self._sbx = sbx
            self._write_sentinel_blocking()
        return sbx

    # --- pause / resume ---
    # The management API can report a resumed sandbox before data-plane routing
    # has converged. A per-pause generation marker detects stale instances;
    # exec validates it atomically with the business command, while file calls
    # use bounded retries after a confirmed resume.
    _PAUSE_PROBE_AFTER_SEC = 240.0
    _RESUME_WAIT_SEC = 60.0
    _SENTINEL_PATH = "/tmp/.rund_resume_sentinel"
    # Consecutive probes are a warm-up barrier, not the exec correctness guard.
    _DATAPLANE_READY_CONSECUTIVE_HITS = 3
    _DATAPLANE_READY_PROBE_INTERVAL_SEC = 2.0
    _POST_RESUME_BACKOFF_SEC = (0.0, 2.0, 5.0, 10.0)
    _STALE_INSTANCE_EXIT_CODE = 222
    _STALE_INSTANCE_MARKER = "__RUND_STALE_RESUME_INSTANCE__"

    def _write_sentinel_blocking(self) -> bool:
        """Write a new generation marker and remember it on success."""
        if self._sbx is None:
            return False
        token = f"{self._sbx.sandbox_id}:{uuid.uuid4().hex}"
        try:
            self._sbx.commands.run(
                f"printf %s {shlex.quote(token)} > "
                f"{shlex.quote(self._SENTINEL_PATH)}",
                user="root", timeout=30,
            )
            self._resume_sentinel_token = token
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("%s write resume sentinel failed: %s",
                              self._log_prefix, exc)
            return False

    def _wait_dataplane_ready_blocking(self) -> bool:
        """Return once consecutive probes observe the current generation."""
        if self._sbx is None:
            return False
        token = getattr(self, "_resume_sentinel_token", None)
        if not token:
            return False
        deadline = time.time() + self._RESUME_WAIT_SEC
        consecutive_hits = 0
        while time.time() < deadline:
            try:
                r = self._sbx.commands.run(
                    f"cat {shlex.quote(self._SENTINEL_PATH)}",
                    user="root", timeout=20,
                )
                if int(getattr(r, "exit_code", 1) or 0) == 0 and \
                        token in (getattr(r, "stdout", "") or ""):
                    consecutive_hits += 1
                    if consecutive_hits >= self._DATAPLANE_READY_CONSECUTIVE_HITS:
                        return True
                else:
                    consecutive_hits = 0
            except Exception:  # noqa: BLE001
                consecutive_hits = 0
            time.sleep(self._DATAPLANE_READY_PROBE_INTERVAL_SEC)
        self.logger.warning(
            "%s data plane not confirmed ready within %.0fs after resume",
            self._log_prefix, self._RESUME_WAIT_SEC,
        )
        return False

    def _ensure_running_blocking(self) -> bool:
        """Resume a paused sandbox and refresh its SDK handle."""
        if self._sbx is None:
            return False
        try:
            state = str(getattr(self._sbx.get_info(), "state", "")).lower()
        except Exception as exc:
            self.logger.debug(
                "%s get_info failed while probing pause state: %s",
                self._log_prefix, exc,
            )
            return False
        if "paused" not in state:
            return False
        self.logger.info(
            "%s sandbox is paused, resuming sandbox_id=%s",
            self._log_prefix, self._sbx.sandbox_id,
        )
        # Class-level connect rebuilds the handle with refreshed credentials.
        sid = self._sbx.sandbox_id
        self._sbx = type(self._sbx).connect(
            sid, timeout=self._sbx_timeout, **self._opts())
        # Management state alone is not a data-plane readiness signal.
        if not self._wait_dataplane_ready_blocking():
            deadline = time.time() + self._RESUME_WAIT_SEC
            while time.time() < deadline:
                try:
                    if "paused" not in str(
                            getattr(self._sbx.get_info(), "state", "")).lower():
                        break
                except Exception:  # noqa: BLE001
                    break
                time.sleep(1)
        return True

    def _call_with_resume(self, fn: Any, *a: Any, **kw: Any) -> Any:
        """Run a data-plane call with pause detection and bounded recovery."""
        resumed = False
        if (self._pause_on_idle
                and time.time() - getattr(self, "_last_ok_ts", 0.0)
                > self._PAUSE_PROBE_AFTER_SEC) or getattr(
                    self, "_pause_probe_forced", False):
            self._pause_probe_forced = False
            resumed = self._ensure_running_blocking()
        try:
            out = (self._retry_after_resume(fn, *a, **kw)
                   if resumed else fn(*a, **kw))
        except _StaleResumeInstanceError:
            # The atomic guard failed before executing the business command.
            if resumed:
                raise
            out = self._retry_after_resume(fn, *a, **kw)
        except Exception:
            if resumed:
                raise
            if not self._ensure_running_blocking():
                raise
            out = self._retry_after_resume(fn, *a, **kw)
        self._last_ok_ts = time.time()
        return out

    def _retry_after_resume(self, fn: Any, *a: Any, **kw: Any) -> Any:
        """Retry a post-resume data-plane call with bounded backoff."""
        last = len(self._POST_RESUME_BACKOFF_SEC) - 1
        for i, delay in enumerate(self._POST_RESUME_BACKOFF_SEC):
            if delay:
                time.sleep(delay)
            try:
                return fn(*a, **kw)
            except Exception as exc:  # noqa: BLE001
                if i == last:
                    raise
                self.logger.debug(
                    "%s post-resume retry %d/%d failed (%s), backing off",
                    self._log_prefix, i + 1, last + 1, exc,
                )
        raise AssertionError("unreachable")  # for type checkers

    async def pause(self) -> None:
        """Pause the sandbox after recording a new snapshot generation."""
        if self._sbx is None:
            raise RuntimeError("Sandbox not started")
        # The new generation must be part of the snapshot being paused.
        sentinel_written = await asyncio.to_thread(self._write_sentinel_blocking)
        if not sentinel_written:
            raise RuntimeError("failed to write resume sentinel before pause")
        await asyncio.to_thread(self._sbx.pause)
        self._pause_probe_forced = True
        self.logger.info(
            "%s sandbox paused sandbox_id=%s", self._log_prefix,
            self._sbx.sandbox_id,
        )

    async def resume(self) -> None:
        """Resume the sandbox and wait for its data plane to become observable."""
        if self._sbx is None:
            raise RuntimeError("Sandbox not started")
        sid = self._sbx.sandbox_id
        self._sbx = await asyncio.to_thread(
            lambda: type(self._sbx).connect(
                sid, timeout=self._sbx_timeout, **self._opts()))
        ready = await asyncio.to_thread(self._wait_dataplane_ready_blocking)
        self._pause_probe_forced = False
        if not ready:
            raise RuntimeError(
                f"sandbox {sid} resumed but data plane was not ready within "
                f"{self._RESUME_WAIT_SEC:.0f}s"
            )
        self.logger.info(
            "%s sandbox resumed sandbox_id=%s", self._log_prefix,
            self._sbx.sandbox_id,
        )

    async def start(self, force_build: bool) -> None:
        self._sbx = await asyncio.to_thread(self._create_sandbox_blocking)

        # Track the FC sandbox: log its identity in the same grep-able shape as
        # OpenSandboxEnvironment ("sandbox created sandbox_id=..."). On success
        # the e2b SDK does not surface FC request ids; those only appear in
        # exception text (request_id=...), which lands in trial.log anyway.
        try:
            info = await asyncio.to_thread(self._sbx.get_info)
            self.logger.info(
                "%s sandbox created sandbox_id=%s template=%s(%s) domain=%s started_at=%s",
                self._log_prefix,
                self._sbx.sandbox_id,
                getattr(info, "name", None),
                getattr(info, "template_id", None),
                getattr(info, "sandbox_domain", None),
                getattr(info, "started_at", None),
            )
        except Exception as exc:
            self.logger.info(
                "%s sandbox created sandbox_id=%s (get_info failed: %s)",
                self._log_prefix,
                self._sbx.sandbox_id,
                exc,
            )

        # Mirror OpenSandboxEnvironment.start(): ensure /logs/{agent,verifier,
        # artifacts} exist and are world-writable (created as root).
        log_dirs = " ".join(
            shlex.quote(str(d))
            for d in (
                EnvironmentPaths.agent_dir,
                EnvironmentPaths.verifier_dir,
                EnvironmentPaths.artifacts_dir,
            )
        )
        r = await self.exec(f"mkdir -p {log_dirs} && chmod 777 {log_dirs}", user="root")
        if r.return_code != 0:
            self.logger.warning(
                "%s: failed to prep /logs dirs: %s", self._log_prefix, r.stderr
            )

        # Upload the task's environment/ dir into the workdir using the
        # canonical base helper (same path resolution as every other backend):
        # self.environment_dir IS the environment dir; workdir = task.workdir
        # or `pwd`. (Our earlier custom upload used the wrong source path.)
        await self._upload_environment_dir_after_start()

        # OSWorld-style config steps have no harbor equivalent, so tasks may
        # ship an environment/setup.sh; run it once (as root) right after the
        # upload, before the agent starts, then remove it from the workdir.
        await self._run_task_setup_script()

        # Keep the e2b management-API connection warm (FC gateway idle
        # timeout ~60s). Long agent steps only touch the sandbox envd
        # connection, leaving the mgmt connection idle -> gateway closes
        # it -> Broken pipe on the final kill(). A periodic get_info()
        # (mgmt API) faux-traffic keeps it alive.
        self._hb_task = asyncio.create_task(self._heartbeat())

    async def _run_task_setup_script(self) -> None:
        workdir = getattr(self.task_env_config, "workdir", None)
        if not workdir:
            r = await self.exec("pwd")
            workdir = (r.stdout or "/").strip()
        script = f"{workdir.rstrip('/')}/setup.sh"
        probe = await self.exec(f"test -f {shlex.quote(script)} && echo yes || echo no")
        if probe.stdout.strip() != "yes":
            return
        r = await self.exec(
            f"cd {shlex.quote(workdir)} && bash setup.sh && rm -f setup.sh",
            user="root",
            timeout_sec=300,
        )
        if r.return_code != 0:
            raise RuntimeError(f"task setup.sh failed: {r.stderr or r.stdout}")
        self.logger.info(
            "%s: task setup.sh executed in %s", self._log_prefix, workdir
        )

    async def _heartbeat(self, interval_sec: float = 25.0) -> None:
        while True:
            try:
                await asyncio.sleep(interval_sec)
                if self._sbx is None:
                    return
                await asyncio.to_thread(self._sbx.get_info)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.logger.debug(
                    "%s heartbeat ignored error: %s", self._log_prefix, exc
                )

    async def stop(self, delete: bool) -> None:
        if self._hb_task is not None:
            self._hb_task.cancel()
            self._hb_task = None
        if not self._sbx:
            return
        sbx, self._sbx = self._sbx, None
        sid = getattr(sbx, "sandbox_id", "?")
        # belt-and-suspenders: even with the heartbeat, retry once so a stale
        # pooled connection is discarded and re-dialed instead of raising.
        last_exc = None
        for attempt in range(2):
            try:
                await asyncio.to_thread(sbx.kill)
                self.logger.info(
                    "%s sandbox killed sandbox_id=%s", self._log_prefix, sid
                )
                return
            except Exception as exc:
                last_exc = exc
                self.logger.warning(
                    "%s kill attempt %d failed sandbox_id=%s: %s",
                    self._log_prefix, attempt + 1, sid, exc,
                )
                await asyncio.sleep(1)
        self.logger.warning(
            "%s kill gave up sandbox_id=%s (sandbox will auto-expire): %s",
            self._log_prefix, sid, last_exc,
        )

    # --- exec ---
    def _guard_exec_for_current_snapshot(self, command: str) -> str:
        """Validate the snapshot generation in the business command request.

        A stale instance exits with a private marker before the business command
        runs, so retrying that marker cannot replay a completed command.
        """
        token = getattr(self, "_resume_sentinel_token", None)
        if not token:
            return command
        path = shlex.quote(self._SENTINEL_PATH)
        expected = shlex.quote(token)
        marker = shlex.quote(self._STALE_INSTANCE_MARKER)
        return (
            f"__rund_resume_actual=$(cat {path} 2>/dev/null || true)\n"
            f"if [ \"$__rund_resume_actual\" != {expected} ]; then\n"
            f"  printf '%s\\n' {marker} >&2\n"
            f"  exit {self._STALE_INSTANCE_EXIT_CODE}\n"
            "fi\n"
            f"{command}"
        )

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        if self._sbx is None:
            raise RuntimeError("Sandbox not started")
        full = command if not cwd else f"cd {shlex.quote(cwd)} && {command}"
        guarded_full = self._guard_exec_for_current_snapshot(full)
        run_user = str(user) if user is not None else self._default_user
        to = int(timeout_sec) if timeout_sec else None
        run_env = {**self._exec_env_defaults, **(env or {})}

        def _do() -> ExecResult:
            try:
                r = self._sbx.commands.run(
                    guarded_full, user=run_user, envs=run_env, timeout=to
                )
                result = ExecResult(
                    stdout=getattr(r, "stdout", "") or "",
                    stderr=getattr(r, "stderr", "") or "",
                    return_code=int(getattr(r, "exit_code", 0) or 0),
                )
                if (result.return_code == self._STALE_INSTANCE_EXIT_CODE
                        and self._STALE_INSTANCE_MARKER in result.stderr):
                    raise _StaleResumeInstanceError(result.stderr)
                return result
            except _StaleResumeInstanceError:
                raise
            except Exception as exc:  # e2b CommandExitException on non-zero exit
                code = getattr(exc, "exit_code", None)
                if code is None:
                    raise
                result = ExecResult(
                    stdout=getattr(exc, "stdout", "") or "",
                    stderr=getattr(exc, "stderr", "") or str(exc),
                    return_code=int(code),
                )
                if (result.return_code == self._STALE_INSTANCE_EXIT_CODE
                        and self._STALE_INSTANCE_MARKER in result.stderr):
                    raise _StaleResumeInstanceError(result.stderr)
                return result

        return await asyncio.to_thread(self._call_with_resume, _do)

    # --- file transfer (tar-based for dirs = robust across SDK quirks) ---
    # FC 网关对单次 files.write 的 payload 有上限(实测 EntityTooLarge),
    # 大文件/大目录 tar 必须分块写再在沙箱内 cat 拼接。
    _WRITE_CHUNK_BYTES = 8 * 1024 * 1024

    def _write_blob_blocking(self, target_path: str, data: bytes) -> None:
        """同步写入沙箱文件; 超阈值则分片写 .partN 后拼回(在工作线程里调)。"""
        if len(data) <= self._WRITE_CHUNK_BYTES:
            self._sbx.files.write(target_path, data)
            return
        n = 0
        for off in range(0, len(data), self._WRITE_CHUNK_BYTES):
            self._sbx.files.write(
                f"{target_path}.part{n}", data[off:off + self._WRITE_CHUNK_BYTES])
            n += 1
        parts = " ".join(shlex.quote(f"{target_path}.part{i}") for i in range(n))
        self._sbx.commands.run(
            f"cat {parts} > {shlex.quote(target_path)} && rm -f {parts}",
            user="root", timeout=600,
        )
        self.logger.info(
            "%s: wrote %.0fMB to %s in %d chunks",
            self._log_prefix, len(data) / 1048576, target_path, n,
        )

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        data = Path(source_path).read_bytes()
        await asyncio.to_thread(
            self._call_with_resume, self._write_blob_blocking, target_path, data)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        src = Path(source_dir)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for p in src.rglob("*"):
                tf.add(p, arcname=p.relative_to(src).as_posix())
        blob = buf.getvalue()
        remote_tar = f"/tmp/rund_up_{int(time.time()*1000)}.tgz"

        await asyncio.to_thread(
            self._call_with_resume, self._write_blob_blocking, remote_tar, blob)
        r = await self.exec(
            f"mkdir -p {shlex.quote(target_dir)} && "
            f"tar xzf {shlex.quote(remote_tar)} -C {shlex.quote(target_dir)} && "
            f"rm -f {shlex.quote(remote_tar)}",
            user="root",
        )
        if r.return_code != 0:
            raise RuntimeError(f"upload_dir untar failed: {r.stderr or r.stdout}")

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        def _do() -> bytes:
            return self._sbx.files.read(source_path, format="bytes")

        data = await asyncio.to_thread(self._call_with_resume, _do)
        tp = Path(target_path)
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_bytes(data)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        remote_tar = f"/tmp/rund_dl_{int(time.time()*1000)}.tgz"
        r = await self.exec(
            f"tar czf {shlex.quote(remote_tar)} -C {shlex.quote(source_dir)} .",
            user="root",
        )
        if r.return_code != 0:
            raise RuntimeError(f"download_dir tar failed: {r.stderr or r.stdout}")

        def _do() -> bytes:
            return self._sbx.files.read(remote_tar, format="bytes")

        blob = await asyncio.to_thread(self._call_with_resume, _do)
        td = Path(target_dir)
        td.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            tf.extractall(td)
        await self.exec(f"rm -f {shlex.quote(remote_tar)}", user="root")
