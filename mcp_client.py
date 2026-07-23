"""기존 unity_mcp/server.py에 stdio MCP 클라이언트로 접속하는 래퍼.

도구 스키마는 list_tools()로 자동 발견하고 Ollama 함수 형식으로 변환한다.
서버·Unity 브리지는 수정하지 않는다.
"""

import asyncio
import json
import os
import re
import shutil
import time
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import config
import local_tools
from audit_logging import ToolAuditLogger


_CS_FLOAT_SUFFIX = re.compile(r"(?<=[\d.])[fF]\b")


def _lenient_json(text: str):
    """C#풍 float 접미사('0.9f')를 허용해 JSON으로 파싱. 실패 시 예외 전파."""
    return json.loads(_CS_FLOAT_SUFFIX.sub("", text))


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
    session = _bridge_session()
    if session and str(session.get("port", "")).isdigit():
        return str(session["port"])
    path = os.path.join(config.UNITY_PROJECT_DIR, "Library", "McpBridgePort.txt")
    try:
        with open(path, encoding="utf-8") as f:
            port = f.read().strip()
        return port if port.isdigit() else None
    except OSError:
        return None


def _bridge_session() -> dict | None:
    """Read the project-scoped bridge identity file when bridge v0.3+ provides it."""
    if not config.UNITY_PROJECT_DIR:
        return None
    path = os.path.join(config.UNITY_PROJECT_DIR, "Library", "McpBridgeSession.json")
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
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

_MUTATIONS = {
    "unity_create_gameobject", "unity_create_gameobjects", "unity_modify_gameobject",
    "unity_delete_gameobject", "unity_add_component", "unity_remove_component",
    "unity_set_component_property", "unity_create_material", "unity_create_scene",
    "unity_open_scene", "unity_save_scene", "unity_refresh_assets", "unity_write_script",
    "unity_delete_script", "unity_install_level_loader", "unity_write_level",
    "unity_execute_menu_item",
}

# 검증은 씬/파일을 고치지 않는다. 플레이 모드 전환과 입력/스크린샷은
# 검증 행위이므로 허용하지만, 모든 편집·파일 쓰기 도구는 스키마에서도 숨기고
# call() 경계에서도 다시 차단한다(누수 tool-call 우회 방지).
VERIFY_ONLY_TOOLS = {
    "unity_ping", "unity_get_state", "unity_get_hierarchy", "unity_get_gameobject",
    "unity_read_console", "unity_list_assets", "unity_screenshot", "unity_play_mode",
    "unity_send_key", "unity_get_input_state", "unity_wait", "unity_read_script",
    "unity_read_level",
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
        self._audit_log: ToolAuditLogger | None = None
        self.audit_log_error: str | None = None
        self.last_audit_log_path: str | None = None
        if config.MCP_AUDIT_LOGS:
            try:
                self._audit_log = ToolAuditLogger(config.MCP_AUDIT_LOG_DIR)
                self.last_audit_log_path = self._audit_log.path
            except OSError as e:
                self.audit_log_error = f"{type(e).__name__}: {e}"
        self._port = _bridge_port() or "8722"  # 서버 프로세스에 넘겨준 포트
        session_info = _bridge_session() or {}
        self._bridge_generation = str(session_info.get("generation", ""))
        params = _server_params()
        # 서버 stderr("Processing request..." 로그)가 채팅 화면에 섞이지 않게 파일로
        errlog = self._stack.enter_context(
            open(
                os.path.join(os.path.dirname(__file__), "mcp_server.log"),
                "a", encoding="utf-8", buffering=1,
            )
        )
        read, write = await self._stack.enter_async_context(stdio_client(params, errlog=errlog))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        self.tools = (await self.session.list_tools()).tools
        self._all_ollama_tools = [_to_ollama(t) for t in self.tools] + local_tools.SCHEMAS
        self._schemas = {t.name: t.inputSchema for t in self.tools}
        self._project_dir: str | None = config.UNITY_PROJECT_DIR or None
        self._project_identity_verified = False
        self.last_raw_result = ""  # /last 명령용, 절단 전 원본
        self.tool_mode = "full"
        self.set_tool_mode(config.TOOL_MODE)
        return self

    async def __aexit__(self, *exc):
        try:
            await self._stack.aclose()
        finally:
            if self._audit_log is not None:
                try:
                    outcome = "error" if exc and exc[0] is not None else "completed"
                    self._audit_log.close(outcome, logging_error=self.audit_log_error)
                except OSError as e:
                    self.audit_log_error = f"{type(e).__name__}: {e}"
                    self._audit_log.abort()
                self._audit_log = None

    def _dismiss_known_modals(self) -> bool:
        """브리지를 마비시키는 알려진 에디터 모달을 자동 처리. 처리했으면 True."""
        if not config.AUTO_CONSENT:
            return False
        import winmodal
        consent = winmodal.dismiss_script_update_consent()
        scene = winmodal.dismiss_scene_modified_ignore()
        return consent or scene

    async def _reconnect_if_port_changed(self) -> bool:
        """브리지가 도메인 리로드로 다른 포트에 바인드했으면 서버를 새 포트로 재기동.

        server.py는 시작 시 UNITY_MCP_PORT를 한 번만 읽으므로, 포트가 바뀌면
        stdio 서버 자체를 다시 띄워야 한다. (진입한 것과 같은 태스크에서만 호출할 것)
        """
        new_port = _bridge_port()
        if not new_port or new_port == self._port:
            return False
        previous_mode = self.tool_mode
        if self._audit_log is not None:
            try:
                self._audit_log.close("reconnected", next_port=new_port)
            except OSError:
                self._audit_log.abort()
            self._audit_log = None
        await self._stack.aclose()
        await self.__aenter__()
        self.set_tool_mode(previous_mode)
        return True

    def _recovery_event(self, phase: str, **payload) -> None:
        if self._audit_log is None:
            return
        try:
            self._audit_log.event("recovery_event", phase=phase, **payload)
        except OSError as error:
            self.audit_log_error = f"{type(error).__name__}: {error}"
            self._audit_log.abort()
            self._audit_log = None

    def _project_identity_error(self, ping_text: str) -> str | None:
        try:
            result = json.loads(ping_text)["result"]
            actual = os.path.normcase(os.path.realpath(str(result["projectPath"])))
        except (json.JSONDecodeError, KeyError, TypeError):
            return "Unity bridge identity could not be read"
        expected = os.path.normcase(os.path.realpath(config.UNITY_PROJECT_DIR))
        if actual != expected:
            return f"Unity bridge project mismatch: expected {expected}, got {actual}"
        return None

    async def _ensure_project_identity(self) -> str | None:
        if self._project_identity_verified or not config.UNITY_PROJECT_DIR:
            return None
        ping = await self._call_once("unity_ping", {})
        error = self._project_identity_error(ping)
        if error is None:
            self._project_identity_verified = True
            return None
        return error

    async def _recover_bridge(self, reason: str) -> bool:
        """Wait for domain reload and prove liveness even when the port is unchanged."""
        started = time.monotonic()
        deadline = started + config.BRIDGE_RECOVERY_TIMEOUT_SECONDS
        attempt = 0
        self._recovery_event("bridge_unavailable", reason=reason, port=self._port)
        while time.monotonic() < deadline:
            attempt += 1
            if attempt == 1 or attempt % 5 == 0:
                import winfocus
                winfocus.focus_unity(config.UNITY_PROJECT_DIR)
            port_changed = await self._reconnect_if_port_changed()
            ping = await self._call_once("unity_ping", {})
            error = self._project_identity_error(ping)
            if error is None:
                session_info = _bridge_session() or {}
                generation = str(session_info.get("generation", ""))
                generation_changed = bool(
                    generation and self._bridge_generation
                    and generation != self._bridge_generation
                )
                self._bridge_generation = generation or self._bridge_generation
                self._project_identity_verified = True
                self._recovery_event(
                    "bridge_ready",
                    attempt=attempt,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    port=self._port,
                    port_changed=port_changed,
                    generation_changed=generation_changed,
                )
                return True
            if "mismatch" in error:
                self._recovery_event("project_mismatch", attempt=attempt, error=error)
                return False
            await asyncio.sleep(config.BRIDGE_RECOVERY_POLL_SECONDS)
        self._recovery_event(
            "bridge_recovery_timeout",
            attempts=attempt,
            elapsed_seconds=round(time.monotonic() - started, 3),
            port=self._port,
        )
        return False

    @property
    def names(self) -> list[str]:
        return [t["function"]["name"] for t in self.ollama_tools]

    def set_tool_mode(self, mode: str) -> str:
        mode = str(mode or "full").strip().lower()
        if mode not in {"full", "verify"}:
            raise ValueError("tool mode must be 'full' or 'verify'")
        self.tool_mode = mode
        if mode == "verify":
            self.ollama_tools = [
                tool for tool in self._all_ollama_tools
                if tool["function"]["name"] in VERIFY_ONLY_TOOLS
            ]
        else:
            self.ollama_tools = list(self._all_ollama_tools)
        if self._audit_log is not None:
            try:
                self._audit_log.event("tool_mode_changed", tool_mode=mode, tools=self.names)
            except OSError as e:
                self.audit_log_error = f"{type(e).__name__}: {e}"
                self._audit_log.abort()
                self._audit_log = None
        return mode

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
            prev_focus = winfocus.focus_unity(config.UNITY_PROJECT_DIR)
        await asyncio.sleep(1.5)  # 컴파일은 refresh 반환 직후에 시작될 수 있다
        deadline = time.monotonic() + config.BRIDGE_RECOVERY_TIMEOUT_SECONDS
        bridge_missing = False
        while time.monotonic() < deadline:
            if self._dismiss_known_modals():
                await asyncio.sleep(2)  # 모달 해제 후 에디터가 이어서 진행할 시간
            state = await self._call_once("unity_get_state", {})
            try:
                r = json.loads(state)["result"]
            except (json.JSONDecodeError, KeyError, TypeError):
                bridge_missing = True
                await self._recover_bridge("compile_domain_reload")
                continue
            bridge_missing = False
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
        if bridge_missing:
            detail = "브릿지 재연결 시간이 초과되었습니다"
        else:
            detail = "Unity 컴파일 또는 에셋 갱신 시간이 초과되었습니다"
        return refresh_text + f"\n[{detail}. recovery_event 로그를 확인하세요.]"

    def _coerce_args(self, name: str, args: dict) -> dict:
        """스키마상 array/number/bool 인자가 문자열로 오면 json.loads로 보정."""
        props = self._schemas.get(name, {}).get("properties", {})
        fixed = {}
        for key, value in args.items():
            schema_type = props.get(key, {}).get("type")
            if isinstance(value, str) and schema_type in ("array", "number", "integer", "boolean"):
                try:
                    value = _lenient_json(value)
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

    async def _call_impl(self, name: str, args: dict) -> str:
        if self.tool_mode == "verify" and name not in VERIFY_ONLY_TOOLS:
            return json.dumps({
                "status": "error",
                "error": (
                    f"verification-only mode blocked {name}; only state/query, play, "
                    "input, wait and screenshot tools are allowed"
                ),
            }, ensure_ascii=False)

        if name == "unity_wait":
            try:
                seconds = local_tools.wait_seconds(args)
            except ValueError as e:
                return _truncate(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
            await asyncio.sleep(seconds)
            text = json.dumps({"status": "ok", "result": {"waited_seconds": seconds}}, ensure_ascii=False)
            self.last_raw_result = text
            return text

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
        if name in _MUTATIONS:
            identity_error = await self._ensure_project_identity()
            if identity_error:
                return json.dumps(
                    {"status": "error", "error": identity_error}, ensure_ascii=False
                )
        # Unity가 백그라운드/최소화면 플레이어 루프가 멈춰 키 입력이 게임에 닿지 않는다.
        # 입력 시뮬레이션 전에 호스트가 결정적으로 포커스를 확보한다.
        if name == "unity_send_key" and config.FOCUS_UNITY_ON_INPUT:
            import winfocus
            winfocus.focus_unity(config.UNITY_PROJECT_DIR)
        text = await self._call_once(name, args)
        # 도메인 리로드(플레이 모드 전환·스크립트 컴파일) 중엔 브리지가 몇 초 다운된다.
        # "Cannot reach"는 명령이 Unity에 도달하지 못한 경우라 재시도해도 안전 → 1회만.
        # "Empty response"는 명령이 실행됐을 수도 있어 읽기 전용 도구만 재시도한다.
        retryable = "Cannot reach the Unity bridge" in text or (
            "Empty response from Unity bridge" in text and name in _READONLY
        )
        if retryable:
            # 모달이 에디터를 막아 생긴 무응답일 수 있다 — 알려진 모달부터 처리
            dismissed = self._dismiss_known_modals()
            # 리로드로 브리지가 다른 포트로 옮겨갔을 수 있다
            recovered = await self._recover_bridge(f"{name}:{text[:120]}")
            if recovered:
                text = await self._call_once(name, args)
            if dismissed:
                text += "\n[막고 있던 API 업데이트 동의 모달을 자동 처리했습니다]"
        if name == "unity_refresh_assets":
            text = await self._wait_for_compile(text)
        self.last_raw_result = text
        return _truncate(text)

    async def call(self, name: str, args: dict) -> str:
        """Execute one tool and audit it even when no Agent is involved."""
        args = dict(args or {})
        # _call_impl stores the untruncated response here for /last and audit.
        # Clear it first so an early policy/unknown-tool return cannot inherit a
        # previous call's raw result.
        self.last_raw_result = ""
        audit = self._audit_log
        audit_state: tuple[int, float] | None = None
        if audit is not None:
            try:
                audit_state = audit.call_started(name, args, self.tool_mode)
            except OSError as e:
                self.audit_log_error = f"{type(e).__name__}: {e}"
                audit.abort()
                self._audit_log = None
                audit = None
        try:
            result = await self._call_impl(name, args)
        except BaseException as e:
            if audit is not None and audit_state is not None:
                try:
                    audit.call_finished(audit_state[0], name, audit_state[1], "", exception=e)
                except OSError:
                    audit.abort()
                    self._audit_log = None
            raise
        if audit is not None and audit_state is not None:
            try:
                full_result = self.last_raw_result or result
                audit.call_finished(audit_state[0], name, audit_state[1], full_result)
            except OSError as e:
                self.audit_log_error = f"{type(e).__name__}: {e}"
                audit.abort()
                self._audit_log = None
        if not self.last_raw_result:
            self.last_raw_result = result
        return result
