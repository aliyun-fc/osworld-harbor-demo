"""Turn the accessibility tree into numbered, directly actionable elements."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

# Reuse the server-specific namespaces to avoid filtering every node.
from llm_agent import _STATE_NS, _COMPONENT_NS  # noqa: E402

# Keep this allowlist aligned with llm_agent._judge_node.
_INTERACTIVE_TAGS = {
    "alert", "canvas", "check-box", "combo-box", "entry", "icon",
    "image", "paragraph", "scroll-bar", "section", "slider", "static",
    "table-cell", "terminal", "text",
}
_INTERACTIVE_SUFFIXES = (
    "item", "button", "heading", "label", "scrollbar", "searchbox",
    "textbox", "link", "tabelement", "textfield", "textarea", "menu",
)
# 这些 tag 是纯展示, 不作为可点目标(仍会出现在文本里供理解, 但不给 ref)
_NON_CLICKABLE = {"label", "static", "paragraph", "heading"}


def _attr(node: ET.Element, ns: str, key: str, default: str = "") -> str:
    return node.get(f"{{{ns}}}{key}", default)


def _bounds(node: ET.Element) -> Optional[Tuple[int, int, int, int]]:
    try:
        cx, cy = (int(v) for v in _attr(node, _COMPONENT_NS, "screencoord", "").strip("()").split(","))
        w, h = (int(v) for v in _attr(node, _COMPONENT_NS, "size", "").strip("()").split(","))
    except Exception:
        return None
    if cx < 0 or cy < 0 or w <= 0 or h <= 0:
        return None
    return cx, cy, w, h


def _keep(node: ET.Element) -> bool:
    tag = node.tag
    if not (tag.startswith("document") or tag in _INTERACTIVE_TAGS
            or any(tag.endswith(s) for s in _INTERACTIVE_SUFFIXES)):
        return False
    if not (_attr(node, _STATE_NS, "showing") == "true"
            and _attr(node, _STATE_NS, "visible") == "true"):
        return False
    if not any(_attr(node, _STATE_NS, k) == "true"
               for k in ("enabled", "editable", "expandable", "checkable")):
        return False
    if not (node.get("name") or (node.text and node.text.strip())):
        return False
    return _bounds(node) is not None


def build_ref_view(at_xml: str, screen_w: int = 1920, screen_h: int = 1080,
                   max_elems: int = 150) -> Tuple[str, Dict[int, Dict[str, Any]]]:
    """把 a11y XML 变成带编号的元素清单 + ref->元素信息映射。

    返回 (给模型看的文本, {ref: {"bounds":(x,y,w,h), "tag":..., "name":...}})
    只有可点元素拿到 ref; 纯 label/static 以 "(text)" 形式出现供理解但无 ref。
    """
    if not at_xml:
        return "", {}
    try:
        root = ET.fromstring(at_xml)
    except ET.ParseError:
        return "", {}

    lines: List[str] = []
    ref_map: Dict[int, Dict[str, Any]] = {}
    ref = 0
    for node in root.iter():
        if not _keep(node):
            continue
        tag = node.tag.split("}", 1)[-1]
        name = (node.get("name") or "").replace("\n", " ").strip()
        text = ((node.text or "").strip()[:60]).replace("\n", " ")
        x, y, w, h = _bounds(node)          # type: ignore[misc]
        # 屏幕外的丢掉
        if x >= screen_w or y >= screen_h or x + w <= 0 or y + h <= 0:
            continue
        label = name or text
        if tag in _NON_CLICKABLE:
            lines.append(f'     (text) {tag} "{label}" at ({x},{y})')
            continue
        ref += 1
        if ref > max_elems:
            break
        states = []
        if _attr(node, _STATE_NS, "focused") == "true":
            states.append("focused")
        if _attr(node, _STATE_NS, "checked") == "true":
            states.append("checked")
        if _attr(node, _STATE_NS, "editable") == "true":
            states.append("editable")
        st = f" [{','.join(states)}]" if states else ""
        extra = f' text="{text}"' if text and text != label else ""
        lines.append(f'[{ref}] {tag} "{label}"{extra} bounds=({x},{y},{w},{h}){st}')
        ref_map[ref] = {"bounds": (x, y, w, h), "tag": tag, "name": label}
    return "\n".join(lines), ref_map


SYS_PROMPT_A11Y_REF = """
You are an agent performing desktop computer tasks on Ubuntu (screen 1920x1080).

Each step you receive:
  1. a screenshot of the screen
  2. a numbered list of the CURRENTLY VISIBLE interactive UI elements, e.g.
       [12] push-button "Save" bounds=(1730,1008,92,34)
       [37] text "Subject" bounds=(220,145,810,38) [editable]
     Lines starting with "(text)" are labels for context only - they have no number.

You do NOT need to guess pixel coordinates. Prefer referring to an element by its
number: the executor clicks the exact centre of its bounding box for you.

Reply with ONE json object per step, inside a ```json code block:

  click an element        {"action": "click", "ref": 12}
  double click            {"action": "double_click", "ref": 12}
  right click             {"action": "right_click", "ref": 12}
  type into an element    {"action": "type", "ref": 37, "text": "hello"}
                          (the element is focused and cleared first)
  type at current focus   {"action": "type", "text": "hello"}
  press keys              {"action": "key", "keys": ["ctrl", "s"]}
  scroll                  {"action": "scroll", "ref": 12, "amount": -3}
  wait for the UI         {"action": "wait"}
  task finished           {"action": "done"}
  task truly impossible   {"action": "fail"}

If (and only if) the thing you need is NOT in the element list - e.g. clicking a
spot inside a canvas/spreadsheet cell, dragging, or running a shell command - use
raw python as an escape hatch:

  {"action": "code", "code": "pyautogui.click(640, 400)\\ntime.sleep(0.5)"}

Rules:
  · Refer to elements by number whenever the target IS in the list. Numbers change
    every step - always use the numbers from the CURRENT list, never an old one.
  · One json object per reply. Think briefly first, then output the json block.
  · Before {"action": "done"}, make sure the screenshot shows the task really finished.
  · Use {"action": "fail"} only if the goal is genuinely impossible, not merely hard.

My computer's password is '{CLIENT_PASSWORD}', use it when sudo is needed.
""".strip()


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_ref_action(response: str) -> Optional[Dict[str, Any]]:
    """从模型回复里取出 json 动作; 失败返回 None。"""
    if not response:
        return None
    candidates = _JSON_BLOCK.findall(response)
    if not candidates:
        # 容错: 没套 code block 的裸 json
        m = re.search(r'\{[^{}]*"action"\s*:\s*"[^"]+"[^{}]*\}', response, re.DOTALL)
        candidates = [m.group(0)] if m else []
    for c in reversed(candidates):          # 取最后一个(通常是结论)
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("action"):
            return obj
    return None


def _center(b: Tuple[int, int, int, int]) -> Tuple[int, int]:
    x, y, w, h = b
    return x + w // 2, y + h // 2


def ref_action_to_code(action: Dict[str, Any],
                       ref_map: Dict[int, Dict[str, Any]]) -> Tuple[str, str]:
    """把 ref 动作翻译成 pyautogui 代码。

    返回 (code, note)。code 为空串表示是 done/fail/wait 之类的控制动作,
    note 用于回传给模型的反馈(例如 ref 失效)。
    """
    kind = str(action.get("action", "")).lower()

    if kind in ("done", "fail", "wait"):
        return "", kind.upper()

    if kind == "code":
        return str(action.get("code") or ""), "raw code"

    if kind == "key":
        keys = action.get("keys") or []
        if isinstance(keys, str):
            keys = [keys]
        if not keys:
            return "", "key action without 'keys'"
        args = ", ".join(repr(str(k)) for k in keys)
        return f"pyautogui.hotkey({args})\ntime.sleep(0.5)", f"hotkey {'+'.join(map(str, keys))}"

    if kind == "type" and action.get("ref") is None:
        text = str(action.get("text", ""))
        return (f"pyautogui.typewrite({text!r}, interval=0.02)\ntime.sleep(0.3)",
                "type at current focus")

    # 以下动作都需要 ref
    ref = action.get("ref")
    try:
        ref = int(ref)
    except (TypeError, ValueError):
        return "", f"invalid ref {action.get('ref')!r}"
    if ref not in ref_map:
        return "", (f"ref {ref} does not exist in the current element list "
                    f"(valid: 1..{len(ref_map)}). Re-read the list and pick again.")

    el = ref_map[ref]
    cx, cy = _center(el["bounds"])
    desc = f'{el["tag"]} "{el["name"]}" @({cx},{cy})'

    if kind == "click":
        return f"pyautogui.click({cx}, {cy})\ntime.sleep(0.5)", f"click {desc}"
    if kind == "double_click":
        return f"pyautogui.doubleClick({cx}, {cy})\ntime.sleep(0.8)", f"double click {desc}"
    if kind == "right_click":
        return f"pyautogui.rightClick({cx}, {cy})\ntime.sleep(0.5)", f"right click {desc}"
    if kind == "type":
        text = str(action.get("text", ""))
        return (f"pyautogui.click({cx}, {cy})\ntime.sleep(0.3)\n"
                f"pyautogui.hotkey('ctrl', 'a')\ntime.sleep(0.1)\n"
                f"pyautogui.typewrite({text!r}, interval=0.02)\ntime.sleep(0.3)",
                f"type into {desc}")
    if kind == "scroll":
        amount = int(action.get("amount", -3))
        return (f"pyautogui.moveTo({cx}, {cy})\ntime.sleep(0.2)\n"
                f"pyautogui.scroll({amount})\ntime.sleep(0.5)",
                f"scroll {amount} at {desc}")
    return "", f"unknown action {kind!r}"
