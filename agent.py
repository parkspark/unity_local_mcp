"""Ollama tool-call 에이전트 루프."""

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
- After unity_play_mode or unity_refresh_assets, Unity reloads its domain: if the NEXT call fails with a connection error, retry it once before reporting failure.
- Never fabricate tool results. If the Unity bridge is unreachable, say so and tell the user to check the Unity Editor.
- unity_screenshot saves a PNG and returns its path. You cannot see images — report the path to the user and ask them to look at it.
- Prefer few, targeted tool calls. Do not dump the full hierarchy unless the user asks for it.
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

        if not calls and _LEAKED_TOOLCALL.search(content):
            calls = _salvage_tool_calls(content)
            if calls:
                # 다음 턴에 모델이 자기 누수 텍스트를 따라하지 않게 히스토리에선 제거
                content = _MARKUP.sub("", content).strip()
                self.on_warn(f"텍스트로 샌 tool-call {len(calls)}건을 복구해 실행합니다.")
            else:
                self.on_warn(
                    "tool-call이 텍스트로 새어 나왔지만 복구하지 못했습니다. "
                    "Ollama 업데이트 또는 /reset을 시도하세요."
                )
        return content, calls

    async def run_turn(self, user_text: str):
        self.history.append({"role": "user", "content": user_text})
        self._trim_history()

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

        # 반복 한도 도달: 툴 없이 요약만 받는다
        self.history.append(
            {
                "role": "user",
                "content": "툴 호출 한도에 도달했습니다. 지금까지의 진행 상황을 요약하고 멈추세요.",
            }
        )
        content, _ = await self._chat(use_tools=False)
        self.history.append({"role": "assistant", "content": content})
