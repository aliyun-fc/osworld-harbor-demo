"""
llm_agent.py - 用 LLM (Qwen) 看截图操作桌面的 agent。

对齐上游 OSWorld PromptAgent 的核心流程:
  截图 [+ a11y_tree] → LLM (system prompt + 历史轨迹) → 解析 pyautogui 代码 → 执行

支持两种 observation_type:
  - "screenshot": 纯截图 (vision-only, 模型靠看图猜坐标)
  - "screenshot_a11y_tree": 截图 + 可访问性树 (默认, 含每个控件的精确坐标)

a11y_tree 来自 OSWorld Flask /accessibility 接口 (pyatspi 拉的 GNOME AT-SPI 树).
linearize 逻辑移植自上游 mm_agents/agent.py + accessibility_tree_wrap/heuristic_retrieve.py.
"""

from __future__ import annotations

import base64
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

logger = logging.getLogger("osworld.llm_agent")

# 上游 OSWorld 用的 namespace (Ubuntu/GNOME)
_STATE_NS = "https://accessibility.ubuntu.example.org/ns/state"
_COMPONENT_NS = "https://accessibility.ubuntu.example.org/ns/component"
_ATTR_NS = "https://accessibility.ubuntu.example.org/ns/attributes"
_VALUE_NS = "https://accessibility.ubuntu.example.org/ns/value"


# ---------- system prompts (对齐上游 prompts.py) ----------

SYS_PROMPT_SCREENSHOT = """
You are an agent which follow my instruction and perform desktop computer tasks as instructed.
You have good knowledge of computer and good internet connection and assume your code will run on a computer for controlling the mouse and keyboard.
For each step, you will get an observation of an image, which is the screenshot of the computer screen and you will predict the action of the computer based on the image.

You are required to use `pyautogui` to perform the action grounded to the observation, but DONOT use the `pyautogui.locateCenterOnScreen` function to locate the element you want to operate with since we have no image of the element you want to operate with. DONOT USE `pyautogui.screenshot()` to make screenshot.
Return one line or multiple lines of python code to perform the action each time, be time efficient. When predicting multiple lines of code, make some small sleep like `time.sleep(0.5);` interval so that the machine could take; Each time you need to predict a complete code, no variables or function can be shared from history
You need to to specify the coordinates of by yourself based on your observation of current observation, but you should be careful to ensure that the coordinates are correct.
You ONLY need to return the code inside a code block, like this:
```python
# your code here
```
The screen resolution is 1920x1080. All coordinates you output must be in this
full-resolution pixel space (x in 0..1919, y in 0..1079).

Specially, it is also allowed to return the following special code:
When you think you have to wait for some time, return ```WAIT```;
When you think the task can not be done, return ```FAIL```. Only use ```FAIL``` when the
target genuinely does not exist or the system cannot support it at all. Being merely
difficult, slow or requiring many steps is NOT a reason to FAIL — keep working instead.
When you think the task is done, return ```DONE```. BEFORE returning ```DONE```, verify on
the CURRENT screenshot that every requirement of the task is actually satisfied (file saved,
dialog closed, value really changed, etc.). If you cannot see the evidence on screen, do NOT
return ```DONE``` — take one more action to surface that evidence first.

My computer's password is '{CLIENT_PASSWORD}', feel free to use it when you need sudo rights.
First give the current screenshot and previous things we did a short reflection, then RETURN ME THE CODE OR SPECIAL CODE I ASKED FOR. NEVER EVER RETURN ME ANYTHING ELSE.
""".strip()

SYS_PROMPT_SCREENSHOT_A11Y = """
You are an agent which follow my instruction and perform desktop computer tasks as instructed.
You have good knowledge of computer and good internet connection and assume your code will run on a computer for controlling the mouse and keyboard.
For each step, you will get an observation of an image and the accessibility tree of the screen, and you will predict the action of the computer based on the image and accessibility tree.

You are required to use `pyautogui` to perform the action grounded to the observation, but DONOT use the `pyautogui.locateCenterOnScreen` function to locate the element you want to operate with since we have no image of the element you want to operate with. DONOT USE `pyautogui.screenshot()` to make screenshot.
Return one line or multiple lines of python code to perform the action each time, be time efficient. When predicting multiple lines of code, make some small sleep like `time.sleep(0.5);` interval so that the machine could take; Each time you need to predict a complete code, no variables or function can be shared from history
You need to to specify the coordinates of by yourself based on your observation of current observation, but you should be careful to ensure that the coordinates are correct.
You ONLY need to return the code inside a code block, like this:
```python
# your code here
```
The screen resolution is 1920x1080. All coordinates you output must be in this
full-resolution pixel space (x in 0..1919, y in 0..1079).

Specially, it is also allowed to return the following special code:
When you think you have to wait for some time, return ```WAIT```;
When you think the task can not be done, return ```FAIL```. Only use ```FAIL``` when the
target genuinely does not exist or the system cannot support it at all. Being merely
difficult, slow or requiring many steps is NOT a reason to FAIL — keep working instead.
When you think the task is done, return ```DONE```. BEFORE returning ```DONE```, verify on
the CURRENT screenshot that every requirement of the task is actually satisfied (file saved,
dialog closed, value really changed, etc.). If you cannot see the evidence on screen, do NOT
return ```DONE``` — take one more action to surface that evidence first.

My computer's password is '{CLIENT_PASSWORD}', feel free to use it when you need sudo rights.
First give the current screenshot and previous things we did a short reflection, then RETURN ME THE CODE OR SPECIAL CODE I ASKED FOR. NEVER EVER RETURN ME ANYTHING ELSE.
""".strip()


# ---------- a11y tree linearize (移植自上游) ----------

def _judge_node(node: ET.Element) -> bool:
    """挑出可交互、可见、有名字/位置的节点 (上游 heuristic_retrieve.judge_node 的简化版)."""
    interactive_tags = {
        "alert", "canvas", "check-box", "combo-box", "entry", "icon",
        "image", "paragraph", "scroll-bar", "section", "slider", "static",
        "table-cell", "terminal", "text",
    }
    interactive_suffixes = (
        "item", "button", "heading", "label", "scrollbar", "searchbox",
        "textbox", "link", "tabelement", "textfield", "textarea", "menu",
    )
    tag = node.tag
    keep = tag.startswith("document") or tag in interactive_tags or any(
        tag.endswith(s) for s in interactive_suffixes
    )
    if not keep:
        return False

    # visible + showing
    showing = node.get(f"{{{_STATE_NS}}}showing", "false") == "true"
    visible = node.get(f"{{{_STATE_NS}}}visible", "false") == "true"
    if not (showing and visible):
        return False

    # 至少一种交互态
    interactive = (
        node.get(f"{{{_STATE_NS}}}enabled", "false") == "true"
        or node.get(f"{{{_STATE_NS}}}editable", "false") == "true"
        or node.get(f"{{{_STATE_NS}}}expandable", "false") == "true"
        or node.get(f"{{{_STATE_NS}}}checkable", "false") == "true"
    )
    if not interactive:
        return False

    # 有 name 或 text
    has_label = bool(node.get("name") or (node.text and node.text.strip()))
    if not has_label:
        return False

    # 有合法坐标和大小
    coord_str = node.get(f"{{{_COMPONENT_NS}}}screencoord", "(-1, -1)")
    size_str = node.get(f"{{{_COMPONENT_NS}}}size", "(-1, -1)")
    try:
        coords = tuple(int(x) for x in coord_str.strip("()").split(","))
        sizes = tuple(int(x) for x in size_str.strip("()").split(","))
    except Exception:
        return False
    if coords[0] < 0 or coords[1] < 0 or sizes[0] <= 0 or sizes[1] <= 0:
        return False

    return True


def linearize_accessibility_tree(at_xml: str, max_chars: int = 30000) -> str:
    """把 a11y XML 压成紧凑的 tab-separated 表 (上游 linearize_accessibility_tree).

    每行: tag\\tname\\ttext\\tposition\\tsize
    """
    if not at_xml:
        return ""
    try:
        root = ET.fromstring(at_xml)
    except ET.ParseError as e:
        logger.warning("a11y XML parse failed: %s", e)
        return ""

    lines: List[str] = ["tag\tname\ttext\tposition (top-left x,y)\tsize (w,h)"]
    for node in root.iter():
        if not _judge_node(node):
            continue
        tag = node.tag.split("}", 1)[-1]  # 去 namespace
        name = (node.get("name") or "").replace("\t", " ").replace("\n", " ")
        text = ((node.text or "")[:60]).replace("\t", " ").replace("\n", " ")
        coord = node.get(f"{{{_COMPONENT_NS}}}screencoord", "")
        size = node.get(f"{{{_COMPONENT_NS}}}size", "")
        lines.append(f"{tag}\t{name}\t{text}\t{coord}\t{size}")

    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n[...truncated]"
    return out


# ---------- helpers ----------

def _encode_image(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


def _parse_actions(response: str) -> List[str]:
    response = "\n".join(
        line.strip() for line in response.split(";") if line.strip()
    )
    if response.strip() in ("WAIT", "DONE", "FAIL"):
        return [response.strip()]

    pattern = r"```(?:\w+\s+)?(.*?)```"
    matches = re.findall(pattern, response, re.DOTALL)

    codes: List[str] = []
    for match in matches:
        match = match.strip()
        if match in ("WAIT", "DONE", "FAIL"):
            codes.append(match)
        elif match.split("\n")[-1] in ("WAIT", "DONE", "FAIL"):
            if len(match.split("\n")) > 1:
                codes.append("\n".join(match.split("\n")[:-1]))
            codes.append(match.split("\n")[-1])
        else:
            codes.append(match)
    return codes


# ---------- main agent ----------

class LLMAgent:
    def __init__(
        self,
        model: str = "qwen3.7-plus",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_tokens: int = 1500,
        temperature: float = 0.5,   # 回退: 1.0 未经验证, 40.9% 基线用的 0.5
        max_trajectory_length: int = 3,
        client_password: str = "password",
        observation_type: str = "screenshot",
        enable_thinking: Optional[bool] = None,
    ):
        if observation_type not in ("screenshot", "screenshot_a11y_tree", "a11y_ref"):
            raise ValueError(f"unsupported observation_type: {observation_type}")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_trajectory_length = max_trajectory_length
        self.observation_type = observation_type
        self.enable_thinking = enable_thinking

        # a11y_ref: 把 a11y 树当"执行接口"用 —— 模型只挑元素编号, 不再猜坐标
        # (实测 Calc 场景有 42 个带精确 bounds 的元素, 模型却一直自己猜像素坐标)
        if observation_type == "a11y_ref":
            from a11y_ref import SYS_PROMPT_A11Y_REF
            sys_tpl = SYS_PROMPT_A11Y_REF
        elif observation_type == "screenshot":
            sys_tpl = SYS_PROMPT_SCREENSHOT
        else:
            sys_tpl = SYS_PROMPT_SCREENSHOT_A11Y
        # 用 replace 而非 format: a11y_ref 的 prompt 里含大量 {"action": ...} json
        # 示例, str.format 会把它们当占位符而抛 KeyError。
        self.system_prompt = sys_tpl.replace("{CLIENT_PASSWORD}", client_password)

        self.client = OpenAI(api_key=api_key, base_url=base_url)

        # 历史轨迹: 每条 obs 是 dict {screenshot: b64, a11y: str|None}
        self.observations: List[Dict[str, Any]] = []
        self.actions: List[str] = []
        self.thoughts: List[str] = []
        # feedbacks[i]: 进入 observations[i] 这一帧之前, 上一步动作的执行结果 (rc/err),
        # 用于让模型看到"上一步报错了", 避免死循环重复同一段坏代码。
        self.feedbacks: List[Optional[str]] = []
        self._pending_feedback: Optional[str] = None

    def reset(self) -> None:
        self.observations.clear()
        self.actions.clear()
        self.thoughts.clear()
        self.feedbacks.clear()
        self._pending_feedback = None

    def soft_reset(self, note: Optional[str] = None) -> None:
        """Clear the trajectory while retaining task context for replanning."""
        self.observations.clear()
        self.actions.clear()
        self.thoughts.clear()
        self.feedbacks.clear()
        self._pending_feedback = note

    def record_feedback(self, text: Optional[str]) -> None:
        """调用方在执行完本步动作后, 把执行结果 (rc/err) 回传给 agent,
        会在下一次 predict 时作为 "上一步动作结果" 注入 prompt。"""
        self._pending_feedback = text

    def predict(
        self,
        instruction: str,
        screenshot_bytes: bytes,
        a11y_tree: Optional[str] = None,
    ) -> Tuple[str, Optional[List[str]]]:
        base64_img = _encode_image(screenshot_bytes)
        a11y_text = (
            linearize_accessibility_tree(a11y_tree)
            if (self.observation_type == "screenshot_a11y_tree" and a11y_tree)
            else None
        )

        messages = self._build_messages(instruction, base64_img, a11y_text)

        try:
            response_text = self._call_llm(messages)
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            response_text = ""

        logger.info("LLM response: %s", response_text[:500])

        try:
            if self.observation_type == "a11y_ref":
                from a11y_ref import parse_ref_action
                act = parse_ref_action(response_text)
                actions = [act] if act else None
            else:
                actions = _parse_actions(response_text)
            self.thoughts.append(response_text)
        except (ValueError, IndexError) as e:
            logger.error("Failed to parse actions: %s", e)
            actions = None
            self.thoughts.append("")

        self.observations.append({"screenshot": base64_img, "a11y": a11y_text})
        self.feedbacks.append(self._pending_feedback)
        self._pending_feedback = None
        self.actions.append(actions[0] if actions else "")

        return response_text, actions

    def _build_messages(
        self,
        instruction: str,
        current_screenshot_b64: str,
        current_a11y: Optional[str],
    ) -> List[Dict[str, Any]]:
        system_text = (
            self.system_prompt
            + f"\nYou are asked to complete the following task: {instruction}"
        )
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_text}
        ]

        recent_obs = self.observations[-self.max_trajectory_length:]
        recent_thought = self.thoughts[-self.max_trajectory_length:]
        recent_fb = self.feedbacks[-self.max_trajectory_length:]

        for obs, thought, fb in zip(recent_obs, recent_thought, recent_fb):
            messages.append(self._user_msg(obs["screenshot"], obs.get("a11y"), fb))
            messages.append({
                "role": "assistant",
                "content": thought.strip() if thought else "No valid action",
            })

        messages.append(
            self._user_msg(current_screenshot_b64, current_a11y, self._pending_feedback)
        )
        return messages

    def _user_msg(
        self,
        screenshot_b64: str,
        a11y: Optional[str],
        feedback: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.observation_type == "screenshot_a11y_tree" and a11y:
            text = (
                "Given the screenshot and info from accessibility tree as below:\n"
                f"{a11y}\n"
                "What's the next step that you will do to help with the task?"
            )
        else:
            text = "Given the screenshot as below. What's the next step that you will do to help with the task?"
        if feedback:
            text = (
                "Result of your previous action (fix it if it failed, do NOT repeat "
                f"the same failing action):\n{feedback}\n\n" + text
            )
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
                },
            ],
        }

    def _call_llm(self, messages: List[Dict[str, Any]]) -> str:
        kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if self.enable_thinking is not None:
            kwargs["extra_body"] = {"enable_thinking": self.enable_thinking}
        resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""
