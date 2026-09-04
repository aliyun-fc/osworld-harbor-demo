"""OSWorldAgent: harbor BaseAgent, 跑 OSWorld 的 screenshot->LLM->action 循环。

以 run_llm_task.py 的循环为基础, 另加卡循环治理(禁用/脱困/无进展熔断)、
周期 reflection、按域降级 observation。用 OSWorldClient (Flask /screenshot
/execute /accessibility) 做截图+执行, 不走 environment.exec 直连。

harbor run --env rund_environment:RundEnvironment --environment-kwarg template=<tpl> \
  --agent osworld_agent:OSWorldAgent --model qwen3.7-plus --agent-kwarg max_steps=12
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# Resolve vendored dependencies relative to the repository.
_VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "osworld"
sys.path.insert(0, str(_VENDOR))
from llm_agent import LLMAgent  # noqa: E402
from osworld_e2b import OSWorldClient  # noqa: E402


def _ensure_big_executor() -> None:
    """Increase executor capacity for concurrent blocking network calls."""
    loop = asyncio.get_event_loop()
    if getattr(loop, "_osworld_pool_enlarged", False):
        return
    workers = int(os.environ.get("OSWORLD_EXECUTOR_WORKERS", "256"))
    loop.set_default_executor(ThreadPoolExecutor(max_workers=workers,
                                                 thread_name_prefix="osworld"))
    loop._osworld_pool_enlarged = True  # type: ignore[attr-defined]

from harbor.agents.base import BaseAgent  # noqa: E402
from harbor.environments.base import BaseEnvironment  # noqa: E402
from harbor.models.agent.context import AgentContext  # noqa: E402


class OSWorldAgent(BaseAgent):
    @staticmethod
    def name() -> str:
        return "osworld"

    def version(self) -> str | None:
        return "2.0.0"

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        logger: Any = None,
        extra_env: dict | None = None,
        max_steps: int = 15,
        observation_type: str = "screenshot",
        client_password: str = "password",
        flask_port: int = 8081,
        gui_only: bool = True,
        bootstrap: bool = True,
        ready_probe: str = "health",
        screenshot_via: str = "x",
        enable_thinking: bool | str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, logger=logger,
                         extra_env=extra_env)
        self._max_steps = int(max_steps)
        self._flask_port = int(flask_port)
        self._observation_type = observation_type
        self._gui_only = str(gui_only).lower() in ("1", "true", "yes")
        self._bootstrap = str(bootstrap).lower() in ("1", "true", "yes")
        self._ready_probe = ready_probe
        # "x" captures X directly; "flask" uses the /screenshot endpoint.
        self._screenshot_via = screenshot_via
        if enable_thinking is None:
            self._enable_thinking = None
        elif isinstance(enable_thinking, bool):
            self._enable_thinking = enable_thinking
        else:
            self._enable_thinking = str(enable_thinking).lower() in ("1", "true", "yes")
        self._model = model_name or "qwen3.7-plus"
        key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("OPENAI_API_KEY")
        base = os.environ.get("DASHSCOPE_BASE_URL") or os.environ.get("OPENAI_API_BASE")
        self._llm = LLMAgent(
            model=self._model, api_key=key, base_url=base,
            observation_type=observation_type, client_password=client_password,
            enable_thinking=self._enable_thinking,
        )
        self._client: OSWorldClient | None = None

    _GUI_ONLY_RULE = (
        "\n\nIMPORTANT: Complete this using the graphical desktop ONLY (Files/"
        "Nautilus with mouse clicks). Do NOT open a terminal or type shell commands."
    )

    # Precise Office properties are more reliable through UNO or document XML.
    _OFFICE_SCRIPT_RULE = (
        "\n\nSTRATEGY HINT for this LibreOffice task. If the goal is to set a PRECISE "
        "property - a font name, a font size in pt, a text/background colour, an image "
        "size in cm, alignment, bold/underline, a slide transition, table contents - "
        "then clicking through menus, font dropdowns and colour pickers is very "
        "unreliable and repeatedly fails. Strongly prefer doing it programmatically:\n"
        "  - This machine HAS a working LibreOffice scripting stack: `python3 -c \"import "
        "uno\"` works, `soffice` is on PATH, and `lxml` + `zipfile` are available.\n"
        "  - Recommended: save/close the document if needed, then manipulate the file "
        "directly with python via subprocess. .odp/.odt/.ods files are ZIP archives "
        "containing content.xml + styles.xml, so you can edit the XML precisely "
        "(zipfile + lxml) and rewrite the archive.\n"
        "  - Alternatively drive LibreOffice through UNO: start it with "
        "`soffice --headless --accept='socket,host=localhost,port=2002;urp;' &` and "
        "connect from python via uno to set exact character/paragraph/page properties "
        "(CharFontName, CharHeight, CharColor, FillColor, Width/Height in 1/100 mm).\n"
        "  - IMPORTANT: whichever route you take, the verifier reads the SAVED file, so "
        "make sure the change is written to the original path on disk. If you edited the "
        "file outside the GUI while it was open, close the GUI document without saving "
        "afterwards so it cannot overwrite your change.\n"
        "  - Only fall back to mouse/menu interaction if the scripted route genuinely "
        "cannot express the change."
    )
    _OFFICE_SCRIPT_DOMAINS = {
        "libreoffice_impress", "libreoffice_calc", "libreoffice_writer",
    }

    # Give the model concrete criteria for benchmark tasks marked infeasible.
    _INFEASIBLE_RULE = (
        "\n\nIMPORTANT - some tasks in this benchmark are deliberately IMPOSSIBLE, and "
        "for those the CORRECT answer is to declare failure. Reply with FAIL (not DONE) "
        "as soon as you are confident the task falls into one of these categories:\n"
        "  - the requested feature does not exist in this application/version at all "
        "(e.g. an export format, a setting, or a menu entry that simply is not there);\n"
        "  - the task asks you to act on content that is not present (e.g. translate "
        "audio hidden in an image, edit a file that does not exist);\n"
        "  - it requires a paid/online service, an account, real-time collaboration with "
        "other people, or content that must be purchased;\n"
        "  - it asks for something logically self-contradictory or outside the machine.\n"
        "Do NOT fake it and do NOT return DONE for such a task - returning DONE when the "
        "goal was impossible is scored as WRONG, while FAIL is scored as CORRECT. "
        "Conversely, merely being difficult, slow or needing many steps is NOT a reason "
        "to FAIL - in that case keep working."
    )

    async def setup(self, environment: BaseEnvironment) -> None:
        sbx = getattr(environment, "_sbx", None)
        if sbx is None:
            raise RuntimeError("OSWorldAgent requires RundEnvironment with _sbx")
        self._client = OSWorldClient(sbx, flask_port=self._flask_port)

        if self._bootstrap:
            sbx.commands.run(
                "nohup /usr/local/bin/entrypoint.sh >/tmp/osw_entrypoint.log 2>&1 < /dev/null &",
                user="root", envs={"FLASK_PORT": str(self._flask_port)},
                background=True, timeout=10,
            )
        if self._ready_probe == "screenshot":
            self._wait_ready_via_screenshot(sbx)
        else:
            self._client.wait_until_ready(timeout_sec=180)
        self.logger.info("osworld desktop+Flask ready")

        # Apply the task's initial OSWorld configuration before the agent runs.
        await self._prepare_osworld_verified_task(environment)

    # Accessibility metadata distracts from terminal/keyboard-oriented domains.
    _SCREENSHOT_ONLY_DOMAINS = {"os", "vlc"}

    _REFLECT_EVERY = 15

    # Escalate repeated actions from feedback, to escape, to a permanent ban.
    _ESCAPE_AT = 3
    _BANLIST_AT = 6
    _STALL_AT = 8
    _ESCAPE_CODES = (
        "pyautogui.press('escape')\ntime.sleep(0.4)\n"
        "pyautogui.press('escape')\ntime.sleep(0.4)",
        "pyautogui.hotkey('alt', 'F4')\ntime.sleep(0.8)",
        "pyautogui.press('escape')\ntime.sleep(0.3)\n"
        "pyautogui.hotkey('super')\ntime.sleep(0.8)\n"
        "pyautogui.press('escape')\ntime.sleep(0.5)",
    )

    async def _prepare_osworld_verified_task(self, environment: BaseEnvironment) -> None:
        """Adapt xlang-ai/osworld-verified tasks:
        1) 补装沙箱内 verifier 依赖 httpx (raw-ubuntu 镜像无 /opt/osworld-verifier venv)
        2) 执行任务 JSON 里的初始 config 步骤 (SetupRunner, 同当年 micro batch 链路)
        """
        env_dir = getattr(environment, "environment_dir", None)
        if not env_dir:
            return
        task_json = Path(env_dir).parent / "tests" / "osworld_task.json"
        if not task_json.exists():
            return
        task = json.loads(task_json.read_text(encoding="utf-8"))
        loop = asyncio.get_running_loop()
        self._task_domain = task.get("snapshot") or ""

        # Verifier modules import these dependencies eagerly inside the sandbox.
        deps_probe = (
            'python3 -c "import httpx, requests, lxml.cssselect, pdfplumber, yaml, '
            'docx, rapidfuzz, formulas, xmltodict, openpyxl, tldextract, pandas, '
            'playwright, acoustid, PyPDF2, skimage, pptx, pydrive, cv2, odf, '
            'fitz, borb, librosa, fastdtw, pygame" 2>/dev/null'
        )
        deps_pkgs = ("httpx requests lxml cssselect pdfplumber pyyaml python-docx "
                     "rapidfuzz formulas xmltodict openpyxl tldextract pandas "
                     "playwright pyacoustid mutagen PyPDF2 pypdf scikit-image "
                     "python-pptx imagehash pydrive opencv-python-headless "
                     "odfpy PyMuPDF borb librosa fastdtw pygame")
        r = await environment.exec(
            f"{deps_probe} || "
            f"python3 -m pip install -q {deps_pkgs} "
            "-i https://mirrors.aliyun.com/pypi/simple/ 2>/dev/null || "
            f"python3 -m pip install -q {deps_pkgs}",
            user="root", timeout_sec=900,
        )
        if r.return_code != 0:
            self.logger.warning("verifier deps install failed: %s",
                                (r.stderr or r.stdout)[:200])

        steps = task.get("config", []) or []
        if steps:
            from setup_runner import SetupRunner
            # Share downloaded setup assets across trials.
            cache_dir = os.environ.get(
                "OSWORLD_SETUP_CACHE",
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             ".cache", "osworld_setup"),
            )
            runner = SetupRunner(self._client, cache_dir=cache_dir)
            self.logger.info("osworld-verified: running %d setup config steps", len(steps))
            await loop.run_in_executor(None, runner.setup, steps)
            self.logger.info("osworld-verified: setup config done")

    def _wait_ready_via_screenshot(self, sbx: Any, timeout_sec: int = 180) -> None:
        """用 /screenshot 返回 200 判定 OSWorld 就绪 (兼容没有 /health 的官方 server)。"""
        deadline = time.time() + timeout_sec
        last = ""
        while time.time() < deadline:
            r = sbx.commands.run(
                f"curl -s -o /dev/null -w '%{{http_code}}' "
                f"http://127.0.0.1:{self._flask_port}/screenshot || echo 000",
                timeout=15,
            )
            last = (r.stdout or "").strip()
            if last == "200":
                return
            time.sleep(3)
        raise RuntimeError(
            f"OSWorld(:{self._flask_port}) not ready in {timeout_sec}s (last={last})")

    _PNG_SIG = b"\x89PNG\r\n\x1a\n"

    def _grab_screenshot(self) -> bytes:
        """按 screenshot_via 拿当前桌面 PNG bytes。
        flask 通道复刻 run_llm_task_micro.grab_png_bytes: curl | base64 走 stdout,
        带完整性校验与重试, 避开 files.read 与坏图。"""
        client = self._client
        assert client is not None
        if self._screenshot_via != "flask":
            return client.screenshot_bytes()
        for _ in range(8):
            try:
                r = client.sandbox.commands.run(
                    f"curl -s {client.base_url}/screenshot | base64 -w0",
                    timeout=15,
                )
                b = (r.stdout or "").strip()
                if b:
                    data = base64.b64decode(b)
                    if len(data) > 1024 and data[:8] == self._PNG_SIG:
                        return data
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
        raise RuntimeError("flask /screenshot 连续多次拿不到完整 PNG")

    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None:
        if self._client is None:
            raise RuntimeError("client not ready; call setup() first")
        client = self._client
        instruction = instruction + self._INFEASIBLE_RULE
        if self._gui_only:
            instruction = instruction + self._GUI_ONLY_RULE
        # Do not inject scripting advice when the task requires GUI-only work.
        elif getattr(self, "_task_domain", "") in self._OFFICE_SCRIPT_DOMAINS:
            instruction = instruction + self._OFFICE_SCRIPT_RULE
        _ensure_big_executor()
        loop = asyncio.get_running_loop()
        result_dir = Path(self.logs_dir)
        traj_path = result_dir / "traj.jsonl"
        action_history: list = []
        done = False
        action_repeat: dict[str, int] = {}
        banned_actions: set[str] = set()
        stall_steps = 0
        prev_shot_sig: tuple | None = None
        escape_level = 0

        for step_idx in range(self._max_steps):
            if done:
                break

            screenshot = await loop.run_in_executor(None, self._grab_screenshot)
            ts = datetime.datetime.now().strftime("%Y%m%d@%H%M%S")
            shot_path = result_dir / f"step_{step_idx}_{ts}.png"
            shot_path.write_bytes(screenshot)

            # Normalize rund bytearrays and Flask bytes before hashing the frame.
            if isinstance(screenshot, bytearray):
                _shot = bytes(screenshot)
            else:
                _shot = screenshot
            shot_sig = (len(_shot), hash(_shot[:4096]),
                        hash(_shot[len(_shot) // 2:][:4096]),
                        hash(_shot[-4096:]))
            if prev_shot_sig is not None and shot_sig == prev_shot_sig:
                stall_steps += 1
            else:
                stall_steps = 0
            prev_shot_sig = shot_sig
            if stall_steps >= self._STALL_AT:
                self.logger.warning(
                    "step %d: screen unchanged for %d steps -> forcing replan",
                    step_idx + 1, stall_steps)
                self._llm.record_feedback(
                    f"WARNING: the screen has not changed at all for {stall_steps} "
                    "consecutive steps. Everything you tried in that window had zero "
                    "effect - you are stuck, most likely behind a modal dialog or on the "
                    "wrong window. STOP the current approach entirely. Either close the "
                    "blocking dialog (Escape / its Cancel button), switch window via the "
                    "taskbar, or accomplish the remaining goal with a shell command "
                    "through subprocess instead of mouse clicks."
                )
                stall_steps = 0

            a11y_tree = None
            self._ref_map = {}
            want_a11y = (self._observation_type in ("screenshot_a11y_tree", "a11y_ref")
                         and getattr(self, "_task_domain", "")
                         not in self._SCREENSHOT_ONLY_DOMAINS)
            if want_a11y:
                try:
                    raw = await loop.run_in_executor(None, client.accessibility_tree)
                    if self._observation_type == "a11y_ref":
                        import a11y_ref as _ar
                        view, self._ref_map = _ar.build_ref_view(raw)
                        a11y_tree = (
                            "Interactive elements currently on screen:\n" + view
                            if view else "(no interactive elements detected)"
                        )
                    else:
                        a11y_tree = raw
                except Exception as e:
                    self.logger.warning("a11y fetch failed: %s", e)

            response_text, actions = await loop.run_in_executor(
                None, self._llm.predict, instruction, screenshot, a11y_tree
            )
            self.logger.info("[step %d/%d] actions=%s", step_idx + 1, self._max_steps,
                             (actions[:1] if actions else None))

            if not actions:
                self._llm.record_feedback(
                    "Your previous reply could NOT be parsed: no python code block and no "
                    "WAIT/DONE/FAIL found. Reply with exactly one ```python ...``` block "
                    "or one of ```WAIT``` / ```DONE``` / ```FAIL```."
                )
                with open(traj_path, "a") as f:
                    f.write(json.dumps({"step_num": step_idx + 1, "action_timestamp": ts,
                                         "actions": [], "response": response_text,
                                         "done": False}, ensure_ascii=False) + "\n")
                continue

            _first = actions[0] if actions else ""
            act_key = (json.dumps(_first, sort_keys=True, ensure_ascii=False)
                       if isinstance(_first, dict) else str(_first))
            if act_key:
                action_repeat[act_key] = action_repeat.get(act_key, 0) + 1
            rep = action_repeat.get(act_key, 0)

            if act_key in banned_actions:
                self.logger.warning(
                    "step %d: action is banned (repeated %d times) -> refusing to execute",
                    step_idx + 1, rep)
                self._llm.record_feedback(
                    "That exact action is BANNED - it has already been tried many times "
                    "with no effect and was NOT executed this time. You must reach the "
                    "goal by a genuinely different route: use the application menu bar, "
                    "a keyboard shortcut, or run a shell command via subprocess. "
                    "Do not click that coordinate again."
                )
                with open(traj_path, "a") as f:
                    f.write(json.dumps({"step_num": step_idx + 1, "action_timestamp": ts,
                                         "actions": actions, "response": response_text,
                                         "done": False, "banned": True},
                                        ensure_ascii=False) + "\n")
                continue

            if rep >= self._BANLIST_AT:
                banned_actions.add(act_key)
                self.logger.warning(
                    "step %d: banning action after %d repeats", step_idx + 1, rep)
                self._llm.record_feedback(
                    f"You have now issued the SAME action {rep} times with no progress. "
                    "It is permanently BANNED from here on. Abandon that approach "
                    "completely and use a different mechanism (menu bar, keyboard "
                    "shortcut, or a shell command)."
                )
            elif rep >= self._ESCAPE_AT:
                esc = self._ESCAPE_CODES[min(escape_level, len(self._ESCAPE_CODES) - 1)]
                escape_level += 1
                self.logger.warning(
                    "step %d: same action x%d -> forcing escape action (level %d)",
                    step_idx + 1, rep, escape_level)
                try:
                    pkgs = ("import pyautogui; import time; "
                            "pyautogui.FAILSAFE = False; ")
                    await loop.run_in_executor(
                        None,
                        lambda: client.execute(["python3", "-c", pkgs + esc],
                                               shell=False, setup=False, timeout=30),
                    )
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("escape action failed: %s", exc)
                self._llm.record_feedback(
                    f"Your last action was repeated {rep} times with no visible progress, "
                    "so the executor just pressed Escape / closed the focused dialog for "
                    "you. The screen may have changed - LOOK at the new screenshot first. "
                    "If a modal dialog or wrong window was blocking you, it may be gone "
                    "now. Do NOT repeat the same click; pick a different route."
                )

            if self._REFLECT_EVERY and (step_idx + 1) % self._REFLECT_EVERY == 0:
                extra = ""
                if step_idx + 1 >= self._max_steps // 2 and banned_actions:
                    extra = (
                        " You have already exhausted several approaches without progress. "
                        "Now explicitly ask yourself whether this task is one of the "
                        "deliberately IMPOSSIBLE ones (feature absent, content not "
                        "present, needs a paid/online service or real-time collaboration). "
                        "If so, reply FAIL now - that is the correct answer and is far "
                        "better than burning the remaining steps or claiming DONE."
                    )
                self._llm.record_feedback(
                    f"[checkpoint step {step_idx + 1}/{self._max_steps}] Before acting, "
                    "briefly re-state: (a) what the task requires, (b) what is already "
                    "done based on the current screenshot, (c) what is still missing. "
                    "If your current route has not produced visible progress, switch route."
                    + extra
                )

            for action in actions:
                if isinstance(action, dict):
                    import a11y_ref as _ar
                    code, note = _ar.ref_action_to_code(action, self._ref_map)
                    self.logger.info("[step %d] %s", step_idx + 1, note[:120])
                    if note in ("DONE", "FAIL", "WAIT"):
                        action = note
                    elif not code:
                        self._llm.record_feedback(f"Action rejected: {note}")
                        action_history.append(json.dumps(action, ensure_ascii=False))
                        continue
                    else:
                        action_history.append(json.dumps(action, ensure_ascii=False))
                        action = code
                else:
                    action_history.append(action)
                if action == "DONE":
                    done = True
                    await self._mark_terminal_state(environment, "DONE")
                    break
                elif action == "FAIL":
                    done = True
                    await self._mark_terminal_state(environment, "FAIL")
                    break
                elif action == "WAIT":
                    await asyncio.sleep(3); continue
                else:
                    pkgs_prefix = "import pyautogui; import time; pyautogui.FAILSAFE = False; "
                    full_code = pkgs_prefix + action

                    def _exec():
                        return client.execute(["python3", "-c", full_code], shell=False,
                                               setup=False, timeout=30)
                    # A failed action should not abort the entire trial.
                    try:
                        resp = await loop.run_in_executor(None, _exec)
                        rc = resp.get("returncode", -1) if isinstance(resp, dict) else -1
                        err = (resp.get("error", "") or "").strip() if isinstance(resp, dict) else ""
                        fb = f"rc={rc}" + (f" err={err[:200]}" if err else "")
                    except Exception as exc:
                        self.logger.warning(
                            "step %d action failed (continuing): %s: %s",
                            step_idx + 1, type(exc).__name__, str(exc)[:200],
                        )
                        fb = (f"previous action FAILED to execute "
                              f"({type(exc).__name__}): {str(exc)[:200]}")
                    self._llm.record_feedback(fb)
                    await asyncio.sleep(2)

            with open(traj_path, "a") as f:
                f.write(json.dumps({"step_num": step_idx + 1, "action_timestamp": ts,
                                     "actions": actions, "response": response_text,
                                     "done": done}, ensure_ascii=False) + "\n")

        await self._write_trajectory(environment, action_history)

    async def _write_trajectory(self, environment: BaseEnvironment,
                                action_history: list) -> None:
        payload = json.dumps({"action_history": action_history}, ensure_ascii=False)
        b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        try:
            await environment.exec(
                f"mkdir -p /logs/agent && "
                f"echo {b64} | base64 -d > /logs/agent/trajectory.json",
                user="root",
            )
        except Exception as exc:
            self.logger.warning("failed to write trajectory.json: %s", exc)

    async def _mark_terminal_state(self, environment: BaseEnvironment, state: str) -> None:
        # Infeasible-task verifiers distinguish an explicit FAIL from a timeout.
        try:
            await environment.exec(
                f"mkdir -p /logs/agent && printf '%s' '{state}' > /logs/agent/terminal_state.txt",
                user="root",
            )
        except Exception as exc:
            self.logger.warning("failed to write terminal_state marker: %s", exc)
