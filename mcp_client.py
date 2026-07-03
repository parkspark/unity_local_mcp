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
import local_tools


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


def _bridge_port() -> str | None:
    """브리지가 Library/McpBridgePort.txt에 기록한 실제 포트.

    도메인 리로드로 고아가 된 소켓이 8722를 점유하면 브리지는 다음 포트로
    옮겨 바인드하므로, 파일이 있으면 그 포트를 서버에 넘겨야 한다.
    """
    if not config.UNITY_PROJECT_DIR:
        return None
    path = os.path.join(config.UNITY_PROJECT_DIR, "Library", "McpBridgePort.txt")
    try:
        with open(path, encoding="utf-8") as f:
            port = f.read().strip()
        return port if port.isdigit() else None
    except OSError:
        return None


def _server_params() -> StdioServerParameters:
    """MCP 서버 실행 방법. venv python을 직접 쓰면 uv 중간 계층이 없어
    종료 시 자식 프로세스가 고아로 남지 않는다. venv가 없으면 uv로 폴백."""
    env = None
    port = _bridge_port()
    if port and "UNITY_MCP_PORT" not in os.environ:
        env = {**os.environ, "UNITY_MCP_PORT": port}
    venv_python = os.path.join(config.UNITY_MCP_DIR, ".venv", "Scripts", "python.exe")
    if os.path.exists(venv_python):
        return StdioServerParameters(
            command=venv_python, args=["server.py"], cwd=config.UNITY_MCP_DIR, env=env
        )
    return StdioServerParameters(
        command=_find_uv(),
        args=["--directory", config.UNITY_MCP_DIR, "run", "server.py"],
        env=env,
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
        self._port = _bridge_port() or "8722"  # 서버 프로세스에 넘겨준 포트
        params = _server_params()
        # 서버 stderr("Processing request..." 로그)가 채팅 화면에 섞이지 않게 파일로
        errlog = self._stack.enter_context(
            open(os.path.join(os.path.dirname(__file__), "mcp_server.log"), "w", encoding="utf-8")
        )
        read, write = await self._stack.enter_async_context(stdio_client(params, errlog=errlog))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        self.tools = (await self.session.list_tools()).tools
        self.ollama_tools = [_to_ollama(t) for t in self.tools] + local_tools.SCHEMAS
        self._schemas = {t.name: t.inputSchema for t in self.tools}
        self._project_dir: str | None = config.UNITY_PROJECT_DIR or None
        self.last_raw_result = ""  # /last 명령용, 절단 전 원본
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def _reconnect_if_port_changed(self) -> bool:
        """브리지가 도메인 리로드로 다른 포트에 바인드했으면 서버를 새 포트로 재기동.

        server.py는 시작 시 UNITY_MCP_PORT를 한 번만 읽으므로, 포트가 바뀌면
        stdio 서버 자체를 다시 띄워야 한다. (진입한 것과 같은 태스크에서만 호출할 것)
        """
        new_port = _bridge_port()
        if not new_port or new_port == self._port:
            return False
        await self._stack.aclose()
        await self.__aenter__()
        return True

    @property
    def names(self) -> list[str]:
        return [t["function"]["name"] for t in self.ollama_tools]

    async def _resolve_project_dir(self) -> str | None:
        """Unity 프로젝트 경로. 최초 사용 시 unity_ping의 projectPath에서 발견해 캐시."""
        if self._project_dir:
            return self._project_dir
        text = await self._call_once("unity_ping", {})
        try:
            path = json.loads(text)["result"]["projectPath"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        if path and os.path.isdir(path):
            self._project_dir = path
            return path
        return None

    async def _wait_for_compile(self, refresh_text: str) -> str:
        """refresh_assets 성공 후 컴파일이 끝날 때까지 호스트가 대기.

        30B 모델에게 타이밍 재시도를 맡기면 실패한다 — 결정적으로 처리한다.
        Unity는 백그라운드에 있으면 컴파일을 미루므로 잠깐 포커스를 줬다가 되돌린다.
        """
        try:
            if json.loads(refresh_text).get("status") != "ok":
                return refresh_text
        except (json.JSONDecodeError, TypeError):
            return refresh_text

        prev_focus = 0
        if config.FOCUS_UNITY_ON_COMPILE:
            import winfocus
            prev_focus = winfocus.focus_unity()
        await asyncio.sleep(1.5)  # 컴파일은 refresh 반환 직후에 시작될 수 있다
        for _ in range(45):  # 최대 ~90초 (첫 스크립트 임포트 + 도메인 리로드는 오래 걸린다)
            state = await self._call_once("unity_get_state", {})
            try:
                r = json.loads(state)["result"]
            except (json.JSONDecodeError, KeyError, TypeError):
                # 도메인 리로드로 브리지 다운 — 포트가 바뀌었으면 재접속 후 계속 대기
                await self._reconnect_if_port_changed()
                await asyncio.sleep(2)
                continue
            if not r.get("isCompiling") and not r.get("isUpdating"):
                # 완료했을 때만 포커스를 되돌린다 — 타임아웃 시 되돌리면 백그라운드의
                # Unity가 리로드를 마저 끝내지 못한다
                if prev_focus:
                    import winfocus
                    winfocus.restore_focus(prev_focus)
                return refresh_text + (
                    "\n[호스트가 컴파일 완료까지 대기했습니다. "
                    'unity_read_console types="error"로 컴파일 에러를 확인하세요.]'
                )
            await asyncio.sleep(2)
        return refresh_text + (
            "\n[90초가 지나도 아직 컴파일 중입니다. Unity 에디터에 확인 대화상자가 떠 있지 "
            "않은지 확인하세요. unity_get_state로 상태를 이어서 확인할 수 있습니다.]"
        )

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
        if name in local_tools.NAMES:
            project_dir = await self._resolve_project_dir()
            if project_dir is None:
                text = json.dumps({
                    "status": "error",
                    "error": (
                        "Unity 프로젝트 경로를 알 수 없습니다. Unity Editor가 열려 있는지 "
                        "확인하거나 UNITY_PROJECT_DIR 환경변수를 설정하세요."
                    ),
                }, ensure_ascii=False)
            else:
                text = local_tools.call(name, args, project_dir)
            self.last_raw_result = text
            return _truncate(text)

        if name not in self._schemas:
            return (
                f"Error: unknown tool '{name}'. "
                f"Available: {', '.join(sorted(self.names))}"
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
            # 리로드로 브리지가 다른 포트로 옮겨갔을 수 있다
            if not await self._reconnect_if_port_changed():
                await asyncio.sleep(4)
                await self._reconnect_if_port_changed()
            text = await self._call_once(name, args)
        if name == "unity_refresh_assets":
            text = await self._wait_for_compile(text)
        self.last_raw_result = text
        return _truncate(text)
