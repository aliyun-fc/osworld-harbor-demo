"""
setup_runner.py - osworld benchmark `SetupController` 的极简等价实现。

OSWorld 的 SetupController(desktop_env/controllers/setup.py)读取任务 JSON
里的 config 数组,按顺序对 VM 执行 _execute_setup / _launch_setup /
_sleep_setup / _activate_window_setup / _download_setup 等。

这里只实现 OS-domain 任务用得到的几种(execute、launch、sleep、
activate_window、download),所有 VM 端动作都委托给 OSWorldClient,在 e2b
沙箱内通过 curl 调 OSWorld Flask。

注意 OSWorld 在执行 execute 命令前会做几个变量替换:
    {CLIENT_PASSWORD} {SCREEN_WIDTH} {SCREEN_HEIGHT}
    {SCREEN_WIDTH_HALF} {SCREEN_HEIGHT_HALF}
我们这里也保持一致,默认屏幕 1920x1080。
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import urllib.request
from typing import Any, Dict, List, Union

from osworld_e2b import OSWorldClient

logger = logging.getLogger(__name__)


def _substitute(command: Union[str, List[str]],
                client_password: str,
                screen_width: int,
                screen_height: int) -> Union[str, List[str]]:
    width_half = str(screen_width // 2)
    height_half = str(screen_height // 2)

    def sub(s: str) -> str:
        return (
            s.replace("{CLIENT_PASSWORD}", client_password)
             .replace("{SCREEN_WIDTH_HALF}", width_half)
             .replace("{SCREEN_HEIGHT_HALF}", height_half)
             .replace("{SCREEN_WIDTH}", str(screen_width))
             .replace("{SCREEN_HEIGHT}", str(screen_height))
        )

    if isinstance(command, str):
        return sub(command)
    return [sub(item) for item in command]


class SetupRunner:
    def __init__(self, client: OSWorldClient,
                  client_password: str = "password",
                  screen_width: int = 1920,
                  screen_height: int = 1080,
                  cache_dir: str = ".cache/osworld_demo"):
        self.client = client
        self.client_password = client_password
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    # --- 入口 ---

    def setup(self, configs: List[Dict[str, Any]]) -> None:
        for i, cfg in enumerate(configs):
            cfg_type = cfg["type"]
            params = cfg.get("parameters", {})
            handler = getattr(self, f"_setup_{cfg_type}", None)
            if handler is None:
                logger.warning("跳过未实现的 setup type: %s", cfg_type)
                continue
            print(f"  [setup {i+1}/{len(configs)}] {cfg_type}({params})")
            handler(**params)
        # 触发 GNOME ding 扩展刷新桌面图标
        # (setup 创建/删除 ~/Desktop/* 后, ding 不一定能及时通过 inotify 看到)
        # 通过 disable/enable 强制 ding 重新扫描 ~/Desktop
        reload_cmd = (
            "export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus && "
            "export DISPLAY=:0 && "
            "gnome-extensions disable ding@rastersoft.com 2>/dev/null; "
            "sleep 1; "
            "gnome-extensions enable ding@rastersoft.com 2>/dev/null; "
            "sleep 2"
        )
        try:
            self.client.execute(reload_cmd, shell=True, setup=False, timeout=15)
        except Exception:
            pass

    # --- 各 type handler ---

    def _setup_execute(self, command: Union[str, List[str]],
                        shell: bool = False, **_extra) -> None:
        cmd = _substitute(command, self.client_password,
                           self.screen_width, self.screen_height)
        resp = self.client.execute(cmd, shell=shell, setup=True, timeout=180)
        if isinstance(resp, dict) and resp.get("status") == "success":
            rc = resp.get("returncode")
            stderr = (resp.get("error") or "").strip()
            if stderr:
                print(f"     stderr: {stderr[:200]}")
            print(f"     returncode={rc}")
        else:
            print(f"     /setup/execute 返回: {resp}")

    def _setup_launch(self, command: Union[str, List[str]],
                       shell: bool = False, **_extra) -> None:
        cmd = _substitute(command, self.client_password,
                           self.screen_width, self.screen_height)
        text = self.client.setup_launch(cmd, shell=shell)
        print(f"     /setup/launch 返回: {text[:200]}")

    def _setup_command(self, command: Union[str, List[str]],
                        **kwargs) -> None:
        # OSWorld 的 _command_setup 直接转发到 _execute_setup
        self._setup_execute(command, **kwargs)

    # open 回退用: 扩展名 → 直接拉起的应用 (老 server 无 /setup/open_file,
    # 且 xdg-open 在 envd 上下文里静默无效——rc=0 但不开窗口, 实测)
    _OPEN_APP_BY_EXT = [
        ({"xlsx", "xls", "ods", "csv"}, "soffice --calc"),
        ({"docx", "doc", "odt", "rtf"}, "soffice --writer"),
        ({"pptx", "ppt", "odp"}, "soffice --impress"),
        ({"pdf"}, "evince"),
        ({"mp4", "mp3", "avi", "mkv", "wav", "m4a", "flac", "ogg", "3gp",
          "mov"}, "vlc"),
        ({"png", "jpg", "jpeg", "gif", "bmp", "webp"}, "eog"),
    ]

    def _setup_open(self, path: str, **_extra) -> None:
        """用默认应用打开文件 (osworld-verified 的 open 步骤, 155 个任务依赖)。
        新版 server: POST /setup/open_file; 杭州 v0.0.3 老 server 没有该端点
        (实测 404), 回退为按扩展名直接后台拉起对应应用 (DISPLAY=:0)。"""
        resp = self.client._curl_json("/setup/open_file", method="POST",
                                       json_body={"path": path}, timeout=300)
        if isinstance(resp, str) and "Not Found" in resp:
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            app = "xdg-open"
            for exts, cmd in self._OPEN_APP_BY_EXT:
                if ext in exts:
                    app = cmd
                    break
            import shlex
            self.client.sandbox.commands.run(
                f"DISPLAY=:0 nohup {app} {shlex.quote(path)} "
                f">/tmp/osw_open.log 2>&1 &",
                user="user", background=True, timeout=15,
            )
            resp = f"(fallback) {app} {path}"
        print(f"     open({path}) 返回: {str(resp)[:150]}")
        # LibreOffice 等冷启动慢, 等窗口起来再让 agent 截第一张图
        time.sleep(12)

    def _setup_sleep(self, seconds: float, **_extra) -> None:
        time.sleep(seconds)

    def _setup_activate_window(self, window_name: str,
                                strict: bool = False,
                                by_class: bool = False, **_extra) -> None:
        text = self.client.setup_activate_window(window_name, strict, by_class)
        print(f"     /setup/activate_window 返回: {text[:200]}")

    @staticmethod
    def _mirror_url(url: str) -> str:
        """huggingface.co 不通时自动换 hf-mirror.com"""
        return url.replace("huggingface.co", "hf-mirror.com")

    def _download_with_fallback(self, url: str, cache_path: str) -> None:
        # 国内网络: 镜像源优先, 原站兼底 (避免对 huggingface.co 白等 120s 超时)
        candidates = [self._mirror_url(url), url]
        if candidates[0] == candidates[1]:
            candidates = [url]
        last_exc: Exception | None = None
        for attempt_url in candidates:
            try:
                print(f"     下载 {attempt_url} -> {cache_path}")
                req = urllib.request.Request(
                    attempt_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                # 原子写: 共享缓存目录下多 trial 并发下载同一文件时防半成品
                tmp_path = f"{cache_path}.tmp.{os.getpid()}"
                with open(tmp_path, "wb") as fp:
                    fp.write(data)
                os.replace(tmp_path, cache_path)
                return
            except Exception as e:
                print(f"     下载失败: {e}")
                last_exc = e
        raise last_exc  # type: ignore[misc]

    def _setup_download(self, files: List[Dict[str, str]], **_extra) -> None:
        """先在本机下载,再 multipart 上传到 VM。"""
        for f in files:
            url = f["url"]
            path = f["path"]
            url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
            cache_path = os.path.join(
                self.cache_dir, f"{url_hash}_{os.path.basename(path)}"
            )
            if not os.path.exists(cache_path):
                self._download_with_fallback(url, cache_path)
            with open(cache_path, "rb") as fp:
                data = fp.read()
            text = self.client.setup_upload_local(
                path, data, filename=os.path.basename(path)
            )
            print(f"     上传 {path}: {text[:200]}")
