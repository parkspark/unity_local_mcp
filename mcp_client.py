"""기존 unity_mcp/server.py에 stdio MCP 클라이언트로 접속하는 래퍼.

도구 스키마는 list_tools()로 자동 발견하고 Ollama 함수 형식으로 변환한다.
서버·Unity 브리지는 수정하지 않는다.
"""

import asyncio
import json
import os
import shutil
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import config


def _find_uv() -> str:
    # Windows에서 stdio 서브프로세스는 전체 경로가 필요하다
    uv = shutil.which("uv")
    if uv:
        return uv
    for cand in (
        os.path.expanduser(r"~\.local\bin\uv.exe"),
        os.path.expanduser(r"~\.cargo\bin\uv.exe"),
    ):
        if os.path.exists(cand):
            return cand
    raise RuntimeError("uv.exe를 찾을 수 없습니다. https://docs.astral.sh/uv/ 참고")


def _server_params() -> StdioServerParameters:
    """MCP 서버 실행 방법. venv python을 직접 쓰면 uv 중간 계층이 없어
    종료 시 자식 프로세스가 고아로 남지 않는다. venv가 없으면 uv로 폴백."""
    venv_python = os.path.join(config.UNITY_MCP_DIR, ".venv", "Scripts", "python.exe")
    if os.path.exists(venv_python):
        return StdioServerParameters(
            command=venv_python, args=["server.py"], cwd=config.UNITY_MCP_DIR
        )
    return StdioServerParameters(
        command=_find_uv(),
        args=["--directory", config.UNITY_MCP_DIR, "run", "server.py"],
    )


def _simplify(node):
    """FastMCP(pydantic)가 내보내는 JSON Schema를 소형 모델 친화적으로 정리.

    Optional[X] → anyOf:[X, null] 패턴을 단일 타입으로 접고, title 키를 제거한다.
    """
    if isinstance(node, dict):
        node.pop("title", None)
        if "anyOf" in node:
            non_null = [s for s in node["anyOf"] if s.get("type") != "null"]
            if len(non_null) == 1:
                node.pop("anyOf")
                node.update(non_null[0])
        for v in node.values():
            _simplify(v)
    elif isinstance(node, list):
        for v in node:
            _simplify(v)
    return node


def _to_ollama(tool) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": _simplify(dict(tool.inputSchema)),
        },
    }


# 재시도해도 부작용이 없는 읽기 전용 도구
_READONLY = {
    "unity_ping", "unity_get_state", "unity_get_hierarchy", "unity_get_gameobject",
    "unity_read_console", "unity_list_assets", "unity_screenshot",
}


def _truncate(text: str) -> str:
    limit = config.TRUNCATE_CHARS
    if len(text) <= limit:
        return text
    return text[:limit] + (
        f"\n[... {len(text) - limit}자 잘림. 결과가 너무 큽니다. "
        "unity_get_hierarchy는 max_depth를 줄이거나 unity_get_gameobject로 개별 조회하세요.]"
    )


class UnityTools:
    """MCP 세션 수명 관리 + 도구 호출. 반드시 같은 asyncio 태스크에서 진입/종료할 것."""

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        params = _server_params()
        # 서버 stderr("Processing request..." 로그)가 채팅 화면에 섞이지 않게 파일로
        errlog = self._stack.enter_context(
            open(os.path.join(os.path.dirname(__file__), "mcp_server.log"), "w", encoding="utf-8")
        )
        read, write = await self._stack.enter_async_context(stdio_client(params, errlog=errlog))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        self.tools = (await self.session.list_tools()).tools
        self.ollama_tools = [_to_ollama(t) for t in self.tools]
        self._schemas = {t.name: t.inputSchema for t in self.tools}
        self.last_raw_result = ""  # /last 명령용, 절단 전 원본
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    @property
    def names(self) -> list[str]:
        return [t.name for t in self.tools]

    def _coerce_args(self, name: str, args: dict) -> dict:
        """스키마상 array/number/bool 인자가 문자열로 오면 json.loads로 보정."""
        props = self._schemas.get(name, {}).get("properties", {})
        fixed = {}
        for key, value in args.items():
            schema_type = props.get(key, {}).get("type")
            if isinstance(value, str) and schema_type in ("array", "number", "integer", "boolean"):
                try:
                    value = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    pass
            fixed[key] = value
        return fixed

    async def _call_once(self, name: str, args: dict) -> str:
        try:
            result = await self.session.call_tool(name, args)
        except Exception as e:
            return f"Tool error: {type(e).__name__}: {e}"
        text = "\n".join(
            c.text for c in result.content if getattr(c, "text", None)
        )
        if result.isError:
            text = f"Tool error: {text}"
        return text

    async def call(self, name: str, args: dict) -> str:
        if name not in self._schemas:
            return (
                f"Error: unknown tool '{name}'. "
                f"Available: {', '.join(sorted(self._schemas))}"
            )
        args = self._coerce_args(name, args)
        text = await self._call_once(name, args)
        # 도메인 리로드(플레이 모드 전환·스크립트 컴파일) 중엔 브리지가 몇 초 다운된다.
        # "Cannot reach"는 명령이 Unity에 도달하지 못한 경우라 재시도해도 안전 → 1회만.
        # "Empty response"는 명령이 실행됐을 수도 있어 읽기 전용 도구만 재시도한다.
        retryable = "Cannot reach the Unity bridge" in text or (
            "Empty response from Unity bridge" in text and name in _READONLY
        )
        if retryable:
            await asyncio.sleep(4)
            text = await self._call_once(name, args)
        self.last_raw_result = text
        return _truncate(text)
