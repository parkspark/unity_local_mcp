"""Ollama tool-call 에이전트 루프."""

import json
import re

import ollama

import config
from mcp_client import UnityTools

SYSTEM_PROMPT = """\
You are a Unity Editor assistant. You control a live Unity Editor through the provided tools.

Language: 사용자가 한국어로 말하면 한국어로 답한다. Tool names and arguments are always English.

Rules:
- Before modifying anything, check current state first: unity_get_state, or unity_get_hierarchy with a small max_depth (e.g. 3), or unity_get_gameobject for one object.
- Verify after acting: after create/modify/delete, confirm the result with unity_get_gameobject or unity_get_hierarchy. After entering play mode, check unity_read_console for errors.
- `target` arguments are hierarchy paths like "Parent/Child". Use exact paths you saw in the hierarchy. Never guess names.
- position/rotation/scale are JSON arrays of 3 numbers, e.g. [0, 1.5, 0]. rotation is euler degrees.
- Tool arguments are STRICT JSON. Numbers never carry C# suffixes: write 0.9, NOT 0.9f. Vectors are real JSON arrays like [0.9, 0.9, 0.9], never the string "[0.9f, 0.9f, 0.9f]".
- NEVER create similar objects one-by-one with repeated unity_create_gameobject calls. For 3+ similar objects call unity_create_gameobjects ONCE with a list of specs. For large or procedural layouts (grids, boards, 20+ objects — e.g. a 10x20 board) do not create them with tools at all: write a MonoBehaviour that builds them in Awake()/Start() with GameObject.CreatePrimitive or Instantiate in a loop, attach it to one empty GameObject, and let the game generate the layout itself.
- After unity_play_mode or unity_refresh_assets, Unity reloads its domain: if the NEXT call fails with a connection error, retry it once before reporting failure.
- Never fabricate tool results. If the Unity bridge is unreachable, say so and tell the user to check the Unity Editor.
- unity_screenshot saves a PNG and returns its path. You cannot see images — report the path to the user and ask them to look at it.
- Prefer few, targeted tool calls. Do not dump the full hierarchy unless the user asks for it.

Writing C# scripts:
- New behaviour script: unity_write_script → unity_refresh_assets (the host waits for compilation) → unity_read_console types="error" → if no errors, unity_add_component with the class name.
- If there are compile errors, fix the script with unity_write_script and repeat.
- The C# class name MUST match the file name. Before modifying an existing script, read it first with unity_read_script.
- ALWAYS place scripts under Assets/Scripts/. Before creating a new script, check it does not already exist elsewhere (unity_list_assets filter "t:Script"). Two files defining the same class break ALL compilation in the project.
- To remove a stale, duplicate, or conflicting script that breaks compilation, DELETE it with unity_delete_script (do NOT just leave it or try to work around it), then unity_refresh_assets to recompile.
- This project uses the NEW Input System ONLY. NEVER use the legacy UnityEngine.Input API (Input.GetAxis, Input.GetButtonDown, Input.GetKey...) — it throws InvalidOperationException at runtime. Instead `using UnityEngine.InputSystem;` and read `Keyboard.current` / `Mouse.current` / `Gamepad.current`, e.g. `keyboard.aKey.isPressed`, `keyboard.spaceKey.wasPressedThisFrame`. Always null-check `Keyboard.current` first.
- Unity 6 renamed APIs — obsolete names trigger a blocking editor dialog. Use `rb.linearVelocity` (NOT `rb.velocity`) and `Object.FindFirstObjectByType<T>()` (NOT `FindObjectOfType<T>()`).
"""

# qwen3-coder가 <tool_call> 여는 태그를 생략하는 등 포맷을 벗어나면 Ollama 파서가
# 놓치고 텍스트로 샌다. 그런 경우를 직접 파싱해 복구한다.
_LEAKED_TOOLCALL = re.compile(r"<(function|tool_call)[=\s>]")
_FUNC_BLOCK = re.compile(r"<function=([\w\-.]+)>(.*?)</function>", re.S)
_PARAM = re.compile(r"<parameter=([\w\-.]+)>\n?(.*?)\n?</parameter>", re.S)
_MARKUP = re.compile(r"</?(?:tool_call|function|parameter)[^>]*>")


def _salvage_tool_calls(content: str) -> list[tuple[str, dict]]:
    """텍스트로 샌 qwen3-coder 형식 tool-call을 (이름, 인자) 목록으로 복구."""
    calls = []
    for m in _FUNC_BLOCK.finditer(content):
        name = m.group(1)
        args = {k: v for k, v in _PARAM.findall(m.group(2))}
        calls.append((name, args))
    return calls


def _call_key(name: str, args: dict) -> tuple[str, str]:
    return name, json.dumps(args, sort_keys=True, ensure_ascii=False)


def _merge_leaked_calls(content, calls, had_leak):
    """정상 파싱된 tool-call에 텍스트로 샌 것을 병합.

    qwen3-coder가 한 응답에서 일부는 정상 tool-call로, 일부는 <function=...> 텍스트로
    흘리는 '혼합 응답'을 처리한다. 기존엔 정상 호출이 하나라도 있으면 누수분을 통째로
    버렸다. 누수분을 앞쪽에 두는 이유: 모델은 보통 주 동작(예: 삭제)을 먼저 서술하고
    후속(예: refresh)을 정상 호출로 낸다. 중복은 이름+인자로 제거한다.

    반환: (마크업 제거된 content, 병합된 calls, 복구 건수).
    """
    if not had_leak:
        return content, calls, 0
    salvaged = _salvage_tool_calls(content)
    if not salvaged:
        return content, calls, 0
    cleaned = _MARKUP.sub("", content).strip()
    seen = {_call_key(n, a) for n, a in calls}
    new = []
    for n, a in salvaged:
        key = _call_key(n, a)
        if key not in seen:
            new.append((n, a))
            seen.add(key)
    return cleaned, new + calls, len(new)


class Agent:
    def __init__(self, tools: UnityTools, on_text, on_tool, on_warn):
        self.client = ollama.AsyncClient()
        self.tools = tools
        self.model = config.MODEL
        self.history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        # 표시 콜백: on_text(스트리밍 텍스트 조각), on_tool(이름, 인자, 결과), on_warn(경고문)
        self.on_text = on_text
        self.on_tool = on_tool
        self.on_warn = on_warn

    def reset(self):
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]

    def _estimated_tokens(self) -> int:
        chars = sum(len(str(m.get("content") or "")) for m in self.history)
        return int(chars / 3.5)

    def _trim_history(self):
        budget = int(config.NUM_CTX * config.HISTORY_BUDGET_RATIO)
        trimmed = False
        # 시스템 메시지(0번)와 최근 대화는 남기고 오래된 것부터 제거
        while self._estimated_tokens() > budget and len(self.history) > 4:
            del self.history[1]
            trimmed = True
        if trimmed:
            self.history.insert(
                1, {"role": "user", "content": "[이전 대화 일부가 컨텍스트 한도로 잘렸습니다]"}
            )

    async def _chat(self, use_tools: bool = True):
        """1회 모델 호출. (content, [(도구명, 인자)]) 반환. 스트리밍 텍스트는 on_text로 전달."""
        kwargs = dict(
            model=self.model,
            messages=self.history,
            keep_alive=config.KEEP_ALIVE,
            options={"num_ctx": config.NUM_CTX, "temperature": config.TEMPERATURE},
        )
        if use_tools:
            kwargs["tools"] = self.tools.ollama_tools

        content, tool_calls = "", []
        if config.STREAM:
            stream = await self.client.chat(stream=True, **kwargs)
            async for chunk in stream:
                if chunk.message.content:
                    content += chunk.message.content
                    self.on_text(chunk.message.content)
                if chunk.message.tool_calls:
                    tool_calls.extend(chunk.message.tool_calls)
        else:
            resp = await self.client.chat(stream=False, **kwargs)
            content = resp.message.content or ""
            tool_calls = list(resp.message.tool_calls or [])
            if content:
                self.on_text(content)

        calls = [(tc.function.name, dict(tc.function.arguments or {})) for tc in tool_calls]

        had_leak = bool(_LEAKED_TOOLCALL.search(content))
        # 다음 턴에 모델이 자기 누수 텍스트를 따라하지 않게 히스토리에선 마크업 제거
        content, calls, recovered = _merge_leaked_calls(content, calls, had_leak)
        if recovered:
            self.on_warn(f"텍스트로 샌 tool-call {recovered}건을 복구해 실행합니다.")
        elif had_leak and not calls:
            self.on_warn(
                "tool-call이 텍스트로 새어 나왔지만 복구하지 못했습니다. "
                "Ollama 업데이트 또는 /reset을 시도하세요."
            )
        return content, calls

    async def run_turn(self, user_text: str):
        self.history.append({"role": "user", "content": user_text})
        self._trim_history()

        call_counts: dict[str, int] = {}
        nudged = False
        for _ in range(config.MAX_ITERS):
            content, calls = await self._chat()
            self.history.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {"function": {"name": n, "arguments": a}} for n, a in calls
                    ] or None,
                }
            )
            if not calls:
                return  # 최종 답변은 이미 스트리밍으로 출력됨

            for name, args in calls:
                result = await self.tools.call(name, args)
                self.on_tool(name, args, result)
                self.history.append(
                    {"role": "tool", "tool_name": name, "content": result}
                )

            # 같은 툴 반복 호출 감지: 개별 호출을 쌓는 대신 배치/스크립트로 유도
            if config.LOOP_GUARD_THRESHOLD and not nudged:
                for name, _a in calls:
                    call_counts[name] = call_counts.get(name, 0) + 1
                    if call_counts[name] >= config.LOOP_GUARD_THRESHOLD:
                        self.history.append({"role": "user", "content": (
                            "[시스템] 같은 툴을 반복 호출하고 있습니다. 반복적인 오브젝트 생성은 "
                            "unity_create_gameobjects 배치 툴 하나로 처리하거나, 큰 그리드는 "
                            "스크립트의 Awake()/Start()에서 생성하세요."
                        )})
                        self.on_warn(
                            f"{name} 반복 호출 감지 — 배치 툴/스크립트 사용을 권고했습니다."
                        )
                        nudged = True
                        break

        # 반복 한도 도달: 툴 없이 요약만 받는다
        self.history.append(
            {
                "role": "user",
                "content": "툴 호출 한도에 도달했습니다. 지금까지의 진행 상황을 요약하고 멈추세요.",
            }
        )
        content, _ = await self._chat(use_tools=False)
        self.history.append({"role": "assistant", "content": content})
