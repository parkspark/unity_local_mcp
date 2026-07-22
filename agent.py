"""Ollama tool-call 에이전트 루프."""

import asyncio
import json
import os
import re
import time
import traceback

import ollama

import config
import planner
from mcp_client import UnityTools
from run_logging import RunLogger
from task_contract import TaskContract
from verification import (
    MUTATION_TOOLS, VerificationContract, VerificationSpec, fix_prompt, write_receipt,
)

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
- Scene/component/material edit tools are edit-mode only. Stop play mode before modifying anything; the bridge rejects these calls before any side effect.
- Never fabricate tool results. If the Unity bridge is unreachable, say so and tell the user to check the Unity Editor.
- unity_screenshot creates missing output directories, saves a PNG and returns its path. You cannot see images — report the path to the user and ask them to look at it.
- Prefer few, targeted tool calls. Do not dump the full hierarchy unless the user asks for it.

Writing C# scripts:
- New behaviour script: unity_write_script → unity_refresh_assets (the host waits for compilation) → unity_read_console types="error" → if no errors, unity_add_component with the class name.
- unity_add_component is idempotent by default. If a duplicate already exists it returns alreadyPresent=true. Use unity_remove_component remove_all=true to clean accidental duplicates; never delete and recreate the whole GameObject just to remove one component.
- If there are compile errors, fix the script with unity_write_script and repeat.
- The C# class name MUST match the file name. Before modifying an existing script, read it first with unity_read_script.
- ALWAYS place scripts under Assets/Scripts/. Before creating a new script, check it does not already exist elsewhere (unity_list_assets filter "t:Script"). Two files defining the same class break ALL compilation in the project.
- To remove a stale, duplicate, or conflicting script that breaks compilation, DELETE it with unity_delete_script (do NOT just leave it or try to work around it), then unity_refresh_assets to recompile.
- This project uses the NEW Input System ONLY. NEVER use the legacy UnityEngine.Input API (Input.GetAxis, Input.GetButtonDown, Input.GetKey...) — it throws InvalidOperationException at runtime. Instead `using UnityEngine.InputSystem;` and read `Keyboard.current` / `Mouse.current` / `Gamepad.current`, e.g. `keyboard.aKey.isPressed`, `keyboard.spaceKey.wasPressedThisFrame`. Always null-check `Keyboard.current` first.
- Unity 6 renamed APIs — obsolete names trigger a blocking editor dialog. Use `rb.linearVelocity` (NOT `rb.velocity`) and `Object.FindFirstObjectByType<T>()` (NOT `FindObjectOfType<T>()`).

Data-driven levels (games with stages/levels):
- NEVER hand-build level layouts in the scene or hardcode them in scripts. Install the canonical loader ONCE with unity_install_level_loader → unity_refresh_assets → check errors → add the LevelLoader component to an empty GameObject and set its levelFile property.
- Write each level as JSON with unity_write_level to Assets/StreamingAssets/Levels/levelN.json. The host validates the schema and reports specific errors — fix and rewrite until it passes.
- The player object MUST be named exactly "Player" (the loader moves it to player_spawn). Chain levels with "next_level": "level2.json"; the last level uses null.
- LevelLoader builds 3D cubes and trigger colliders. Player movement MUST use Rigidbody/Collider and Vector3/Collision (3D), NEVER Rigidbody2D/Collider2D/Vector2/Collision2D.
- Level JSON needs NO recompile: after writing, verify directly with play mode and look for "[LevelLoader] Loaded <name>" in the console. "[LevelLoader] GOAL reached" confirms level clear; "[LevelLoader] ALL LEVELS CLEAR" confirms the chain end.

Input simulation (gameplay verification):
- unity_send_key simulates a keyboard key during play mode only. action "tap" presses and auto-releases after `duration` seconds; "press" holds until "release".
- When exact key-release evidence matters, call unity_get_input_state after a wait and require both held and pendingReleases to be empty.
- To verify movement: unity_get_gameobject Player (note position) → unity_send_key key="rightArrow" action="press" → unity_wait 1 → unity_send_key key="rightArrow" action="release" → unity_get_gameobject Player again and compare positions.
- Unchanged before/after positions are a failed verification: fix the Player component/physics/input implementation, then repeat the measurement.
- Combos: press one key, tap another, then release (e.g. hold rightArrow, tap space to jump).
"""

# qwen3-coder가 <tool_call> 여는 태그를 생략하는 등 포맷을 벗어나면 Ollama 파서가
# 놓치고 텍스트로 샌다. 그런 경우를 직접 파싱해 복구한다.
SYSTEM_PROMPT += """

Execution policy:
- Do not use unity_execute_menu_item; native dialogs are intentionally blocked.
- Existing scripts are out of scope unless the user explicitly names their Assets/... path.
- The host enforces script compilation checks after writes and a unity_wait + runtime error check after play mode. Complete those checks before claiming success.
"""

VERIFY_MODE_PROMPT = """\
[호스트 강제 검증 전용 모드]
현재 상태를 검사하고 실제 증거를 수집하는 일만 수행하라. 파일, 씬, GameObject,
컴포넌트, 머티리얼을 생성·수정·삭제하지 마라. 편집 도구는 스키마에서 숨겨지고
호스트에서도 차단된다. 기존 결과가 요구와 다르면 고치려 하지 말고 차이와 증거를
보고하라. 플레이 모드에 들어갔다면 검증 후 반드시 종료하라.
"""

_LEAKED_TOOLCALL = re.compile(r"<(function|tool_call)[=\s>]")
_FUNC_BLOCK = re.compile(r"<function=([\w\-.]+)>(.*?)</function>", re.S)
_PARAM = re.compile(r"<parameter=([\w\-.]+)>\n?(.*?)\n?</parameter>", re.S)
_MARKUP = re.compile(r"</?(?:tool_call|function|parameter)[^>]*>")


def _screenshot_path(result: str) -> str | None:
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return None

    def find(node):
        if isinstance(node, str) and node.lower().endswith(".png"):
            return node
        if isinstance(node, dict):
            return next((found for value in node.values() if (found := find(value))), None)
        if isinstance(node, list):
            return next((found for value in node if (found := find(value))), None)
        return None

    return find(data)


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


def _milestone_prompt(plan: "planner.Plan", idx: int, ledger: "planner.ArtifactLedger",
                      prev_error: str = "") -> str:
    """마일스톤 실행 프롬프트. 전부 호스트가 결정적으로 합성한다(모델 요약 없음)."""
    done_status = {title: ok for title, ok, _ in ledger.done}
    plan_lines = []
    for i, m in enumerate(plan.milestones):
        if m.title in done_status:
            mark = "완료" if done_status[m.title] else "실패"
        elif i == idx:
            mark = "← 현재"
        else:
            mark = "대기"
        plan_lines.append(f"{m.id} {m.title} [{mark}]")
    m = plan.milestones[idx]
    parts = [
        f"[전체 목표] {plan.request}",
        "[계획]\n" + "\n".join(plan_lines),
        "[지금까지의 산출물]\n" + ledger.summary(),
    ]
    if prev_error:
        parts.append(f"[이전 시도 실패 원인] {prev_error}")
        parts.append(
            "[재시도 규칙] 위 산출물 대장에 있는 파일·씬·오브젝트는 이미 성공한 결과다. "
            "먼저 조회해서 재사용하고, 누락 또는 실제 오류가 확인되지 않은 산출물을 다시 "
            "설치·생성·덮어쓰지 마라. 이전 검증의 미완료 부분부터 이어서 수행하라."
        )
    parts.append(f"[현재 마일스톤 {m.id}] {m.goal}")
    if m.deliverables:
        parts.append("이 마일스톤이 만들어야 하는 파일: " + ", ".join(m.deliverables))
    parts.append(
        "이 마일스톤만 수행하라. 이후 마일스톤의 작업은 하지 마라. "
        "필요한 검증(누락 시 호스트가 알려준다)까지 끝나면 한두 문장으로 보고하고 멈춰라."
    )
    return "\n\n".join(parts)


class Agent:
    def __init__(self, tools: UnityTools, on_text, on_tool, on_warn, on_milestone=None,
                 enable_logging: bool | None = None,
                 enable_verification: bool | None = None):
        self.client = ollama.AsyncClient()
        self.tools = tools
        self.model = config.MODEL
        self.history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        # 표시 콜백: on_text(스트리밍 텍스트 조각), on_tool(이름, 인자, 결과), on_warn(경고문)
        self._on_text_callback = on_text
        self._on_tool_callback = on_tool
        self._on_warn_callback = on_warn
        # on_milestone(idx, total, title): 플랜 실행 진행 표시 (없으면 무시)
        self._on_milestone_callback = on_milestone or (lambda idx, total, title: None)
        self.on_text = self._emit_text
        self.on_tool = self._emit_tool
        self.on_warn = self._emit_warn
        self.on_milestone = self._emit_milestone
        self.enable_logging = config.RUN_LOGS if enable_logging is None else enable_logging
        self.enable_verification = (
            config.VERIFY_ORCHESTRATION if enable_verification is None else enable_verification
        )
        self._run_log: RunLogger | None = None
        self._run_log_error: str | None = None
        self.last_run_log_paths: tuple[str, str] | None = None
        self.last_verification_receipt_path: str | None = None
        self.known_session_scripts: set[str] = set()
        self._active_tool_mode = "full"
        self._verify_entered_play = False
        self._suppress_text = False
        self._turn_mutation_count = 0

    @staticmethod
    def _display(callback, *args):
        """UI/console encoding failures must not abort a Unity operation."""
        try:
            callback(*args)
        except UnicodeError:
            # The complete content still exists in the UTF-8 execution/audit log.
            # Suppressing a broken legacy-console render is safer than retrying a
            # callback that may have emitted partial output already.
            return

    def _emit_text(self, chunk: str):
        if self._suppress_text:
            return
        self._display(self._on_text_callback, chunk)

    def _log(self, event: str, **payload):
        if self._run_log is None:
            return
        try:
            self._run_log.event(event, **payload)
        except OSError as e:
            self._run_log_error = f"{type(e).__name__}: {e}"
            self._run_log.abort()
            self._run_log = None
            try:
                self._display(self._on_warn_callback,
                    f"실행 로그 기록이 중단됐습니다({self._run_log_error}). 작업은 계속합니다."
                )
            except Exception:
                pass

    def _emit_tool(self, name: str, args: dict, result: str):
        self._log("tool_result", name=name, arguments=args, result=result)
        self._display(self._on_tool_callback, name, args, result)

    def _emit_warn(self, message: str):
        self._log("warning", message=message)
        self._display(self._on_warn_callback, message)

    def _emit_milestone(self, idx: int, total: int, title: str):
        self._log("milestone_started", index=idx + 1, total=total, title=title)
        self._display(self._on_milestone_callback, idx, total, title)

    def reset(self):
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.known_session_scripts.clear()

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

    async def _chat(self, messages: list[dict] | None = None, use_tools: bool = True):
        """1회 모델 호출. (content, [(도구명, 인자)]) 반환. 스트리밍 텍스트는 on_text로 전달."""
        kwargs = dict(
            model=self.model,
            messages=self.history if messages is None else messages,
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
        self._log(
            "assistant_response",
            content=content,
            tool_calls=[{"name": name, "arguments": args} for name, args in calls],
            tools_enabled=use_tools,
        )
        return content, calls

    async def _inspect_screenshot(self, result: str) -> str | None:
        """Optionally feed a locally analysed screenshot back into this tool loop."""
        if not config.AUTO_VISION:
            return None
        path = _screenshot_path(result)
        if path and not os.path.isabs(path):
            # Unity accepts project-relative output paths. Resolve those before
            # giving the image to the local vision model.
            path = os.path.join(config.UNITY_PROJECT_DIR, path)
        if not path or not os.path.exists(path):
            return None
        try:
            response = await ollama.AsyncClient().chat(
                model=config.VISION_MODEL,
                messages=[{
                    "role": "user",
                    "content": (
                        "Inspect this Unity game screenshot. Report only observable facts: "
                        "whether the requested game objects/UI are visible, obvious layout problems, "
                        "and whether the image is too incomplete to verify."
                    ),
                    "images": [path],
                }],
                options={"temperature": 0},
            )
        except Exception as e:
            self.on_warn(f"Local vision analysis failed: {type(e).__name__}: {e}")
            return None
        content = response.message.content or None
        self._log("vision_result", model=config.VISION_MODEL, content=content, image_path=path)
        return content

    async def run_turn(self, user_text: str, tool_mode: str | None = None):
        """Execute one request and persist a transcript for every outcome.

        ``tool_mode="verify"`` is a one-turn, host-enforced read/execute-only
        boundary.  Tool schemas are filtered and leaked mutation calls are still
        rejected by :class:`UnityTools`.
        """
        self.last_run_log_paths = None
        self.last_verification_receipt_path = None
        self._run_log_error = None
        self._turn_mutation_count = 0
        previous_tool_mode = getattr(self.tools, "tool_mode", "full")
        active_tool_mode = str(tool_mode or previous_tool_mode or "full").lower()
        self._active_tool_mode = active_tool_mode
        self._verify_entered_play = False
        if hasattr(self.tools, "set_tool_mode"):
            self.tools.set_tool_mode(active_tool_mode)
        effective_user_text = user_text
        if active_tool_mode == "verify":
            effective_user_text = VERIFY_MODE_PROMPT + "\n[검증 요청]\n" + user_text
        spec = VerificationSpec.from_request(user_text, force=active_tool_mode == "verify")
        managed_verification = self.enable_verification and spec.enabled
        if self.enable_logging:
            try:
                self._run_log = RunLogger(config.RUN_LOG_DIR, user_text, self.model)
                self.last_run_log_paths = self._run_log.paths
                self._log(
                    "run_configuration",
                    planner=config.PLANNER,
                    max_iters=config.MAX_ITERS,
                    milestone_max_iters=config.MILESTONE_MAX_ITERS,
                    plan_total_iters=config.PLAN_MAX_TOTAL_ITERS,
                    tool_mode=active_tool_mode,
                    verification_orchestration=managed_verification,
                    verification_spec=spec.__dict__,
                )
            except OSError as e:
                self._run_log = None
                self._run_log_error = f"{type(e).__name__}: {e}"
                self._display(self._on_warn_callback,
                    f"실행 로그를 시작하지 못했습니다({self._run_log_error}). 작업은 계속합니다."
                )

        outcome = "completed"
        success = False
        build_success: bool | None = None
        started = time.monotonic()
        try:
            self._suppress_text = managed_verification
            if active_tool_mode == "verify" and managed_verification:
                build_success = None
            else:
                build_success = await self._execute_requested_work(
                    effective_user_text, active_tool_mode
                )
            if managed_verification:
                success = await self._run_verification_orchestration(
                    spec, build_success, started, allow_fix=active_tool_mode != "verify"
                )
                outcome = "verified" if success else "failed"
            else:
                success = bool(build_success)
                outcome = "completed" if success else "failed"
            return success
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
                outcome = "interrupted"
            else:
                outcome = "error"
            self._log(
                "exception",
                exception_type=type(e).__name__,
                message=str(e),
                traceback=traceback.format_exc(),
            )
            raise
        finally:
            if active_tool_mode == "verify" and self._verify_entered_play:
                # A failed/limited verification must not strand Unity in play
                # mode or leave simulated keys held. Exiting play mode clears the
                # bridge's held/pending key sets deterministically.
                try:
                    cleanup_args = {"action": "stop"}
                    cleanup_result = await self.tools.call("unity_play_mode", cleanup_args)
                    self.on_tool("unity_play_mode", cleanup_args, cleanup_result)
                    self._log("verify_cleanup", action="stop", result=cleanup_result)
                except BaseException as cleanup_error:
                    self._log(
                        "verify_cleanup_error",
                        exception_type=type(cleanup_error).__name__,
                        message=str(cleanup_error),
                    )
            logger = self._run_log
            if logger is not None:
                try:
                    logger.close(outcome, logging_error=self._run_log_error)
                except OSError as e:
                    self._run_log_error = f"{type(e).__name__}: {e}"
                    logger.abort()
                    try:
                        self._display(self._on_warn_callback,
                            f"실행 로그 종료 기록에 실패했습니다({self._run_log_error})."
                        )
                    except Exception:
                        pass
                self._run_log = None
            if hasattr(self.tools, "set_tool_mode"):
                self.tools.set_tool_mode(previous_tool_mode)
            self._active_tool_mode = previous_tool_mode
            self._verify_entered_play = False
            self._suppress_text = False

    async def _execute_requested_work(self, user_text: str, active_tool_mode: str) -> bool:
        """Run the v1.8 builder. Its prose is provisional under v1.9."""
        plan = None
        if active_tool_mode != "verify" and config.PLANNER != "off" and (
            config.PLANNER == "always" or planner.looks_large(user_text)
        ):
            self.on_warn("큰 요청으로 판단해 실행 계획을 먼저 세웁니다...")
            plan = await self._make_plan(user_text)
        if plan is None:
            self._log("execution_mode", mode="single", tool_mode=active_tool_mode)
            return await self._run_single(user_text)
        self._log(
            "execution_mode",
            mode="plan",
            milestones=[{
                "id": m.id,
                "title": m.title,
                "goal": m.goal,
                "deliverables": m.deliverables,
                "verify": m.verify,
                "max_iters": m.max_iters,
            } for m in plan.milestones],
        )
        self.on_warn(
            "실행 계획: "
            + " → ".join(
                f"{m.id} {m.title} (verify: {','.join(m.verify) or 'none'})"
                for m in plan.milestones
            )
        )
        return await self._run_plan(user_text, plan)

    async def _make_plan(self, user_text: str):
        """플래닝 호출 래퍼 (테스트에서 오버라이드 지점)."""
        return await planner.make_plan(self.client, self.model, user_text, self.on_warn)

    async def _verification_call(self, contract: VerificationContract,
                                 name: str, args: dict) -> str:
        """Execute one host-selected verification call and record its evidence."""
        args, violation = contract.prepare_call(name, args)
        if violation:
            result = json.dumps({"status": "error", "error": violation}, ensure_ascii=False)
            self.on_warn(violation)
        else:
            try:
                result = await self.tools.call(name, args)
                # UnityTools intentionally truncates large results before they
                # reach a model/UI. Host verification must parse the retained
                # full response or a long console stack could become invalid
                # JSON and silently erase real error evidence.
                evidence_result = getattr(self.tools, "last_raw_result", "") or result
                contract.observe(name, args, evidence_result)
            except Exception as error:
                detail = f"{name}:{type(error).__name__}:{error}"
                contract.tool_errors.append(detail)
                result = json.dumps(
                    {"status": "error", "error": detail}, ensure_ascii=False
                )
                self._log("verification_tool_error", name=name, error=detail)
        self.on_tool(name, args, result)
        return result

    async def _collect_verification(self, spec: VerificationSpec) -> VerificationContract:
        """Collect a fixed evidence sequence without giving a model completion authority."""
        contract = VerificationContract(spec, config.UNITY_PROJECT_DIR)
        if hasattr(self.tools, "set_tool_mode"):
            self.tools.set_tool_mode("verify")
        self._active_tool_mode = "verify"
        self._verify_entered_play = False
        self._log("verification_started", checklist=spec.checklist())

        async def confirm_play_active() -> bool:
            """The play command acknowledgement is provisional; sample actual state."""
            await self._verification_call(contract, "unity_get_state", {})
            return contract.playing

        async def measure_motion(label: str, key: str, boost: bool = False) -> bool:
            if not await confirm_play_active():
                return False
            await self._verification_call(
                contract, "unity_get_gameobject", {"target": "Player"}
            )
            if spec.require_camera_follow or label in {"d", "a", "boost_normal", "boost_shift"}:
                await self._verification_call(
                    contract, "unity_get_gameobject", {"target": "Main Camera"}
                )
            if not contract.begin_motion(label):
                return False
            await self._verification_call(contract, "unity_send_key", {
                "key": key, "action": "press",
            })
            if boost:
                await self._verification_call(contract, "unity_send_key", {
                    "key": "leftShift", "action": "press",
                })
            await self._verification_call(contract, "unity_wait", {"seconds": 0.5})
            if boost:
                await self._verification_call(contract, "unity_send_key", {
                    "key": "leftShift", "action": "release",
                })
            await self._verification_call(contract, "unity_send_key", {
                "key": key, "action": "release",
            })
            await self._verification_call(contract, "unity_wait", {"seconds": 0.5})
            await self._verification_call(
                contract, "unity_get_gameobject", {"target": "Player"}
            )
            if spec.require_camera_follow or label in {"d", "a", "boost_normal", "boost_shift"}:
                await self._verification_call(
                    contract, "unity_get_gameobject", {"target": "Main Camera"}
                )
            contract.end_motion(label)
            return True

        try:
            await self._verification_call(contract, "unity_get_state", {})
            await self._verification_call(
                contract, "unity_read_console", {"types": "error,exception"}
            )
            for target in spec.required_components:
                await self._verification_call(
                    contract, "unity_get_gameobject", {"target": target}
                )

            if spec.require_gameplay:
                result = await self._verification_call(
                    contract, "unity_play_mode", {"action": "play"}
                )
                decoded = json.JSONDecoder().raw_decode(str(result).lstrip())[0]
                self._verify_entered_play = (
                    decoded.get("status") == "ok" and contract.playing
                )
                await self._verification_call(contract, "unity_wait", {"seconds": 0.7})
                if await confirm_play_active():
                    await self._verification_call(
                        contract, "unity_read_console", {"types": "error,exception"}
                    )
                    if spec.require_level_marker:
                        await self._verification_call(
                            contract, "unity_read_console", {"types": "log"}
                        )
                if spec.require_movement:
                    await measure_motion("rightArrow", "rightArrow")
                if spec.require_bidirectional:
                    await measure_motion("d", "d")
                    await measure_motion("a", "a")
                if spec.require_boost:
                    await measure_motion("boost_normal", "d")
                    await measure_motion("boost_shift", "d", boost=True)
                if spec.require_jump and await confirm_play_active():
                    # Capture two post-input samples so a brief apex is not
                    # mistaken for a failed jump.
                    await self._verification_call(
                        contract, "unity_get_gameobject", {"target": "Player"}
                    )
                    await self._verification_call(contract, "unity_send_key", {
                        "key": "space", "action": "tap", "duration": 0.08,
                    })
                    await self._verification_call(contract, "unity_wait", {"seconds": 0.5})
                    await self._verification_call(
                        contract, "unity_get_gameobject", {"target": "Player"}
                    )
                    await self._verification_call(contract, "unity_wait", {"seconds": 0.5})
                    await self._verification_call(
                        contract, "unity_get_gameobject", {"target": "Player"}
                    )
                if spec.require_screenshot and contract.playing:
                    stamp = time.strftime("%Y%m%d_%H%M%S")
                    await self._verification_call(contract, "unity_screenshot", {
                        "view": "game", "width": 1280, "height": 720,
                        "output_path": f"Assets/Screenshots/verification/v19_{stamp}.png",
                    })
                if spec.require_movement or spec.require_jump or spec.require_boost:
                    await self._verification_call(contract, "unity_get_input_state", {})
        except (TypeError, ValueError, AttributeError, KeyError) as error:
            self._log(
                "verification_collection_error",
                exception_type=type(error).__name__, message=str(error),
            )
        finally:
            if self._verify_entered_play or contract.playing:
                try:
                    await self._verification_call(
                        contract, "unity_play_mode", {"action": "stop"}
                    )
                finally:
                    self._verify_entered_play = False
            # A final state read makes the stopped/saved claim measurable rather
            # than inferring it from the stop command's acknowledgement.
            await self._verification_call(contract, "unity_get_state", {})

        failures = contract.failures()
        self._log(
            "verification_finished",
            outcome="passed" if not failures else "failed",
            failures=failures,
            evidence=contract.evidence(),
        )
        return contract

    async def _run_verification_orchestration(
        self, spec: VerificationSpec, build_success: bool | None,
        started: float, allow_fix: bool,
    ) -> bool:
        """Verify, repair only failed checks in fresh contexts, then reverify."""
        attempts: list[dict] = []
        contract = await self._collect_verification(spec)
        failures = contract.failures()
        if build_success is not None and self._turn_mutation_count == 0:
            failures.append("builder_produced_no_mutation_evidence")
        attempts.append({
            "phase": "verify", "attempt": 1,
            "failures": failures, "evidence": contract.evidence(),
        })
        previous_fingerprint = tuple(sorted(failures))
        stagnant = 0

        for cycle in range(1, config.FIX_MAX_CYCLES + 1):
            if not failures or not allow_fix:
                break
            if time.monotonic() - started >= config.TASK_TIMEOUT_SECONDS:
                failures.append("task_time_budget_exhausted")
                break
            self.on_warn(
                f"독립 검증 미통과 {len(failures)}건 — 실패 항목 자동 수정 "
                f"{cycle}/{config.FIX_MAX_CYCLES}"
            )
            if hasattr(self.tools, "set_tool_mode"):
                self.tools.set_tool_mode("full")
            self._active_tool_mode = "full"
            evidence = contract.evidence()
            # Host evidence may identify an exact existing script even when the
            # original natural-language request did not name its path. Scope
            # automatic repair to only those measured components/stack traces.
            repair_scope = [spec.request, json.dumps(evidence, ensure_ascii=False, default=str)]
            for component_types in evidence.get("components", {}).values():
                for component_type in component_types:
                    class_name = str(component_type).rsplit(".", 1)[-1]
                    candidate = f"Assets/Scripts/{class_name}.cs"
                    if os.path.exists(os.path.join(config.UNITY_PROJECT_DIR, candidate)):
                        repair_scope.append(candidate)
            fix_contract = TaskContract.from_request(
                "\n".join(repair_scope), self.known_session_scripts
            )
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": fix_prompt(spec, failures, evidence)},
            ]
            remaining = max(0.001, config.TASK_TIMEOUT_SECONDS - (time.monotonic() - started))
            try:
                fix_ok, fix_note, fix_iters = await asyncio.wait_for(
                    self._react_loop(messages, fix_contract, config.FIX_MAX_ITERS),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                failures.append("task_time_budget_exhausted")
                self._log("verification_fix_timeout", cycle=cycle, timeout=remaining)
                break
            self._log(
                "verification_fix_finished", cycle=cycle, model_loop_ok=fix_ok,
                note=fix_note, iterations=fix_iters,
            )

            contract = await self._collect_verification(spec)
            failures = contract.failures()
            if self._turn_mutation_count == 0:
                failures.append("builder_produced_no_mutation_evidence")
            attempts.append({
                "phase": "reverify", "attempt": cycle + 1,
                "failures": failures, "evidence": contract.evidence(),
            })
            fingerprint = tuple(sorted(failures))
            if fingerprint == previous_fingerprint:
                stagnant += 1
                if config.NO_PROGRESS_LIMIT and stagnant >= config.NO_PROGRESS_LIMIT:
                    failures.append("no_verification_progress")
                    break
            else:
                stagnant = 0
            previous_fingerprint = fingerprint

        success = not failures
        status = "verified" if success else "failed"
        try:
            receipt = write_receipt(
                config.VERIFICATION_RECEIPT_DIR, spec, status, contract.evidence(),
                failures, attempts, time.monotonic() - started, build_success,
            )
            self.last_verification_receipt_path = receipt
            self._log("verification_receipt", path=receipt, status=status)
        except OSError as error:
            receipt = None
            self.on_warn(f"검증 영수증 저장 실패: {type(error).__name__}: {error}")

        if success:
            report = "\n✅ 호스트 독립 검증 통과 — 실제 Unity 증거로 완료를 판정했습니다."
        else:
            report = (
                "\n❌ 완료로 판정하지 않았습니다. 미통과: "
                + ", ".join(failures)
            )
        if receipt:
            report += f"\n검증 영수증: {receipt}"
        report += "\n"
        self._display(self._on_text_callback, report)
        self.history.append({"role": "assistant", "content": report.strip()})
        return success

    async def _react_loop(self, messages: list[dict], contract: TaskContract,
                          max_iters: int, ledger=None) -> tuple[bool, str, int]:
        """도구 호출 루프 본문. (정상 종료 여부, 실패 사유, 사용 iteration) 반환.

        단일 모드에서는 messages가 self.history이고, 플랜 모드에서는 마일스톤별
        fresh 리스트다. 동작(정책 게이트, 검증 강제, 루프 가드, 누수 복구)은 동일.
        """
        call_counts: dict[str, int] = {}
        nudged = False
        for iteration in range(max_iters):
            content, calls = await self._chat(messages)
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {"function": {"name": n, "arguments": a}} for n, a in calls
                    ] or None,
                }
            )
            if not calls:
                missing = contract.missing_verification()
                if missing:
                    self._log("verification_incomplete", missing=missing)
                    self.on_warn(
                        "Verification is incomplete: " + "; ".join(missing)
                        + " — asking the local model to finish it."
                    )
                    messages.append({
                        "role": "user",
                        "content": "[Required verification before completion] " + "; ".join(missing),
                    })
                    continue
                return True, "", iteration + 1  # 최종 답변은 이미 스트리밍으로 출력됨

            for name, args in calls:
                args, violation = contract.prepare_call(name, args)
                if violation:
                    result = json.dumps({"status": "error", "error": violation}, ensure_ascii=False)
                    self.on_warn(violation)
                else:
                    result = await self.tools.call(name, args)
                    if self._active_tool_mode == "verify" and name == "unity_play_mode":
                        try:
                            response, _ = json.JSONDecoder().raw_decode(result.lstrip())
                            if response.get("status") == "ok":
                                action = str(args.get("action", "")).lower()
                                if action == "play":
                                    self._verify_entered_play = True
                                elif action == "stop":
                                    self._verify_entered_play = False
                        except (TypeError, ValueError, AttributeError):
                            pass
                    contract.observe(name, args, result)
                    if name in MUTATION_TOOLS:
                        try:
                            response, _ = json.JSONDecoder().raw_decode(result.lstrip())
                            if response.get("status") == "ok":
                                self._turn_mutation_count += 1
                        except (TypeError, ValueError, AttributeError):
                            pass
                    if ledger is not None:
                        ledger.observe(name, args, result)
                    self.known_session_scripts = set(contract.session_scripts)
                self.on_tool(name, args, result)
                vision = await self._inspect_screenshot(result) if name == "unity_screenshot" else None
                if vision:
                    result += f"\n[Local vision result from {config.VISION_MODEL}] {vision}"
                messages.append(
                    {"role": "tool", "tool_name": name, "content": result}
                )

            # 같은 툴 반복 호출 감지: 개별 호출을 쌓는 대신 배치/스크립트로 유도
            if config.LOOP_GUARD_THRESHOLD and not nudged:
                for name, _a in calls:
                    call_counts[name] = call_counts.get(name, 0) + 1
                    if call_counts[name] >= config.LOOP_GUARD_THRESHOLD:
                        messages.append({"role": "user", "content": (
                            "[시스템] 같은 툴을 반복 호출하고 있습니다. 반복적인 오브젝트 생성은 "
                            "unity_create_gameobjects 배치 툴 하나로 처리하거나, 큰 그리드는 "
                            "스크립트의 Awake()/Start()에서 생성하세요."
                        )})
                        self.on_warn(
                            f"{name} 반복 호출 감지 — 배치 툴/스크립트 사용을 권고했습니다."
                        )
                        nudged = True
                        break
        return False, "tool-call iteration limit reached", max_iters

    async def _run_single(self, user_text: str) -> bool:
        """기존 단일 ReAct 루프 (v1.6까지의 run_turn 동작)."""
        contract = TaskContract.from_request(user_text, self.known_session_scripts)
        self.history.append({"role": "user", "content": user_text})
        self._trim_history()

        ok, _note, _used = await self._react_loop(self.history, contract, config.MAX_ITERS)
        if ok:
            return True

        # 반복 한도 도달: 툴 없이 요약만 받는다
        self.history.append(
            {
                "role": "user",
                "content": "툴 호출 한도에 도달했습니다. 지금까지의 진행 상황을 요약하고 멈추세요.",
            }
        )
        content, _ = await self._chat(use_tools=False)
        self.history.append({"role": "assistant", "content": content})
        return False

    def _deliverables_missing(self, milestone) -> list[str]:
        """마일스톤 deliverables의 파일 존재를 호스트가 결정적으로 확인."""
        if not config.UNITY_PROJECT_DIR:
            return []
        missing = []
        for rel in milestone.deliverables:
            if not os.path.exists(os.path.join(config.UNITY_PROJECT_DIR, rel)):
                missing.append(rel)
        return missing

    async def _run_milestone(self, plan, idx: int, ledger, prev_error: str = "",
                             max_iters: int | None = None) -> tuple[bool, str, int]:
        m = plan.milestones[idx]
        contract = TaskContract.for_milestone(m, self.known_session_scripts)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _milestone_prompt(plan, idx, ledger, prev_error)},
        ]
        # 마일스톤 경계에서 도메인 리로드/포트 호핑을 흡수 (실패해도 call()의 재시도가 처리)
        ping_result = await self.tools.call("unity_ping", {})
        self._log("milestone_ping", milestone_id=m.id, result=ping_result)
        ok, note, used = await self._react_loop(messages, contract, max_iters or m.max_iters, ledger)
        if ok:
            missing_files = self._deliverables_missing(m)
            if missing_files:
                return False, "deliverables not created: " + ", ".join(missing_files), used
        return ok, note, used

    async def _run_plan(self, user_text: str, plan) -> bool:
        """마일스톤 순차 실행. 마일스톤마다 fresh 히스토리 + 자체 계약."""
        self.history.append({"role": "user", "content": user_text})
        ledger = planner.ArtifactLedger()
        budget = config.PLAN_MAX_TOTAL_ITERS
        for idx, m in enumerate(plan.milestones):
            if budget <= 0:
                ledger.milestone_done(m.title, False, "plan iteration budget exhausted")
                break
            self.on_milestone(idx, len(plan.milestones), m.title)
            iters = min(m.max_iters, budget)
            ok, note, used = await self._run_milestone(plan, idx, ledger, max_iters=iters)
            budget -= used
            retries = 0
            while not ok and retries < config.MILESTONE_RETRIES and budget > 0:
                retries += 1
                self.on_warn(f"마일스톤 실패({note}) — 재시도 {retries}/{config.MILESTONE_RETRIES}")
                iters = min(m.max_iters, budget)
                ok, note, used = await self._run_milestone(
                    plan, idx, ledger, prev_error=note, max_iters=iters
                )
                budget -= used
            ledger.milestone_done(m.title, ok, note)
            self._log(
                "milestone_finished",
                id=m.id,
                title=m.title,
                outcome="completed" if ok else "failed",
                note=note,
                retries=retries,
                remaining_plan_iterations=budget,
            )
            if not ok:
                self.on_warn(f"마일스톤 '{m.title}' 최종 실패 — 계획을 중단합니다.")
                break
        # 순차 실행이 중단된 경우 사용자에게 후속 단계가 단순 누락된 것이 아니라
        # 의도적으로 미착수 상태임을 명확히 보여 준다.
        for pending in plan.milestones[len(ledger.done):]:
            ledger.milestone_pending(pending.title)
        report = ledger.report()
        self._log("plan_report", report=report)
        self.on_text("\n" + report + "\n")
        self.history.append({"role": "assistant", "content": report})
        return (
            len(ledger.done) == len(plan.milestones)
            and all(ok is True for _title, ok, _note in ledger.done)
        )
