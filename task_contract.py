"""Deterministic safety and completion checks for one agent request.

The language model proposes tool calls; this module decides whether each call is
within the request's working set and tracks the minimum verification workflow.
It deliberately contains no model logic, so a guessed tool name cannot bypass
the policy.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Iterable

import config


# Stop at the first recognised file extension.  The previous broad expression
# could consume "Assets/Foo.cs and Assets/Bar.unity" as one invalid path.
# Accept Korean postpositions immediately after a path (e.g. ``.cs를 읽어``).
# ``\b`` is incorrect here because Python treats Korean letters as word
# characters, so there is no boundary between the final ``s`` and ``를``.
_ASSET_PATH = re.compile(
    r"Assets/[^\r\n]*?\.(?:cs|unity|prefab|mat|json)(?![A-Za-z0-9_.-])", re.I
)
_SCRIPT_PREFIX = "Assets/Scripts/"
_SCENE_PREFIX = "Assets/Scenes/"
_LEVELS_PREFIX = "Assets/StreamingAssets/Levels/"
_LOADER_SCRIPT = "Assets/Scripts/LevelLoader.cs"
_COMPILE_ERROR_SCRIPT = re.compile(
    r"(?P<path>Assets[\\/]+Scripts[\\/]+[^()\r\n\"']+?\.cs)"
    r"\(\s*\d+\s*,\s*\d+\s*\)\s*:\s*error\s+CS\d+",
    re.I,
)
_LEVEL_WORKFLOW = re.compile(
    r"level\s*loader|levelloader|level\s*json|레벨\s*json|"
    r"data[- ]driven|데이터\s*주도|레벨|스테이지|"
    r"Assets/StreamingAssets/Levels/|Assets/Scripts/LevelLoader\.cs",
    re.I,
)
_ACTION_INPUT_WORKFLOW = re.compile(
    r"\bplayer\s*input\b|\bplayerinput\b|\binput\s*action\b|\binputaction\b|"
    r"action\s*map|callback|콜백|액션\s*맵",
    re.I,
)
_INPUT_SIM_WORDS = (
    "플레이 검증", "조작", "입력 테스트", "입력 시뮬레이",
    "keyboard", "send_key", "키 입력",
)
_BEHAVIOUR_WORDS = ("이동", "점프", "move", "movement", "jump")
# Only wording that actually asks for proof of runtime behaviour. A bare
# "확인"/"test" also appears in read-only requests such as
# "PlayerMovement 스크립트 확인해줘", which must not be forced into a Play Mode
# input measurement.
_VERIFICATION_WORDS = ("검증", "verify")
_VERIFICATION_PHRASES = re.compile(
    r"실제로\s*\S{0,12}?\s*(?:되는지|동작|작동|움직)|"
    r"(?:동작|작동|되는지)\s*(?:하는지\s*)?(?:확인|테스트|체크)|"
    r"actually\s+(?:works?|moves?|jumps?)",
    re.I,
)
_SCENE_MUTATIONS = {
    "unity_create_gameobject", "unity_create_gameobjects", "unity_modify_gameobject",
    "unity_delete_gameobject", "unity_add_component", "unity_remove_component",
    "unity_set_component_property",
    "unity_create_material", "unity_create_scene",
}
_SCENE_QUERIES = {"unity_get_hierarchy", "unity_get_gameobject"}


def _normalise_path(value: object) -> str:
    return str(value or "").replace("\\", "/").strip().lstrip("/")


def _successful(result: str) -> bool:
    """Treat only a leading explicit MCP/local ``status=ok`` response as success.

    Some host workflows append a deterministic note after the MCP JSON (notably
    the compile-wait note after ``unity_refresh_assets``), so parsing the entire
    string with ``json.loads`` would incorrectly turn a real success into a
    failure.  ``raw_decode`` keeps the status decision tied to the first JSON
    value while safely ignoring that host-owned suffix.
    """
    try:
        data, _end = json.JSONDecoder().raw_decode(str(result).lstrip())
        return isinstance(data, dict) and data.get("status") == "ok"
    except (TypeError, ValueError, AttributeError):
        return False


def _compile_error_script_paths(result: str) -> set[str]:
    """Return exact project scripts named by compiler errors in a console result."""
    try:
        data, _end = json.JSONDecoder().raw_decode(str(result).lstrip())
    except (TypeError, ValueError, AttributeError):
        return set()

    strings: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    collect(data)
    paths: set[str] = set()
    for text in strings:
        for match in _COMPILE_ERROR_SCRIPT.finditer(text):
            path = re.sub(r"/+", "/", _normalise_path(match.group("path")))
            if path.startswith(_SCRIPT_PREFIX) and path.lower().endswith(".cs"):
                paths.add(path)
    return paths


def _content_digest(content: object) -> str:
    return hashlib.sha1(str(content or "").encode("utf-8")).hexdigest()


def _direct_keyboard_keys(request: str) -> set[str]:
    """Extract explicit keyboard controls that need no PlayerInput wiring."""
    if _ACTION_INPUT_WORKFLOW.search(request):
        return set()
    lowered = request.lower()
    keys: set[str] = set()
    # Same scheme wording that verification.py's _AD_SCHEME accepts, so the
    # measured keys and the enforced script keys cannot disagree.
    if re.search(r"(?<![a-z0-9])(?:a\s*[/,·+]\s*d|d\s*[/,·+]\s*a)(?![a-z0-9])", lowered):
        keys.update({"aKey", "dKey"})
    if re.search(r"(?<![a-z0-9])wasd(?![a-z0-9])", lowered):
        keys.update({"wKey", "aKey", "sKey", "dKey"})
    if "space" in lowered or "스페이스" in lowered:
        keys.add("spaceKey")
    if "방향키" in lowered or "arrow key" in lowered:
        keys.update({"leftArrowKey", "rightArrowKey"})
    return keys


def _method_body(content: str, method: str) -> str:
    """Body of a MonoBehaviour method, delimited by brace depth.

    Scanning to the next member declaration instead would depend on the model's
    line breaks, and a single-line script would fold FixedUpdate's statements
    into Update's body.
    """
    match = re.search(
        rf"\b(?:void|bool|float|int|double|Vector2|Vector3)\s+{method}"
        rf"\s*\([^)]*\)\s*\{{",
        content,
    )
    if not match:
        return ""
    depth, start = 1, match.end()
    for index in range(start, len(content)):
        if content[index] == "{":
            depth += 1
        elif content[index] == "}":
            depth -= 1
            if depth == 0:
                return content[start:index]
    return content[start:]


_LATCH_EXAMPLE = (
    "void Update() {\n"
    "    var keyboard = Keyboard.current;\n"
    "    if (keyboard == null) return;\n"
    "    bool pressed = keyboard.spaceKey.isPressed;\n"
    "    if (pressed && !jumpHeld && isGrounded) jumpRequested = true;\n"
    "    jumpHeld = pressed;\n"
    "}\n"
    "void FixedUpdate() {\n"
    "    if (jumpRequested && isGrounded) {\n"
    "        rb.AddForce(Vector3.up * jumpForce, ForceMode.Impulse);\n"
    "        jumpRequested = false;\n"
    "    }\n"
    "}"
)
_GROUNDING_EXAMPLE = (
    "// Cast from the collider itself so the check cannot be thrown off by where\n"
    "// the scene happens to place the player or the floor:\n"
    "var col = GetComponent<Collider>();\n"
    "isGrounded = Physics.Raycast(col.bounds.center, Vector3.down,\n"
    "                             col.bounds.extents.y + 0.15f);\n"
    "// or detect it from contacts, which needs no geometry at all:\n"
    "void OnCollisionStay(Collision collision) {\n"
    "    foreach (ContactPoint contact in collision.contacts)\n"
    "        if (Vector3.Dot(contact.normal, Vector3.up) > 0.5f) { isGrounded = true; return; }\n"
    "}\n"
    "void OnCollisionExit(Collision collision) { isGrounded = false; }\n"
    "// Rejected: a check decided by an inspector field nothing assigns (an unset\n"
    "// LayerMask is 0 and matches nothing, an unset Transform is null).\n"
    "// Do NOT invent a child ground-check point at a guessed offset. A default\n"
    "// capsule is 2 units tall, so its bottom is at local y = -1; both\n"
    "// Vector3.down * 0.5f and (-height/2 + radius) land INSIDE the capsule and\n"
    "// a short ray from there can never reach the floor."
)
_GROUND_TAG_EXAMPLE = (
    "// A scene created this session defines no Ground tag, and CompareTag throws\n"
    "// at runtime when the tag is undefined. Decide it from the contact normal:\n"
    "void OnCollisionStay(Collision collision) {\n"
    "    foreach (ContactPoint contact in collision.contacts)\n"
    "        if (Vector3.Dot(contact.normal, Vector3.up) > 0.5f) { isGrounded = true; return; }\n"
    "}\n"
    "void OnCollisionExit(Collision collision) { isGrounded = false; }"
)
_MOVEMENT_EXAMPLE = (
    "float axis = 0f;\n"
    "if (keyboard.dKey.isPressed) axis = 1f; else if (keyboard.aKey.isPressed) axis = -1f;\n"
    "rb.linearVelocity = new Vector3(axis * moveSpeed, rb.linearVelocity.y, 0f);"
)
# Concrete code for each gap, keyed by its label. The repair path already hands
# out code like this and landed a working script on its first attempt, while
# this gate handed out bare labels and took fourteen. The label says what is
# missing; only the snippet says what to write.
_GAP_REMEDIES: dict[str, str] = {
    "using UnityEngine.InputSystem": "using UnityEngine.InputSystem;",
    "Keyboard.current": (
        "var keyboard = Keyboard.current;\nif (keyboard == null) return;"
    ),
    "Rigidbody.linearVelocity": _MOVEMENT_EXAMPLE,
    "Update read of spaceKey.isPressed": _LATCH_EXAMPLE,
    "Update jumpRequested latch": _LATCH_EXAMPLE,
    "Update rising-edge guard (pressed && !jumpHeld ... jumpHeld = pressed)": _LATCH_EXAMPLE,
    "FixedUpdate jumpRequested consumption": _LATCH_EXAMPLE,
    "self-contained ground check": _GROUNDING_EXAMPLE,
    'no CompareTag("Ground")': _GROUND_TAG_EXAMPLE,
    "no legacy UnityEngine.Input API": (
        "// Replace Input.GetAxis/GetKey/GetButtonDown entirely:\n"
        + _MOVEMENT_EXAMPLE
    ),
}
_GROUND_QUERIES = r"Raycast|CheckSphere|CheckBox|CheckCapsule|OverlapSphere|SphereCast|BoxCast"
_INSPECTOR_FIELD = re.compile(
    r"^[^\S\n]*(?:\[SerializeField\][^\n]*?)?"
    r"(?:public|private|protected|internal)\s+(LayerMask|Transform)\s+(\w+)\s*(=|;)",
    re.M,
)


def _grounding_gaps(content: str) -> list[str]:
    """Reject only ground checks that provably cannot succeed.

    v1.11.9 required the literal `.bounds` or an `OnCollision*` method, which
    rejected `Physics.Raycast(transform.position, Vector3.down, 1.5f)` — correct,
    self-contained code — fourteen times in one run and pushed the model into
    renaming and shrinking the script to escape the gate. The property that
    actually matters is whether the check depends on inspector state, because
    nothing in this pipeline assigns it: an unset LayerMask is 0 and matches no
    layer, an unset Transform is null. Behaviour beyond that is the verification
    layer's job, not a static gate's.
    """
    gaps: list[str] = []
    has_check = bool(
        re.search(_GROUND_QUERIES, content, re.I)
        or "oncollision" in content.lower()
        or ".bounds" in content.lower()
    )
    if not has_check:
        return ["self-contained ground check"]
    for match in _INSPECTOR_FIELD.finditer(content):
        kind, field = match.group(1), match.group(2)
        if match.group(3) == "=":
            continue
        used_by_check = re.search(
            rf"(?:{_GROUND_QUERIES})[^;]*\b{re.escape(field)}\b", content, re.I
        ) or re.search(rf"\b{re.escape(field)}\s*\.\s*position", content)
        if not used_by_check:
            continue
        guarded = re.search(
            rf"\b{re.escape(field)}\s*(?:=(?!=)|[!=]=\s*null|\.value)", content
        )
        if guarded:
            continue
        gaps.append(
            f"self-contained ground check ({kind} {field} is never assigned, "
            "so the check can never succeed)"
        )
    return gaps


def _frame_body(content: str, method: str, depth: int = 2) -> str:
    """Code that runs each frame from `method`, including helpers it calls.

    `void Update() { HandleInput(); CheckGrounded(); }` runs the same code as an
    inline Update and is better structured, but reading only the literal body
    reports every latch requirement as missing. The property under test is what
    executes per frame, not which method the statements are typed into.
    """
    seen: set[str] = {method}
    body = _method_body(content, method)
    collected = [body]
    frontier = [body]
    for _ in range(depth):
        following: list[str] = []
        for chunk in frontier:
            for callee in set(re.findall(r"\b([A-Z]\w*)\s*\(\s*\)", chunk)):
                if callee in seen:
                    continue
                seen.add(callee)
                nested = _method_body(content, callee)
                if nested:
                    collected.append(nested)
                    following.append(nested)
        if not following:
            break
        frontier = following
    return "\n".join(collected)


def _jump_latch_gaps(content: str) -> list[str]:
    """Require an explicit rising edge around the jump latch.

    Both obvious spellings are wrong here. `wasPressedThisFrame` can have its
    edge consumed before gameplay Update runs, because the bridge re-queues a
    held key every editor tick. A bare `isPressed` latch has the opposite
    problem: it re-arms on every frame the key is down, so FixedUpdate applies
    one impulse per physics step and the launch velocity stacks. Comparing
    against the previous frame's state is the only form both agree on.
    """
    update_body = _frame_body(content, "Update")
    fixed_body = _frame_body(content, "FixedUpdate")
    gaps: list[str] = []
    if "spaceKey.isPressed" not in update_body:
        gaps.append("Update read of spaceKey.isPressed")
    if not re.search(r"\bjumpRequested\s*=\s*true\b", update_body):
        gaps.append("Update jumpRequested latch")
    # A rising edge needs a negated flag that Update itself writes back. That
    # write-back is what separates `!jumpHeld` from an unrelated `!isGrounded`
    # whose only assignment lives in a collision callback.
    has_edge = any(
        candidate != "jumpRequested"
        and re.search(rf"\b{re.escape(candidate)}\s*=(?!=)", update_body)
        for candidate in set(re.findall(r"!\s*(\w+)", update_body))
    )
    if not has_edge:
        gaps.append(
            "Update rising-edge guard (pressed && !jumpHeld ... jumpHeld = pressed)"
        )
    if not re.search(r"\bjumpRequested\s*=\s*false\b", fixed_body):
        gaps.append("FixedUpdate jumpRequested consumption")
    return gaps


@dataclass
class TaskContract:
    """Per-request guardrails and machine-checkable verification milestones."""

    user_paths: set[str] = field(default_factory=set)
    session_scripts: set[str] = field(default_factory=set)
    written_scripts: set[str] = field(default_factory=set)
    refreshed_after_write: bool = False
    compile_checked: bool = False
    played: bool = False
    waited_after_play: bool = False
    runtime_checked: bool = False
    stopped_after_play: bool = False
    scene_verification_pending: bool = False
    scene_save_pending: bool = False
    require_screenshot: bool = False
    screenshot_taken: bool = False
    levels_written: set[str] = field(default_factory=set)
    level_runtime_verified: bool = False
    require_play: bool = False
    require_input_sim: bool = False
    input_sent: bool = False
    player_position_observed: bool = False
    input_sent_after_observation: bool = False
    input_effect_checked: bool = False
    player_position_before: tuple[float, float, float] | None = None
    player_position_after: tuple[float, float, float] | None = None
    input_movement_verified: bool = False
    level_load_marker_seen: bool = False
    active_scene_path: str | None = None
    allow_level_workflow: bool = False
    compile_error_scripts: set[str] = field(default_factory=set)
    direct_keyboard_keys: set[str] = field(default_factory=set)
    fresh_scene_requested: bool = False
    blocked_input_writes: int = 0
    last_written_content: dict[str, str] = field(default_factory=dict)

    def _input_script_block_message(self, path: str, gaps: list[str]) -> str:
        """Name every gap, but show code for the ones to fix first.

        A flat list of nine labels tells the model nothing about where to start;
        that is what turned one unsatisfied check into eight renamed, shrinking
        scripts. Labels stay complete so the model can see the whole picture,
        and the two leading gaps carry the exact code that closes them.
        """
        required = ", ".join(sorted(self.direct_keyboard_keys))
        remedies: list[str] = []
        for gap in gaps:
            snippet = next(
                (code for key, code in _GAP_REMEDIES.items() if gap.startswith(key)),
                None,
            )
            if snippet and snippet not in remedies:
                remedies.append(snippet)
            if len(remedies) == 2:
                break
        # Never print the "fix it with this shape" header with nothing under it.
        # A gap whose label had no snippet produced exactly that, which is the
        # bare-label failure this message exists to end.
        fix_first = ""
        if remedies:
            fix_first = (
                "Fix the first item with exactly this shape:\n"
                + "\n\n".join(remedies)
                + "\n"
            )
        repeated = ""
        if self.blocked_input_writes >= 2:
            repeated = (
                f"\n\nThis is block #{self.blocked_input_writes} for this request. "
                "The same policy applies to every player/movement/controller script, "
                "so renaming the file or simplifying it away will not pass. Fix only "
                "the first item above, keep everything else identical, and write again."
            )
        return (
            f"Policy blocked event-only input script {path}: this request names simple "
            f"keyboard controls ({required}), so read Keyboard.current directly and "
            f"include every requested key.\n"
            f"Missing: {', '.join(gaps)}.\n"
            f"{fix_first}"
            "Do not use the legacy UnityEngine.Input API, and do not create PlayerInput, "
            "InputActionAsset, generated controls, OnMove/OnJump callback wiring, or a "
            "separate input handler unless the user explicitly requested that "
            f"architecture.{repeated}"
        )

    @classmethod
    def from_request(cls, request: str, known_scripts: Iterable[str] = ()) -> "TaskContract":
        request_lower = request.lower()
        require_input_sim = (
            any(word in request_lower for word in _INPUT_SIM_WORDS)
            or (
                any(word in request_lower for word in _BEHAVIOUR_WORDS)
                and (
                    any(word in request_lower for word in _VERIFICATION_WORDS)
                    or bool(_VERIFICATION_PHRASES.search(request))
                )
            )
        )
        return cls(
            user_paths={_normalise_path(m.group(0)) for m in _ASSET_PATH.finditer(request)},
            session_scripts=set(known_scripts),
            require_screenshot=any(word in request_lower for word in ("screenshot", "스크린샷", "capture", "캡처")),
            require_play=require_input_sim,
            require_input_sim=require_input_sim,
            allow_level_workflow=bool(_LEVEL_WORKFLOW.search(request)),
            direct_keyboard_keys=_direct_keyboard_keys(request),
            fresh_scene_requested=any(
                phrase in request_lower
                for phrase in ("새 빈 씬", "새 씬", "new empty scene", "new scene")
            ),
        )

    @classmethod
    def for_milestone(cls, milestone, known_scripts: Iterable[str] = ()) -> "TaskContract":
        """플랜 실행 시 마일스톤별 계약. goal 텍스트 + deliverables + verify를 반영한다."""
        contract = cls.from_request(milestone.goal, known_scripts)
        contract.user_paths |= {_normalise_path(d) for d in milestone.deliverables}
        verify = set(milestone.verify)
        contract.require_screenshot |= "screenshot" in verify
        contract.require_play |= bool({"play", "input"} & verify)
        contract.require_input_sim |= "input" in verify
        return contract

    def prepare_call(self, name: str, args: dict) -> tuple[dict, str | None]:
        """Return sanitised arguments or a policy error before executing a tool."""
        args = dict(args or {})

        # Menu automation is nondeterministic and can open native dialogs.  The
        # bridge should expose a purpose-built MCP command instead.
        if name == "unity_execute_menu_item":
            return args, (
                "Policy blocked unity_execute_menu_item: use a dedicated MCP tool "
                "such as unity_create_scene or unity_save_scene instead."
            )

        if name in {"unity_install_level_loader", "unity_write_level", "unity_read_level"}:
            if not self.allow_level_workflow:
                return args, (
                    f"Policy blocked {name}: the user did not request a level/stage, "
                    "LevelLoader, level JSON, or a data-driven level workflow."
                )

        if name == "unity_list_assets" and "t:script" in str(args.get("filter", "")).lower():
            # Packages can contain thousands of scripts and contaminate a small
            # local model's context.  Project scripts are the useful default.
            args.setdefault("folder", "Assets/Scripts")
            args.setdefault("limit", 30)

        if name in {"unity_write_script", "unity_read_script", "unity_delete_script"}:
            path = _normalise_path(args.get("path"))
            args["path"] = path
            if not path.startswith(_SCRIPT_PREFIX) or not path.lower().endswith(".cs"):
                return args, "Policy blocked script access: scripts must be under Assets/Scripts/ and end in .cs."
            if (
                name == "unity_write_script"
                and self.last_written_content.get(path)
                == _content_digest(args.get("content"))
            ):
                # Rewriting the bytes already on disk changes nothing but costs a
                # compile cycle and a model turn. One repair loop spent four of
                # its five writes resending an identical file and then reported
                # no_verification_progress.
                return args, (
                    f"Policy blocked {name} for {path}: this content is byte-identical "
                    "to what is already on disk, so writing it cannot change the "
                    "verification result. The reported failure is still present in "
                    "this exact code — change the implementation before writing again."
                )
            if name == "unity_write_script" and self.direct_keyboard_keys:
                filename = path.rsplit("/", 1)[-1].lower()
                input_behaviour = any(
                    marker in filename
                    for marker in ("player", "movement", "controller", "input", "character")
                )
                if input_behaviour:
                    # The gate reports what is wrong and lets the model rewrite
                    # it. Editing the content here would silently make the
                    # harness the author, so a receipt could no longer say
                    # whether the local model produced a working script.
                    content = str(args.get("content", ""))
                    lowered_content = content.lower()
                    missing_keys = sorted(
                        key for key in self.direct_keyboard_keys
                        if key.lower() not in lowered_content
                    )
                    missing_patterns: list[str] = []
                    if "unityengine.inputsystem" not in lowered_content:
                        missing_patterns.append("using UnityEngine.InputSystem")
                    if "keyboard.current" not in lowered_content:
                        missing_patterns.append("Keyboard.current")
                    if "linearvelocity" not in lowered_content:
                        missing_patterns.append("Rigidbody.linearVelocity")
                    if "spaceKey" in self.direct_keyboard_keys:
                        missing_patterns.extend(_jump_latch_gaps(content))
                        if self.fresh_scene_requested and re.search(
                            r"CompareTag\s*\(\s*\"Ground\"", content, re.I
                        ):
                            # A scene created in this session defines no custom
                            # Ground tag, and CompareTag throws at runtime when
                            # the tag is undefined.
                            missing_patterns.append(
                                'no CompareTag("Ground") in a scene created this '
                                "session (use contact.normal instead)"
                            )
                        missing_patterns.extend(_grounding_gaps(content))
                    if re.search(r"\b(?:unityengine\.)?input\.", lowered_content):
                        missing_patterns.append("no legacy UnityEngine.Input API")
                    if missing_keys or missing_patterns:
                        gaps = missing_patterns + [
                            f"missing key {key}" for key in missing_keys
                        ]
                        self.blocked_input_writes += 1
                        return args, self._input_script_block_message(path, gaps)
            unscoped_existing = path not in self.user_paths | self.session_scripts
            compiler_proven = path in self.compile_error_scripts
            if name == "unity_delete_script" and unscoped_existing and not compiler_proven:
                return args, (
                    f"Policy blocked {name} for {path}: the user did not explicitly scope this existing script. "
                    "Only scripts created in this session, an Assets/... path named by the user, "
                    "or an exact script path reported by the current compiler errors may be deleted."
                )
            if (
                name == "unity_read_script"
                and unscoped_existing
                and not config.ALLOW_UNSCOPED_SCRIPT_READ
            ):
                return args, (
                    f"Policy blocked {name} for {path}: the user did not explicitly scope this existing script. "
                    "Set UNITY_AGENT_ALLOW_UNSCOPED_SCRIPT_READ=1 to allow read-only inspection; "
                    "delete access remains scoped."
                )

        if name == "unity_add_component" and self.direct_keyboard_keys:
            component_name = str(args.get("component_type", "")).rsplit(".", 1)[-1]
            component = component_name.lower()
            if component == "playerinput":
                return args, (
                    "Policy blocked PlayerInput for explicit simple keyboard controls: "
                    "read Keyboard.current and the requested keys directly in the player "
                    "behaviour. PlayerInput/action callback wiring was not requested."
                )
            if (
                self.fresh_scene_requested
                and component in {"playermovement", "playercontroller", "playerinputhandler"}
                and f"Assets/Scripts/{component_name}.cs" not in self.session_scripts
            ):
                return args, (
                    f"Policy blocked reuse of {component_name} in a fresh keyboard scene: "
                    f"write Assets/Scripts/{component_name}.cs in this session first so "
                    "the direct-key, jump-latch, physics, and compile checks apply to the "
                    "actual script being attached."
                )

        if name in {"unity_create_scene", "unity_open_scene", "unity_save_scene"}:
            path = _normalise_path(args.get("path"))
            if path:
                args["path"] = path
            if name == "unity_save_scene" and not path:
                if self.active_scene_path:
                    path = self.active_scene_path
                    args["path"] = path
                else:
                    return args, (
                        "Policy blocked pathless scene save: call unity_get_state first so the host "
                        "can resolve the active Assets/Scenes/*.unity path without a native dialog."
                    )
            if not path.startswith(_SCENE_PREFIX) or not path.lower().endswith(".unity"):
                return args, "Policy blocked scene creation: use an Assets/Scenes/*.unity path."
            scoped_scenes = {
                item for item in self.user_paths if item.lower().endswith(".unity")
            }
            if scoped_scenes and path not in scoped_scenes:
                return args, (
                    f"Policy blocked {name} for {path}: canonical scene target is "
                    + ", ".join(sorted(scoped_scenes))
                )

        if name in {"unity_write_level", "unity_read_level"}:
            path = _normalise_path(args.get("path"))
            args["path"] = path
            if not path.startswith(_LEVELS_PREFIX) or not path.lower().endswith(".json"):
                return args, (
                    "Policy blocked level access: level files must be under "
                    "Assets/StreamingAssets/Levels/ and end in .json."
                )

        if name == "unity_send_key" and not self.played:
            return args, (
                'Policy blocked unity_send_key: enter play mode first with unity_play_mode action="play".'
            )

        return args, None

    def observe(self, name: str, args: dict, result: str) -> None:
        """Update milestones only after a successful tool response."""
        if not _successful(result):
            return
        if name == "unity_get_state":
            try:
                data, _ = json.JSONDecoder().raw_decode(str(result).lstrip())
                path = _normalise_path(
                    data.get("result", {}).get("activeScene", {}).get("path", "")
                )
                if path.startswith(_SCENE_PREFIX) and path.lower().endswith(".unity"):
                    self.active_scene_path = path
            except (TypeError, ValueError, AttributeError):
                pass
        if name in _SCENE_MUTATIONS:
            self.scene_verification_pending = True
            if name != "unity_create_scene":
                self.scene_save_pending = True
        elif name in _SCENE_QUERIES:
            self.scene_verification_pending = False
        elif name == "unity_save_scene":
            self.scene_save_pending = False
        elif name == "unity_screenshot":
            self.screenshot_taken = True

        if name == "unity_write_script":
            path = _normalise_path(args.get("path"))
            self.written_scripts.add(path)
            self.session_scripts.add(path)
            self.last_written_content[path] = _content_digest(args.get("content"))
            self.refreshed_after_write = False
            self.compile_checked = False
        elif name == "unity_delete_script":
            path = _normalise_path(args.get("path"))
            self.compile_error_scripts.discard(path)
            self.session_scripts.discard(path)
        elif name == "unity_install_level_loader":
            # 로더 설치는 스크립트 쓰기와 동일하게 컴파일 검증 사이클을 요구한다.
            self.written_scripts.add(_LOADER_SCRIPT)
            self.session_scripts.add(_LOADER_SCRIPT)
            self.refreshed_after_write = False
            self.compile_checked = False
        elif name == "unity_write_level":
            self.levels_written.add(_normalise_path(args.get("path")))
            self.level_runtime_verified = False
            self.level_load_marker_seen = False
        elif name == "unity_send_key":
            self.input_sent = True
            if self.player_position_before is not None:
                self.input_sent_after_observation = True
                self.input_effect_checked = False
        elif name == "unity_get_gameobject" and str(args.get("target", "")).strip().lower() == "player":
            position = self._player_position(result)
            if position is not None:
                self.player_position_observed = True
                if self.input_sent_after_observation and self.player_position_before is not None:
                    self.player_position_after = position
                    self.input_effect_checked = True
                    self.input_movement_verified = any(
                        abs(after - before) > 1e-3
                        for before, after in zip(self.player_position_before, position)
                    )
                    if not self.input_movement_verified:
                        # The unchanged position becomes a new baseline so the
                        # agent can fix the implementation and retry input.
                        self.player_position_before = position
                else:
                    self.player_position_before = position
        elif name == "unity_refresh_assets" and self.written_scripts:
            self.refreshed_after_write = True
        elif name == "unity_read_console":
            requested_types = str(args.get("types", "")).lower()
            if not requested_types or "error" in requested_types or "exception" in requested_types:
                self.compile_error_scripts.update(_compile_error_script_paths(result))
            if "[LevelLoader] Loaded" in result:
                self.level_load_marker_seen = True
            if not requested_types or "error" in requested_types or "exception" in requested_types:
                if self.refreshed_after_write:
                    self.compile_checked = True
                if self.played and self.waited_after_play:
                    self.runtime_checked = True
                    self.level_runtime_verified = self.level_load_marker_seen
        elif name == "unity_play_mode":
            action = str(args.get("action", "")).lower()
            if action == "play":
                self.played = True
                self.waited_after_play = False
                self.runtime_checked = False
                self.stopped_after_play = False
            elif action == "stop" and self.played:
                self.stopped_after_play = True
        elif name == "unity_wait" and self.played:
            self.waited_after_play = True

    def missing_verification(self) -> list[str]:
        missing: list[str] = []
        if self.written_scripts:
            if not self.refreshed_after_write:
                missing.append("call unity_refresh_assets after writing the script")
            elif not self.compile_checked:
                missing.append('check compilation with unity_read_console types="error,exception"')
        if self.levels_written and not self.level_runtime_verified:
            missing.append(
                'verify the level at runtime: unity_play_mode action="play", unity_wait, then '
                "unity_read_console and confirm a '[LevelLoader] Loaded' entry with no errors"
            )
        # Persist edit-mode changes before asking for play mode. Unity rejects
        # scene saves while playing, so this ordering is part of the deterministic
        # workflow guidance rather than cosmetic message ordering.
        if self.scene_verification_pending:
            missing.append("verify the latest scene change with unity_get_gameobject or unity_get_hierarchy")
        if self.scene_save_pending:
            missing.append("persist scene changes with unity_save_scene before entering play mode")
        if self.require_play and not self.played:
            missing.append('enter play mode with unity_play_mode action="play" to verify the game runs')
        if self.require_input_sim and self.played:
            if not self.player_position_observed:
                missing.append(
                    "get the Player position with unity_get_gameobject before simulated input"
                )
            elif not self.input_sent_after_observation:
                missing.append(
                    "simulate gameplay input with unity_send_key after recording the Player position"
                )
            elif not self.input_effect_checked:
                missing.append(
                    "get the Player again after unity_send_key and compare the before/after positions"
                )
            elif not self.input_movement_verified:
                missing.append(
                    "Player position did not change after input; fix the 3D Rigidbody/PlayerMovement setup, "
                    "then record the Player position and repeat unity_send_key plus the after-position check"
                )
        if self.played:
            if not self.waited_after_play:
                missing.append("wait 0.5 to 10 seconds with unity_wait after entering play mode")
            if not self.runtime_checked:
                missing.append('check runtime errors with unity_read_console types="error,exception" after play mode')
            if not self.stopped_after_play:
                missing.append("stop play mode with unity_play_mode action=\"stop\"")
        if self.require_screenshot and self.played and not self.screenshot_taken:
            missing.append("capture the running game with unity_screenshot before completion")
        return missing

    @staticmethod
    def _player_position(result: str) -> tuple[float, float, float] | None:
        """Extract a successful Player world position from an MCP result."""
        try:
            data, _end = json.JSONDecoder().raw_decode(str(result).lstrip())
            position = data["result"]["transform"]["position"]
            if not isinstance(position, list) or len(position) != 3:
                return None
            return tuple(float(value) for value in position)
        except (TypeError, ValueError, KeyError, IndexError, AttributeError):
            return None
