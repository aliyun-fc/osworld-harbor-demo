"""MicroEnvironment: Harbor BaseEnvironment adapter over e2b microsandbox.

与 RundEnvironment（FC rund microVM）的唯一差异在"沙箱怎么来"：
  - 先套 micro 网络补丁（禁 keepalive + 数据面 https→http, 见 micro_e2b.py）
  - template= 时直接 Sandbox.create（带退避重试）；否则用 micro build 模式现建
其余（exec/upload/download/心跳/stop/setup.sh）全部复用 RundEnvironment。

Register with Harbor WITHOUT touching harbor source:
  harbor run --env /abs/path/micro_environment.py:MicroEnvironment \
             --environment-kwarg template=<prebuilt_micro_template>   # skip build
  (or --environment-kwarg image=<registry/image:tag> to micro-build first)

Key knobs (via --environment-kwarg or env):
  template   : reuse an existing micro template (MICRO_TEMPLATE)
  image      : image to micro-build from (MICRO_IMAGE / E2E_MICRO_UBUNTU_IMAGE)
  os_type    : micro OS type, default linux-amd64
  data_port  : micro 数据面端口 (build header 用), default 49983
  micro_entrypoint : 容器 entrypoint JSON, default ["sleep", "infinity"]
  E2B_API_KEY / E2B_API_URL / E2B_DOMAIN + E2E_MICRO_* : from env (.env.micro)

沙箱存活时长由 sandbox_timeout_sec 控制。
"""
from __future__ import annotations

import os
from typing import Any

import micro_e2b
from rund_environment import RundEnvironment


class MicroEnvironment(RundEnvironment):
    _log_prefix = "micro"

    # The micro image starts OSWorld Flask on port 5000 through systemd.
    _exec_env_defaults: dict[str, str] = {
        "VM_NET_IP": "127.0.0.1",
        "OSWORLD_SERVER_PORT": "5000",
    }

    def __init__(
        self,
        *args: Any,
        template: str | None = None,
        image: str | None = None,
        os_type: str = "linux-amd64",
        data_port: int = 49983,
        micro_entrypoint: str = '["sleep", "infinity"]',
        **kwargs: Any,
    ) -> None:
        self._os_type = os_type
        self._data_port = int(data_port)
        self._micro_entrypoint = micro_entrypoint
        # 显式解析 micro 自己的 env 变量, 避免落到父类的 RUND_TEMPLATE/RUND_IMAGE
        template = template or os.environ.get("MICRO_TEMPLATE")
        image = (
            image
            or os.environ.get("MICRO_IMAGE")
            or os.environ.get("E2E_MICRO_UBUNTU_IMAGE")
        )
        super().__init__(*args, template=template, image=image, **kwargs)

    @staticmethod
    def type() -> str:
        return "micro"

    def _create_sandbox_blocking(self) -> Any:
        # 补丁必须在任何 e2b 网络操作前生效（幂等，可与并发 trial 共存）
        micro_e2b.apply_micro_patches()

        cfg = micro_e2b.get_config_from_env(
            api_key=self._api_key, api_url=self._api_url, domain=self._domain
        )
        template = self._template
        if not template:
            # 模版命名: e2b-micro-<env>-<label>-<nonce>, 自动控在 64 字符内
            name = micro_e2b.make_micro_template_name(cfg, self.environment_name)
            self.logger.info(
                "micro build template name=%s image=%s os_type=%s",
                name, self._image, self._os_type,
            )
            info = micro_e2b.build_micro_template(
                cfg,
                name,
                self._image,
                os_type=self._os_type,
                port=self._data_port,
                # entrypoint 等额外构建头经 headers 传入
                headers={
                    "X-E2B-Template-Alpha-Micro-Entrypoint": self._micro_entrypoint,
                } if self._micro_entrypoint else None,
            )
            # 用 template_id 创建, 避开 alias 传播延迟
            template = info.template_id
            self.logger.info("micro template ready template_id=%s", template)
        return micro_e2b.create_micro_sandbox(
            cfg, template, timeout=self._sbx_timeout
        )
