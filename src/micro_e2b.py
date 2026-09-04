"""micro_e2b.py: microsandbox (micro build 模式) 的 e2b SDK 适配层。

公共 API 形状对齐 osworld_micro.py，可直接互换。

必须了解的四条约束：
  1. micro headers（Build-Mode / Alpha-CPU|Memory|Disk-Size / Source-VPC 系列）
     挂在**所有**管理 API 上，不只是 build —— Sandbox.create/get_info/list
     缺了同样会失败，勿删 get_api_params 里的统一注入。
  2. SDK 参数 cpu_count/memory_mb 不生效，实际资源完全由 headers 决定；
     build 走 build_in_background + 自轮询 get_build_status。
  3. 网络补丁（强制 HTTP/1.1、禁 keepalive、数据面 https→http）不在 import
     时全局生效，以免在 harbor 插件进程里误伤 rund 线；调用方须先显式
     apply_micro_patches()。
  4. create 遇 httpx 瞬态错误重试 5 次（30/60/90/120/180s），重试前先用
     Sandbox.list 找回可能已创建成功的沙箱，避免留下孤儿沙箱。
"""
from __future__ import annotations

import dataclasses
import os
import time
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

REQUEST_ID_HEADER = "X-Request-Id"

# 避免重复 patch 的全局标记
_PATCHED_TRANSPORT = False
_PATCHED_HTTPX_CLIENT = False
_PATCHED_SANDBOX_URL = False
_PATCHED_HTTP_LOGGING = False


# ---------------------------------------------------------------- config ---

@dataclasses.dataclass
class MicroConfig:
    """micro sandbox 环境配置 """

    api_key: Optional[str] = None
    api_url: Optional[str] = None
    domain: Optional[str] = None
    ubuntu_image: Optional[str] = None
    source_vpc: bool = True
    vpc_id: Optional[str] = None
    vswitch_ids: Optional[str] = None
    security_group_id: Optional[str] = None
    cpu: str = "16"
    memory: str = "16384"
    disk_size: str = "61440"
    request_timeout: int = 600
    debug: bool = False

    @property
    def env_name(self) -> str:
        """根据域名推断环境名称，用于生成模板名"""
        domain = (self.domain or self.api_url or "").lower()
        if "shanghai-cloudspe" in domain:
            return "shanghai-spe"
        if "mulzone" in domain or "mulzones" in domain:
            return "shanghai-mulzones"
        if "shanghai" in domain:
            return "shanghai"
        return "default"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").lower()
    return value in {"1", "true", "yes"} if value else default


def get_config_from_env(
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    domain: Optional[str] = None,
) -> MicroConfig:
    """从环境变量组装 MicroConfig。"""
    return MicroConfig(
        api_key=(api_key or os.environ.get("E2B_API_KEY")
                 or os.environ.get("E2E_E2B_API_KEY")),
        api_url=api_url or os.environ.get("E2B_API_URL"),
        domain=domain or os.environ.get("E2B_DOMAIN"),
        ubuntu_image=os.environ.get("E2E_MICRO_UBUNTU_IMAGE"),
        source_vpc=_env_bool("E2E_MICRO_SOURCE_VPC", default=True),
        vpc_id=os.environ.get("E2E_MICRO_SOURCE_VPC_ID"),
        vswitch_ids=os.environ.get("E2E_MICRO_SOURCE_VSWITCH_IDS"),
        security_group_id=os.environ.get("E2E_MICRO_SOURCE_SECURITY_GROUP_ID"),
        cpu=os.environ.get("E2E_MICRO_CPU", "16"),
        memory=os.environ.get("E2E_MICRO_MEMORY", "16384"),
        disk_size=os.environ.get("E2E_MICRO_DISK_SIZE", "61440"),
        request_timeout=int(os.environ.get("E2E_REQUEST_TIMEOUT", "600")),
        debug=_env_bool("DEBUG"),
    )


def make_request_id() -> str:
    """生成用于串联本地日志与服务端日志的请求 ID。"""
    return str(uuid.uuid4())


def get_source_vpc_headers(config: MicroConfig) -> Dict[str, str]:
    """源端 VPC 构建请求头"""
    if not config.source_vpc or not (
        config.vpc_id and config.vswitch_ids and config.security_group_id
    ):
        return {}
    return {
        "X-E2B-Template-Source-VPC-ID": config.vpc_id,
        "X-E2B-Template-Source-VSwitch-IDs": config.vswitch_ids,
        "X-E2B-Template-Source-Security-Group-ID": config.security_group_id,
    }


def get_api_params(
    config: MicroConfig,
    extra_headers: Optional[Dict[str, str]] = None,
    request_timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """构造 micro sandbox API 调用参数（micro headers 挂所有管理请求）。"""
    headers = {
        "X-E2B-Template-Build-Mode": "micro",
        "X-E2B-Template-Alpha-CPU": str(config.cpu),
        "X-E2B-Template-Alpha-Memory": str(config.memory),
        "X-E2B-Template-Alpha-Disk-Size": str(config.disk_size),
    }
    if extra_headers:
        headers.update(extra_headers)
    headers.update(get_source_vpc_headers(config))
    headers.setdefault(REQUEST_ID_HEADER, make_request_id())
    headers.setdefault("Connection", "close")

    params: Dict[str, Any] = {
        "request_timeout": request_timeout or config.request_timeout,
        "headers": headers,
        "api_key": config.api_key,
    }
    if config.domain:
        params["domain"] = config.domain
    if config.api_url:
        params["api_url"] = config.api_url
    return params


# --------------------------------------------------------------- patches ---

def _no_keepalive_limits() -> httpx.Limits:
    return httpx.Limits(
        max_keepalive_connections=0,
        max_connections=int(os.getenv("E2B_MAX_CONNECTIONS", "2000")),
    )


def patch_disable_keepalive() -> None:
    """禁用 httpx keepalive 并强制 HTTP/1.1
    ，这里收敛到函数里按需开启（幂等）。
    """
    global _PATCHED_TRANSPORT, _PATCHED_HTTPX_CLIENT
    os.environ.setdefault("E2B_MAX_KEEPALIVE_CONNECTIONS", "0")

    if not _PATCHED_TRANSPORT:
        # 层 1: transport 构造器强制 http2=False + 无 keepalive limits
        # (e2b 的 TransportWithLogger 继承 HTTPTransport, 这里统一覆盖其 kwargs)
        _orig_sync = httpx.HTTPTransport.__init__
        _orig_async = httpx.AsyncHTTPTransport.__init__

        def _patched_sync(self, *args, **kwargs):
            kwargs["http2"] = False
            kwargs["limits"] = _no_keepalive_limits()
            _orig_sync(self, *args, **kwargs)

        def _patched_async(self, *args, **kwargs):
            kwargs["http2"] = False
            kwargs["limits"] = _no_keepalive_limits()
            _orig_async(self, *args, **kwargs)

        httpx.HTTPTransport.__init__ = _patched_sync
        httpx.AsyncHTTPTransport.__init__ = _patched_async
        _PATCHED_TRANSPORT = True

    if not _PATCHED_HTTPX_CLIENT:
        # 层 2: Client 默认 limits 兜底
        _orig_client_init = httpx.Client.__init__

        def _patched_client_init(self, *args, **kwargs):
            kwargs.setdefault("limits", httpx.Limits(
                max_keepalive_connections=0, max_connections=10))
            _orig_client_init(self, *args, **kwargs)

        httpx.Client.__init__ = _patched_client_init
        _PATCHED_HTTPX_CLIENT = True

    # e2b SDK 可能已按旧 limits 缓存了 transport 单例, 清掉让新配置生效
    import sys
    for mod_name in ("e2b.api.client_sync", "e2b.api.client_async"):
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for cls_name in ("TransportWithLogger", "AsyncTransportWithLogger"):
            cls = getattr(mod, cls_name, None)
            if cls is not None and hasattr(cls, "_instances"):
                cls._instances.clear()


def patch_sandbox_url_to_http() -> None:
    """将 sandbox 数据面 URL 强制替换为 HTTP。"""
    global _PATCHED_SANDBOX_URL
    if _PATCHED_SANDBOX_URL:
        return

    from e2b.connection_config import ConnectionConfig

    original_get_sandbox_url = ConnectionConfig.get_sandbox_url

    def http_sandbox_url(self, sandbox_id: str, sandbox_domain: str) -> str:
        url = original_get_sandbox_url(self, sandbox_id, sandbox_domain)
        return url.replace("https://", "http://", 1)

    ConnectionConfig.get_sandbox_url = http_sandbox_url

    # 新版 SDK 才有 get_sandbox_direct_url (e2b 2.24 没有), 有则一并补
    original_direct = getattr(ConnectionConfig, "get_sandbox_direct_url", None)
    if original_direct is not None:

        def http_sandbox_direct_url(self, sandbox_id: str, sandbox_domain: str) -> str:
            url = original_direct(self, sandbox_id, sandbox_domain)
            return url.replace("https://", "http://", 1)

        ConnectionConfig.get_sandbox_direct_url = http_sandbox_direct_url
    _PATCHED_SANDBOX_URL = True


def patch_http_logging() -> None:
    """打印所有 httpx 请求与响应（联调用）。"""
    global _PATCHED_HTTP_LOGGING
    if _PATCHED_HTTP_LOGGING:
        return

    sensitive = ("api-key", "authorization", "access-token", "token", "secret")

    def _redact(headers) -> Dict[str, str]:
        return {
            k: ("<redacted>" if any(p in k.lower() for p in sensitive) else str(v))
            for k, v in dict(headers).items()
        }

    original_send = httpx.Client.send

    def logged_send(self, request: httpx.Request, *args, **kwargs):
        request.headers["Connection"] = "close"
        print(f"[HTTPX REQUEST] {request.method} {request.url}")
        print(f"  headers={_redact(request.headers)}")
        try:
            response = original_send(self, request, *args, **kwargs)
        except Exception as e:
            print(f"[HTTPX ERROR] {type(e).__name__}: {e}")
            raise
        print(f"[HTTPX RESPONSE] {response.status_code} "
              f"{request.method} {request.url}")
        return response

    httpx.Client.send = logged_send
    _PATCHED_HTTP_LOGGING = True


def apply_micro_patches() -> None:
    """应用 micro sandbox 必需的网络与传输层补丁"""
    patch_disable_keepalive()
    patch_sandbox_url_to_http()


# ------------------------------------------------------------ build/create ---

def make_micro_template_name(config: MicroConfig, label: str) -> str:
    """生成 micro 模板名称，控制在 64 字符以内。"""
    env_slug = {
        "shanghai": "hk",
        "shanghai-spe": "spe",
        "shanghai-mulzones": "mulz",
    }.get(config.env_name, config.env_name.replace("_", "-"))
    nonce = str(time.time_ns())[-12:]
    name = f"e2b-micro-{env_slug}-{label}-{nonce}"
    if len(name) <= 64:
        return name
    suffix = f"-{nonce}"
    keep = 64 - len(suffix)
    return f"{name[:keep].rstrip('-')}{suffix}"


def build_micro_template(
    config: MicroConfig,
    template_name: str,
    image: str,
    os_type: str = "linux-amd64",
    port: int = 49983,
    headers: Optional[Dict[str, str]] = None,
    build_timeout_seconds: int = 600,
    build_poll_interval_seconds: int = 1,
    on_build_logs: Optional[Callable[[Any], None]] = None,
) -> Any:
    """从镜像异步构建 micro 模板并轮询等待完成，返回 BuildInfo。

    entrypoint 等额外构建头由调用方经 headers= 传入
    (如 {"X-E2B-Template-Alpha-Micro-Entrypoint": '["sleep", "infinity"]'})。
    """
    from e2b import Template, default_build_logger

    extra_headers = {
        "X-E2B-Template-Alpha-OS-Type": os_type,
        "X-E2B-Template-Alpha-Port": str(port),
    }
    if headers:
        extra_headers.update(headers)

    api_params = get_api_params(config, extra_headers)
    print(f"构建镜像: {image}", flush=True)
    print(f"构建请求头: {api_params.get('headers', {})}", flush=True)

    rootfs_template = Template().from_image(image=image)
    # cpu_count/memory_mb 为 SDK 必填摆设, micro 实际资源由 headers 决定
    template = Template().build_in_background(
        template=rootfs_template,
        name=template_name,
        cpu_count=8,
        memory_mb=8192,
        skip_cache=False,
        on_build_logs=on_build_logs or default_build_logger(),
        **api_params,
    )
    print(f"模板: name={template.name} id={template.template_id} "
          f"build={template.build_id}", flush=True)

    for _ in range(build_timeout_seconds // build_poll_interval_seconds):
        status = Template.get_build_status(
            template, logs_offset=0, **get_api_params(config, extra_headers))
        status_value = status.status.value
        if status_value == "ready":
            print("模板构建完成", flush=True)
            return template
        if status_value in {"building", "waiting"}:
            print(f"等待模板构建: {status_value}", flush=True)
            time.sleep(build_poll_interval_seconds)
            continue
        if status_value == "error":
            raise RuntimeError(f"模板构建失败: {status}")
        raise RuntimeError(f"意外的模板构建状态: {status}")

    raise TimeoutError(f"模板构建超时 ({build_timeout_seconds}s)")


def _find_recent_sandbox(config: MicroConfig, template_id: str) -> Any:
    """查找基于指定 template 且最近启动的沙箱（create 异常后的恢复路径）。"""
    from e2b import Sandbox

    try:
        paginator = Sandbox.list(limit=50, **get_api_params(config))
        candidates = []
        while paginator.has_next:
            for info in paginator.next_items():
                if info.template_id == template_id and str(info.state) not in {
                    "killed", "dead", "error",
                }:
                    candidates.append(info)
        if not candidates:
            return None
        latest = max(candidates, key=lambda info: info.started_at)
        print(f"找到最近沙箱: {latest.sandbox_id} state={latest.state} "
              f"started_at={latest.started_at}", flush=True)
        return Sandbox.connect(latest.sandbox_id, timeout=900,
                               **get_api_params(config))
    except Exception as e:  # noqa: BLE001
        print(f"恢复沙箱失败: {type(e).__name__}: {e}", flush=True)
        return None


def create_micro_sandbox(
    config: MicroConfig,
    template_id: str,
    extra_headers: Optional[Dict[str, str]] = None,
    max_retries: int = 5,
    retry_wait_seconds: Tuple[int, ...] = (30, 60, 90, 120, 180),
    timeout: int = 900,
) -> Any:
    """基于已构建的 micro 模板创建沙箱，瞬态网络错误重试 + 找回恢复。"""
    from e2b import Sandbox

    sandbox = None
    for attempt in range(1, max_retries + 1):
        try:
            api_params = get_api_params(
                config,
                extra_headers,
                request_timeout=max(config.request_timeout, 1200),
            )
            sandbox = Sandbox.create(
                template=template_id, timeout=timeout, **api_params)
            print(f"沙箱创建成功: {sandbox.sandbox_id}", flush=True)
            break
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as e:
            print(f"Sandbox.create 遇到瞬时网络错误 {type(e).__name__} "
                  f"(attempt {attempt}/{max_retries}): {e}", flush=True)
            if attempt < max_retries:
                wait_time = retry_wait_seconds[attempt - 1]
                print(f"等待 {wait_time}s 后尝试恢复...", flush=True)
                time.sleep(wait_time)
                recovered = _find_recent_sandbox(config, template_id)
                if recovered is not None:
                    sandbox = recovered
                    print(f"恢复沙箱: {sandbox.sandbox_id}", flush=True)
                    break
                print("未找到可恢复沙箱，重试 Sandbox.create", flush=True)
            else:
                raise
    if sandbox is None:
        raise RuntimeError("Sandbox.create 多次重试后失败")

    sandbox_info = Sandbox.get_info(
        sandbox_id=sandbox.sandbox_id, **get_api_params(config, extra_headers))
    print(f"沙箱信息: {sandbox_info}", flush=True)
    return sandbox


def cleanup_sandbox(config: MicroConfig, sandbox: Any) -> None:
    """非调试模式下销毁沙箱。"""
    if not config.debug:
        sandbox.kill()


def cleanup_template(config: MicroConfig, template_id: str) -> None:
    """非调试模式下删除模板（用 httpx 代替 requests）。"""
    if config.debug or not config.api_url:
        return
    response = httpx.delete(
        f"{config.api_url}/templates/{template_id}",
        headers={
            "X-API-Key": config.api_key or "",
            "Accept": "application/json",
            REQUEST_ID_HEADER: make_request_id(),
        },
        timeout=30,
    )
    if response.status_code not in (200, 202, 204, 404):
        raise RuntimeError(
            f"删除模板失败: status={response.status_code}, body={response.text}")
