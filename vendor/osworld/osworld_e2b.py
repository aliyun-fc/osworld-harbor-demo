"""
osworld_e2b.py - 在 e2b 沙箱里拉起 OSWorld,并通过沙箱内 curl 调 OSWorld Flask API。

这是给 e2b-common-demos/osworld/ 这一组 demo 复用的小工具,核心提供两个东西:

1. `OSWorldSandbox`: 用 osworld-native-dev 镜像构建模板、创建沙箱,并
   以 root 身份在 FLASK_PORT(默认 8081,避开 envd 的 5000)上拉起
   /usr/local/bin/entrypoint.sh,直到 /health 返回 200。

2. `OSWorldClient`: 通过 `sandbox.commands.run("curl ...")` 在沙箱内访问
   OSWorld 的 REST 接口(/setup/execute、/execute、/setup/launch、/setup/upload、
   /screenshot、/terminal、/screen_size 等),不依赖 e2b 公网代理 —— cn-beijing-pre
   的网关对 traffic_access_token 当前会返回 403,从沙箱内访问不受影响。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from e2b import Sandbox, Template

# --- HTTP 拦截: 记录最近一次 E2B API 调用的 request-id 等 header ---
_REQ_ID_HEADERS = (
    "x-request-id",
    "x-fc-request-id",
    "eagleeye-traceid",
    "traceparent",
    "x-e2b-request-id",
    "x-log-requestid",
)

_last_resp_meta: dict[str, Any] = {
    "url": None, "status": None,
    "request_ids": {}, "body_snippet": None,
}

_orig_httpx_send = httpx.Client.send


def _patched_httpx_send(self, request, **kwargs):
    response = _orig_httpx_send(self, request, **kwargs)
    try:
        url = str(response.request.url)
        if "e2b" in url or "fc-e2b" in url or "aliyuncs.com" in url:
            _last_resp_meta["url"] = url
            _last_resp_meta["status"] = response.status_code
            _last_resp_meta["request_ids"] = {
                h: response.headers.get(h)
                for h in _REQ_ID_HEADERS
                if response.headers.get(h)
            }
            body = response.content
            if body:
                _last_resp_meta["body_snippet"] = body[:500].decode(
                    "utf-8", errors="replace")
    except Exception:
        pass
    return response


httpx.Client.send = _patched_httpx_send


def dump_last_resp_meta() -> str:
    if not _last_resp_meta.get("url"):
        return "(未捕获到 E2B HTTP 响应)"
    parts = [
        f"  url         : {_last_resp_meta['url']}",
        f"  status      : {_last_resp_meta['status']}",
        f"  request_ids : {_last_resp_meta['request_ids'] or '(none)'}",
    ]
    if _last_resp_meta.get("body_snippet"):
        parts.append(
            f"  body_snippet: {_last_resp_meta['body_snippet'][:300]}")
    return "\n".join(parts)


DEFAULT_IMAGE = os.environ.get("E2B_TEMPLATE_IMAGE", "")
DEFAULT_FLASK_PORT = 8081  # envd 占用了 5000


def _conn_opts() -> dict[str, str]:
    opts: dict[str, str] = {}
    api_url = os.environ.get("E2B_API_URL")
    domain = os.environ.get("E2B_DOMAIN")
    if api_url:
        opts["api_url"] = api_url
    if domain:
        opts["domain"] = domain
    return opts


@dataclass
class CommandResult:
    """sandbox.commands.run 结果的简化封装。"""
    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class OSWorldClient:
    """通过沙箱内 curl 访问 OSWorld Flask API。"""

    def __init__(self, sandbox: Sandbox, flask_port: int = DEFAULT_FLASK_PORT):
        self.sandbox = sandbox
        self.flask_port = flask_port
        self.base_url = f"http://127.0.0.1:{flask_port}"

    # --- 基础 HTTP ---

    def _curl(self, path: str, method: str = "GET",
              json_body: Optional[dict] = None,
              extra_curl_args: str = "",
              timeout: int = 60) -> str:
        url = f"{self.base_url}{path}"
        if json_body is None:
            cmd = f"curl -sS -X {method} {extra_curl_args} {url} || true"
        else:
            # 不走 sandbox.files.write (envd 文件 API 在 micro 环境偶发崩),
            # 改用 commands.run + base64 在沙箱内落 json body, 只依赖稳定的 commands.run。
            import base64
            b64 = base64.b64encode(
                json.dumps(json_body).encode("utf-8")).decode("ascii")
            cmd = (
                f"echo {b64} | base64 -d > /tmp/osworld_body.json && "
                f"curl -sS -X {method} -H 'Content-Type: application/json' "
                f"--data-binary @/tmp/osworld_body.json {extra_curl_args} {url} "
                f"|| true"
            )
        r = self.sandbox.commands.run(cmd, timeout=timeout)
        return (r.stdout or "").strip()

    def _curl_json(self, path: str, method: str = "GET",
                   json_body: Optional[dict] = None,
                   timeout: int = 60) -> Any:
        text = self._curl(path, method=method, json_body=json_body,
                          timeout=timeout)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    # --- 健康检查 ---

    def health(self) -> Optional[dict]:
        text = self._curl("/health")
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    def wait_until_ready(self, timeout_sec: int = 240) -> None:
        deadline = time.time() + timeout_sec
        last_code = ""
        while time.time() < deadline:
            time.sleep(3)
            r = self.sandbox.commands.run(
                f"bash -lc 'curl -s -o /dev/null -w %{{http_code}} "
                f"{self.base_url}/health || echo 000'",
                timeout=15,
            )
            last_code = (r.stdout or "").strip()
            print(f"  /health -> {last_code}")
            if last_code == "200":
                return
        log = self.sandbox.commands.run(
            "tail -n 80 /tmp/entrypoint.log /tmp/flask-server.log "
            "/tmp/xvfb.log /tmp/gnome-session.log 2>/dev/null; true",
            user="root",
            timeout=15,
        )
        print("---- OSWorld 启动诊断日志 ----")
        print(log.stdout)
        raise RuntimeError(
            f"OSWorld 在 {timeout_sec}s 内未就绪 (last code={last_code})"
        )

    # --- /execute 与 /setup/execute ---

    def execute(self, command, shell: bool = False,
                setup: bool = False, timeout: int = 120) -> dict:
        """
        POST /execute (或 /setup/execute) - 在 VM 内执行命令并返回 stdout/stderr。

        对应 osworld 的 PythonController.execute_command / SetupController._execute_setup。
        """
        path = "/setup/execute" if setup else "/execute"
        body = {"command": command, "shell": shell}
        resp = self._curl_json(path, method="POST", json_body=body,
                                timeout=timeout)
        if not isinstance(resp, dict):
            return {"status": "error", "output": "", "error": str(resp),
                    "returncode": -1}
        return resp

    # --- /setup/launch ---

    def setup_launch(self, command, shell: bool = False,
                      timeout: int = 60) -> str:
        body = {"command": command, "shell": shell}
        return self._curl("/setup/launch", method="POST",
                           json_body=body, timeout=timeout)

    # --- /setup/upload (multipart) ---

    # FC 网关对单次 files.write 的 payload 有上限, 大文件必须分块写再拼接。
    # 2026-07-31 micro 全量: 3 个 trial 因 53MB/33MB/29MB 素材直接 EntityTooLarge。
    _UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024

    def _write_sandbox_file(self, path: str, data: bytes, timeout: int = 300) -> None:
        """把 bytes 写进沙箱; 超过分块阈值时切片写 .partN 再 cat 拼回。"""
        if len(data) <= self._UPLOAD_CHUNK_BYTES:
            self.sandbox.files.write(path, data)
            return
        n = 0
        for off in range(0, len(data), self._UPLOAD_CHUNK_BYTES):
            self.sandbox.files.write(
                f"{path}.part{n}", data[off:off + self._UPLOAD_CHUNK_BYTES])
            n += 1
        # 按序拼接后清理分片 (shell 通配排序对 >=10 片会乱序, 故显式列出)
        parts = " ".join(f"'{path}.part{i}'" for i in range(n))
        self.sandbox.commands.run(
            f"cat {parts} > '{path}' && rm -f {parts}", timeout=timeout)
        print(f"     (分 {n} 块上传 {len(data)/1048576:.0f}MB)")

    def setup_upload_local(self, vm_path: str, local_bytes: bytes,
                            filename: str = "upload.bin",
                            timeout: int = 120) -> str:
        """把 bytes 通过 /setup/upload 上传到 VM 路径。先写到沙箱内临时文件,
        再用 curl multipart 上传给 OSWorld Flask。"""
        tmp_path = f"/tmp/osworld_upload_{int(time.time()*1000)}_{filename}"
        self._write_sandbox_file(tmp_path, local_bytes)
        # OSWorld 的 /setup/upload 期望两个表单字段:file_path 和 file_data
        cmd = (
            f"curl -sS -X POST "
            f"-F 'file_path={vm_path}' "
            f"-F 'file_data=@{tmp_path};filename={filename}' "
            f"{self.base_url}/setup/upload || true"
        )
        r = self.sandbox.commands.run(cmd, timeout=timeout)
        return (r.stdout or "").strip()

    # --- /setup/activate_window ---

    def setup_activate_window(self, window_name: str, strict: bool = False,
                               by_class: bool = False) -> str:
        body = {"window_name": window_name, "strict": strict,
                "by_class": by_class}
        return self._curl("/setup/activate_window", method="POST",
                           json_body=body)

    # --- /setup/change_wallpaper ---

    def setup_change_wallpaper(self, path: str) -> str:
        return self._curl("/setup/change_wallpaper", method="POST",
                           json_body={"path": path})

    # --- /screenshot ---

    def screenshot_to_local(self, local_file: str, timeout: int = 60) -> int:
        """把当前桌面截图存到本地文件,返回字节数。"""
        sb_tmp = f"/tmp/osworld_shot_{int(time.time()*1000)}.png"
        self.sandbox.commands.run(
            f"bash -lc 'curl -s -o {sb_tmp} {self.base_url}/screenshot'",
            timeout=timeout,
        )
        data = self.sandbox.files.read(sb_tmp, format="bytes")
        with open(local_file, "wb") as f:
            f.write(data)
        return len(data)

    def screenshot_bytes(self, timeout: int = 60,
                          use_x_direct: bool = True) -> bytes:
        """截取当前桌面,返回 PNG bytes。

        默认 use_x_direct=True (走 import 直接从 X server 截图)。
        v0.0.3-envd 镜像 Flask /screenshot 接口截到的是过期/缓存桌面,
        会导致 LLM agent 看不到实时状态、误以为操作没生效。
        显式 use_x_direct=False 可以走 Flask /screenshot (有 cursor 合成)。
        """
        sb_tmp = f"/tmp/osworld_shot_{int(time.time()*1000)}.png"
        if use_x_direct:
            self.sandbox.commands.run(
                f"DISPLAY=:0 import -window root {sb_tmp}",
                timeout=timeout, user="user",
            )
        else:
            self.sandbox.commands.run(
                f"bash -lc 'curl -s -o {sb_tmp} {self.base_url}/screenshot'",
                timeout=timeout,
            )
        return self.sandbox.files.read(sb_tmp, format="bytes")

    # --- /accessibility ---

    def accessibility_tree(self, timeout: int = 60) -> Optional[str]:
        """拉 GNOME a11y tree (XML), 返回 AT-SPI 暴露的窗口/控件结构.

        每个 visible/enabled 节点带 cp:screencoord (左上角坐标) 和 cp:size,
        给 LLM 提供精确的 UI 控件位置 (避免它瞎猜坐标).
        """
        resp = self._curl_json("/accessibility", method="GET", timeout=timeout)
        if isinstance(resp, dict):
            return resp.get("AT")
        return None

    # --- /list_directory ---

    def list_directory(self, path: str, timeout: int = 60) -> Optional[dict]:
        resp = self._curl_json("/list_directory", method="POST",
                               json_body={"path": path}, timeout=timeout)
        if isinstance(resp, dict):
            return resp.get("directory_tree")
        return None

    # --- /terminal ---

    def terminal_output(self) -> Optional[str]:
        resp = self._curl_json("/terminal")
        if isinstance(resp, dict):
            return resp.get("output")
        return None

    # --- /screen_size /platform ---

    def screen_size(self) -> Optional[dict]:
        return self._curl_json("/screen_size", method="POST")

    def platform_info(self) -> Optional[str]:
        return self._curl("/platform")


class OSWorldSandbox:
    """构建模板 → 创建沙箱 → 拉起 OSWorld → 提供 OSWorldClient。"""

    def __init__(self,
                  image: str = DEFAULT_IMAGE,
                  template_name: Optional[str] = None,
                  flask_port: int = DEFAULT_FLASK_PORT,
                  cpu_count: int = 4,
                  memory_mb: int = 8192,
                  sandbox_timeout: int = 900,
                  api_key: Optional[str] = None):
        if api_key is None:
            api_key = os.environ.get("E2B_API_KEY")
        if not api_key:
            raise RuntimeError("E2B_API_KEY 未设置")
        if not image:
            raise RuntimeError("E2B_TEMPLATE_IMAGE 未设置")
        self.image = image
        self.template_name = template_name or f"osworld-osdomain-{int(time.time())}"
        self.flask_port = flask_port
        self.cpu_count = cpu_count
        self.memory_mb = memory_mb
        self.sandbox_timeout = sandbox_timeout
        self.api_key = api_key
        self.conn_opts = _conn_opts()
        self.sandbox: Optional[Sandbox] = None
        self.client: Optional[OSWorldClient] = None

    # --- 生命周期 ---

    def build_template(self) -> str:
        print(f"[OSWorldSandbox] 构建模板: {self.template_name} (image={self.image})")
        try:
            info = Template.build(
                Template().from_image(image=self.image),
                self.template_name,
                api_key=self.api_key,
                cpu_count=self.cpu_count,
                memory_mb=self.memory_mb,
                **self.conn_opts,
            )
        except Exception:
            print(f"[OSWorldSandbox] 构建失败，最近一次 API 响应元信息:\n{dump_last_resp_meta()}")
            raise
        print(f"[OSWorldSandbox] 模板就绪: {info.name}")
        return info.name

    def create_sandbox(self, template_name: str) -> Sandbox:
        print("[OSWorldSandbox] 创建沙箱")
        sbx = Sandbox.create(
            template=template_name,
            api_key=self.api_key,
            timeout=self.sandbox_timeout,
            **self.conn_opts,
        )
        print(f"[OSWorldSandbox] sandbox_id = {sbx.sandbox_id}")
        self.sandbox = sbx
        self.client = OSWorldClient(sbx, flask_port=self.flask_port)
        return sbx

    def bootstrap_osworld(self) -> None:
        if self.sandbox is None or self.client is None:
            raise RuntimeError("sandbox 尚未创建")
        print(f"[OSWorldSandbox] 以 root 启动 entrypoint.sh (FLASK_PORT={self.flask_port})")
        self.sandbox.commands.run(
            "nohup /usr/local/bin/entrypoint.sh "
            ">/tmp/entrypoint.log 2>&1 < /dev/null &",
            user="root",
            envs={"FLASK_PORT": str(self.flask_port)},
            background=True,
            timeout=10,
        )
        self.client.wait_until_ready()
        print("[OSWorldSandbox] OSWorld 已就绪")

    def start(self) -> OSWorldClient:
        """一次性走完 build → create → bootstrap,返回可用的 client。"""
        name = self.build_template()
        self.create_sandbox(name)
        self.bootstrap_osworld()
        assert self.client is not None
        return self.client

    def kill(self) -> None:
        if self.sandbox is not None:
            try:
                self.sandbox.kill()
            finally:
                self.sandbox = None
                self.client = None

    def __enter__(self) -> OSWorldClient:
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.kill()
