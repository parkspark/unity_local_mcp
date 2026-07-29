"""v1.9 host-owned Unity verification specifications, evidence and receipts.

The builder model may propose work, but it cannot mark that work complete.  A
host-selected read/play/input sequence turns real Unity JSON results into
deterministic pass/fail evidence.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Iterable

import config
from preflight import inspect_request
from policy_lint import lint_scripts
from version import __version__

VERSION = __version__
_BUILD_WORDS = (
    "만들", "제작", "구현", "생성", "수정", "개선", "업데이트", "추가", "삭제",
    # Common Korean repair verbs. Without these a behaviour bug report like
    # "부스트가 동작하지 않는다. 고쳐줘" skipped host verification entirely and
    # left the model free to declare success on its own.
    "고치", "고쳐", "해결",
    "build", "create", "make", "implement", "update", "fix", "add", "remove",
    "repair",
)
_GAME_WORDS = ("게임", "플랫포머", "횡스크롤", "platformer", "side-scroller", "game")
# Behaviour verbs are strong signals on their own. "Space 점프가 동작하지 않는다.
# 수정하고 검증한다" must enable jump measurement even though it never says 게임 —
# gating these on a game keyword is what allowed an empty spec to report success.
_MOVEMENT_WORDS = ("이동", "움직", "걷", "달리", "movement", "walking", "running")
_MOVEMENT_CONTEXT_WORDS = ("플랫포머", "횡스크롤", "플레이어", "player")
_JUMP_WORDS = ("점프", "jump", "jumping", "착지", "landing")
_JUMP_CONTEXT_WORDS = ("플랫포머", "platformer")
_CAMERA_WORDS = ("카메라", "camera")
_CAMERA_FOLLOW_WORDS = ("추종", "따라", "follow", "following")
_LEVEL_WORDS = ("levelloader", "level json", "레벨 json", "데이터 주도", "data-driven")
_BOOST_WORDS = ("부스트", "boost", "dash", "대시")
_BOOST_CONTEXT_WORDS = ("shift",)
_LANDING_WORDS = ("착지", "land", "landing")
# Control scheme named in the request. "A/D 좌우 이동" must be verified with A/D,
# not with the harness's default arrow keys.
_AD_SCHEME = re.compile(r"\ba\s*[/,·+]\s*d\b|\bd\s*[/,·+]\s*a\b|\bwasd\b", re.I)
_ARROW_SCHEME = re.compile(r"방향키|화살표|arrow\s*keys?", re.I)
# Jump key named in the request. Same class of bug as the movement scheme:
# testing space against a game bound to W fails it for the harness's assumption.
_JUMP_KEY_SCHEMES = (
    (re.compile(r"\bw\s*(?:키|key)?\s*(?:로|으로)?\s*(?:점프|jump)", re.I), "w"),
    (re.compile(r"(?:위쪽\s*방향키|up\s*arrow|uparrow)\s*(?:키|key)?\s*(?:로|으로)?\s*(?:점프|jump)", re.I), "upArrow"),
)
# Runtime-behaviour language that demands Play Mode proof. If a request uses any
# of these but no concrete check could be derived, the host refuses to report
# success instead of silently verifying nothing (verification_spec_empty).
_BEHAVIOUR_HINT_WORDS = (
    "점프", "jump", "이동", "움직", "걷", "달리", "착지", "land", "landing",
    "부스트", "boost", "대시", "dash", "추종", "follow", "조작", "입력",
    "플레이", "play", "동작", "작동", "실행", "물리", "충돌", "collision",
    "gameplay", "runtime",
)

MUTATION_TOOLS = {
    "unity_create_gameobject", "unity_create_gameobjects", "unity_modify_gameobject",
    "unity_delete_gameobject", "unity_add_component", "unity_remove_component",
    "unity_set_component_property", "unity_create_material", "unity_create_scene",
    "unity_open_scene", "unity_save_scene", "unity_refresh_assets", "unity_write_script",
    "unity_delete_script", "unity_install_level_loader", "unity_write_level",
    "unity_execute_menu_item",
}


def _has_word(lower: str, words: Iterable[str]) -> bool:
    """Match Korean by substring and Latin on latin-letter boundaries.

    Korean has no word delimiters, so substring matching is correct there.
    Latin needs a boundary or "remove" would match "move" and every deletion
    request would demand a movement measurement. `\\b` is wrong here because
    Hangul counts as a word character, so "Main Camera는" would not match
    "camera" — the boundary must be defined against latin characters only.
    """
    for word in words:
        if word.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", lower):
                return True
        elif word in lower:
            return True
    return False


def _decode(result: str) -> dict | None:
    try:
        value, _ = json.JSONDecoder().raw_decode(str(result).lstrip())
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError, AttributeError):
        return None


def _ok(result: str) -> dict | None:
    value = _decode(result)
    if value and value.get("status") == "ok" and isinstance(value.get("result"), dict):
        return value["result"]
    return None


def _position(value: dict) -> tuple[float, float, float] | None:
    try:
        raw = value["transform"]["position"]
        if not isinstance(raw, list) or len(raw) != 3:
            return None
        return tuple(float(item) for item in raw)
    except (KeyError, TypeError, ValueError):
        return None


def _normalise_path(value: str) -> str:
    return str(value or "").replace("\\", "/").lstrip("/")


def _compact_entries(entries: list, limit: int = 20) -> list:
    """Keep receipts useful when one runtime error repeats every physics tick."""
    compact: list = []
    seen: set[str] = set()
    for entry in entries:
        key = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        compact.append(entry)
        if len(compact) >= limit:
            break
    return compact


@dataclass
class VerificationSpec:
    request: str
    enabled: bool
    asset_paths: list[str] = field(default_factory=list)
    scene_path: str | None = None
    # The request used runtime-behaviour language, whether or not a concrete
    # check could be derived from it.
    behaviour_requested: bool = False
    require_gameplay: bool = False
    require_movement: bool = False
    require_jump: bool = False
    require_camera_follow: bool = False
    require_camera_fixed_z: bool = False
    require_camera_target: bool = False
    require_player_constraints: bool = False
    require_boost: bool = False
    require_bidirectional: bool = False
    require_level_marker: bool = False
    require_screenshot: bool = False
    require_idle_stability: bool = False
    require_jump_landing: bool = False
    require_left_boost: bool = False
    idle_duration: float = 0.5
    idle_max_delta_x: float = 0.05
    movement_duration: float = 1.0
    movement_min_distance: float = 2.0
    # Upper sanity bound. A repeated AddForce(..., Impulse) in FixedUpdate sends
    # the player 100+ units per second, which passed a min-distance-only check
    # while being obviously broken physics.
    movement_max_speed: float = 25.0
    # Which keys the request asked for. Verifying rightArrow against a script
    # that only reads A/D fails the game for the harness's own assumption.
    move_right_key: str = "rightArrow"
    move_left_key: str = "leftArrow"
    movement_keys_explicit: bool = False
    jump_key: str = "space"
    boost_duration: float = 0.5
    boost_min_ratio: float = 1.4
    jump_min_rise: float = 0.5
    required_components: dict[str, list[str]] = field(default_factory=dict)

    def movement_max_distance(self, duration: float | None = None) -> float:
        return self.movement_max_speed * (
            self.movement_duration if duration is None else duration
        )

    @classmethod
    def from_request(cls, request: str, force: bool = False) -> "VerificationSpec":
        lower = request.lower()
        preflight = inspect_request(request, config.SCENE_PATH_POLICY)
        assets = preflight.asset_paths
        game = _has_word(lower, _GAME_WORDS)
        # Behaviour words stand alone; entity words ("플레이어") still need game
        # context so that "Player 이름을 바꿔줘" does not demand a Play Mode run.
        movement = _has_word(lower, _MOVEMENT_WORDS) or (
            game and _has_word(lower, _MOVEMENT_CONTEXT_WORDS)
        )
        jump = _has_word(lower, _JUMP_WORDS) or (
            game and _has_word(lower, _JUMP_CONTEXT_WORDS)
        )
        # A camera merely mentioned in a non-game request should not trigger an
        # input measurement. Games, or explicit follow/tracking wording, do.
        camera = _has_word(lower, _CAMERA_WORDS) and (
            game or _has_word(lower, _CAMERA_FOLLOW_WORDS)
        )
        boost = _has_word(lower, _BOOST_WORDS) or (
            movement and _has_word(lower, _BOOST_CONTEXT_WORDS)
        )
        # Boost is measured as a ratio against normal movement, so it implies it.
        movement = movement or boost
        level = _has_word(lower, _LEVEL_WORDS)
        script_classes = [os.path.splitext(os.path.basename(path))[0] for path in assets
                          if path.lower().endswith(".cs")]
        components: dict[str, list[str]] = {}
        if movement or jump:
            components["Player"] = ["Rigidbody", "Collider"]
            player_class = next((name for name in script_classes
                                 if "player" in name.lower() and "movement" in name.lower()), None)
            if player_class:
                components["Player"].append(player_class)
            elif "playermovement" in lower:
                # Preserve the older generic convention when no exact script
                # path was supplied in the request.
                components["Player"].append("PlayerMovement")
        if camera:
            components["Main Camera"] = ["Camera"]
            camera_class = next((name for name in script_classes if "camera" in name.lower()), None)
            if camera_class:
                components["Main Camera"].append(camera_class)
            elif "sidescrollercamera" in lower:
                components["Main Camera"].append("SideScrollerCamera")
        # Any extracted behaviour condition implies Play Mode: a check that is
        # never run must never count as passed.
        behaviour = movement or jump or camera or boost
        if _AD_SCHEME.search(request):
            right_key, left_key, keys_explicit = "d", "a", True
        elif _ARROW_SCHEME.search(request):
            right_key, left_key, keys_explicit = "rightArrow", "leftArrow", True
        else:
            right_key, left_key, keys_explicit = "rightArrow", "leftArrow", False
        jump_key = next(
            (key for pattern, key in _JUMP_KEY_SCHEMES if pattern.search(request)),
            "space",
        )
        return cls(
            request=preflight.normalized_request,
            enabled=force or _has_word(lower, _BUILD_WORDS),
            asset_paths=assets,
            scene_path=preflight.canonical_scene_path,
            behaviour_requested=_has_word(lower, _BEHAVIOUR_HINT_WORDS),
            require_gameplay=game or level or behaviour,
            require_movement=movement,
            require_jump=jump,
            require_camera_follow=camera,
            require_camera_fixed_z=camera and (
                "z는 고정" in lower or "z 고정" in lower or "fixed z" in lower
            ),
            require_camera_target=camera and (
                "target이 null이 아니" in lower or "target is not null" in lower
            ),
            require_player_constraints=movement and (
                "z 이동과 회전을 고정" in lower
                or "z position and rotation" in lower
            ),
            require_boost=boost,
            require_bidirectional=movement and bool(
                re.search(r"\ba\b", lower) and re.search(r"\bd\b", lower)
            ),
            require_level_marker=level,
            require_screenshot=game,
            require_idle_stability="무입력 0.5초" in lower or "idle 0.5" in lower,
            # Landing is measured as part of the jump arc, so it must not be
            # requested without a jump measurement to attach it to.
            require_jump_landing=jump and _has_word(lower, _LANDING_WORDS),
            move_right_key=right_key,
            move_left_key=left_key,
            movement_keys_explicit=keys_explicit,
            jump_key=jump_key,
            require_left_boost="a+leftshift" in lower.replace(" ", ""),
            required_components=components,
        )

    # Canonical check names shared by the receipt and the empty-spec guard.
    # (name, is-requested) — evaluated in a fixed order so receipts diff cleanly.
    def requested_checks(self) -> list[str]:
        flags = [
            ("gameplay", self.require_gameplay),
            ("movement", self.require_movement),
            ("bidirectional", self.require_bidirectional),
            ("idle_stability", self.require_idle_stability),
            ("jump", self.require_jump),
            ("jump_landing", self.require_jump_landing),
            ("camera_follow", self.require_camera_follow),
            ("camera_fixed_z", self.require_camera_fixed_z),
            ("camera_target", self.require_camera_target),
            ("player_constraints", self.require_player_constraints),
            ("boost", self.require_boost),
            ("left_boost", self.require_left_boost),
            ("level_marker", self.require_level_marker),
            ("screenshot", self.require_screenshot),
        ]
        names = [name for name, enabled in flags if enabled]
        names.extend(f"components:{target}" for target in sorted(self.required_components))
        return names

    def behaviour_checks(self) -> list[str]:
        """Requested checks that need an actual Play Mode measurement."""
        static = {"components", "level_marker", "screenshot"}
        return [
            name for name in self.requested_checks()
            if name.split(":")[0] not in static
        ]

    def checklist(self) -> list[str]:
        checks = [
            "요청에 명시된 Assets 파일이 실제 디스크에 존재",
            "unity_get_state: 활성 씬이 저장됐고(isDirty=false) Play Mode가 아님",
            "unity_read_console types=error,exception: 컴파일 오류 0건",
        ]
        for target, components in self.required_components.items():
            checks.append(f"{target} 컴포넌트 포함: {', '.join(components)}")
        if self.require_gameplay:
            checks.append("Play 진입 후 unity_wait, 런타임 오류 0건")
        if self.require_level_marker:
            checks.append("런타임 콘솔에 [LevelLoader] Loaded 마커")
        if self.require_movement:
            checks.append(
                f"{self.move_right_key} 입력 전후 Player X가 실제로 증가하되 "
                f"{self.movement_max_distance():.0f} 이내"
            )
        if self.require_idle_stability:
            checks.append(
                f"무입력 {self.idle_duration}초 Player X 변화가 "
                f"{self.idle_max_delta_x} 이하"
            )
        if self.require_jump:
            checks.append(f"{self.jump_key} 입력 전후 Player Y가 실제로 증가")
        if self.require_camera_follow:
            checks.append("Player 이동과 같은 구간에 Main Camera X가 실제로 증가")
        if self.require_camera_fixed_z:
            checks.append("Player 이동 중 Main Camera Z 변화가 0.05 이하")
        if self.require_camera_target:
            checks.append("Play 진입 전 SideScrollerCamera.target이 null이 아님")
        if self.require_player_constraints:
            checks.append("Rigidbody가 Z 위치와 X/Y/Z 회전을 모두 고정")
        if self.require_boost:
            checks.append(
                f"D 이동 대비 D+LeftShift 이동 거리가 {self.boost_min_ratio}배 이상"
            )
        if self.require_left_boost:
            checks.append("A+LeftShift도 왼쪽으로 동일한 부스트 효과")
        if self.require_bidirectional:
            checks.append(
                f"D/A를 각각 {self.movement_duration}초 입력해 "
                f"{self.movement_min_distance} 이상 "
                f"{self.movement_max_distance():.0f} 이하 이동"
            )
        if self.require_jump_landing:
            checks.append("점프가 기준 높이 이상 상승한 뒤 시작 높이로 착지")
        if self.require_screenshot:
            checks.append("Play 중 Game 뷰 스크린샷 파일 생성")
        if self.require_gameplay:
            checks.append("입력 해제 후 Play 종료 및 unity_get_state isPlaying=false")
        return checks


@dataclass
class VerificationContract:
    spec: VerificationSpec
    project_dir: str
    session_scripts: set[str] = field(default_factory=set)
    state_seen: bool = False
    scene_path_seen: str | None = None
    scene_clean: bool = False
    final_stopped: bool = False
    compile_checked: bool = False
    compile_errors: list = field(default_factory=list)
    compile_error_count: int = 0
    played: bool = False
    playing: bool = False
    waited: bool = False
    runtime_checked: bool = False
    runtime_errors: list = field(default_factory=list)
    runtime_error_count: int = 0
    level_marker_seen: bool = False
    observed_components: dict[str, list[str]] = field(default_factory=dict)
    observed_component_data: dict[str, dict[str, dict]] = field(default_factory=dict)
    latest_positions: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    movement_before: tuple[float, float, float] | None = None
    movement_after: tuple[float, float, float] | None = None
    camera_before: tuple[float, float, float] | None = None
    camera_after: tuple[float, float, float] | None = None
    jump_before: tuple[float, float, float] | None = None
    jump_after: tuple[float, float, float] | None = None
    jump_peak_y: float | None = None
    movement_input_seen: bool = False
    jump_input_seen: bool = False
    screenshot_path: str | None = None
    screenshot_in_play: bool = False
    input_released: bool = False
    tool_errors: list[str] = field(default_factory=list)
    play_active_confirmed: bool = False
    play_ended_unexpectedly: bool = False
    final_stop_requested: bool = False
    motion_before: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    motion_after: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    camera_motion_before: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    camera_motion_after: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    motion_duration: dict[str, float] = field(default_factory=dict)
    idle_before: tuple[float, float, float] | None = None
    idle_after: tuple[float, float, float] | None = None
    jump_landed: bool = False
    blocked_by: dict[str, list[str]] = field(default_factory=dict)
    policy_violations: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.policy_violations = lint_scripts(
            self.spec.request, self.spec.asset_paths, self.project_dir
        )

    def block(self, stage: str, reason: str) -> None:
        self.blocked_by.setdefault(stage, [])
        if reason not in self.blocked_by[stage]:
            self.blocked_by[stage].append(reason)

    def prepare_call(self, name: str, args: dict) -> tuple[dict, str | None]:
        args = dict(args or {})
        if name in MUTATION_TOOLS:
            return args, f"Verification blocked mutation tool: {name}"
        if name == "unity_send_key" and not self.playing:
            return args, "Verification blocked input outside Play Mode"
        return args, None

    def begin_motion(self, name: str) -> bool:
        player = self.latest_positions.get("player")
        if player is None:
            return False
        self.motion_before[name] = player
        camera = self.latest_positions.get("main camera")
        if camera is not None:
            self.camera_motion_before[name] = camera
        return True

    def rightward_delta(self) -> float | None:
        """X change of the canonical rightward sample, or None if unmeasured."""
        before, after = self.motion_before.get("rightArrow"), self.motion_after.get("rightArrow")
        return None if before is None or after is None else after[0] - before[0]

    def end_motion(self, name: str) -> None:
        player = self.latest_positions.get("player")
        if player is not None:
            self.motion_after[name] = player
        camera = self.latest_positions.get("main camera")
        if camera is not None:
            self.camera_motion_after[name] = camera

    def observe(self, name: str, args: dict, result: str) -> None:
        data = _ok(result)
        if data is None:
            return
        if name == "unity_get_state":
            self.state_seen = True
            self.playing = bool(data.get("isPlaying"))
            scene = data.get("activeScene") or {}
            self.scene_path_seen = _normalise_path(scene.get("path", "")) or None
            self.scene_clean = bool(self.scene_path_seen) and not bool(scene.get("isDirty", True))
            if self.played and self.playing:
                self.play_active_confirmed = True
            # isPlaying can temporarily read false while Unity reloads the
            # domain after accepting a play request. It is an unexpected end
            # only after this contract has observed Play Mode active once.
            if self.play_active_confirmed and not self.playing:
                self.final_stopped = True
                if not self.final_stop_requested:
                    self.play_ended_unexpectedly = True
        elif name == "unity_play_mode":
            action = str(args.get("action", "")).lower()
            if action == "play":
                self.played = True
                self.playing = True
                self.play_active_confirmed = False
                self.waited = False
                self.runtime_checked = False
                self.final_stopped = False
                self.final_stop_requested = False
            elif action == "stop":
                self.playing = False
                self.final_stop_requested = True
        elif name == "unity_wait" and self.playing:
            self.waited = True
        elif name == "unity_read_console":
            entries = data.get("entries") if isinstance(data.get("entries"), list) else []
            if "[LevelLoader] Loaded" in str(result):
                self.level_marker_seen = True
            requested = str(args.get("types", "")).lower()
            is_error_check = not requested or "error" in requested or "exception" in requested
            if is_error_check:
                if self.playing and self.waited:
                    self.runtime_checked = True
                    self.runtime_errors = _compact_entries(self.runtime_errors + entries)
                    self.runtime_error_count = len(self.runtime_errors)
                elif not self.playing:
                    self.compile_checked = True
                    self.compile_error_count = len(entries)
                    self.compile_errors = _compact_entries(entries)
        elif name == "unity_get_gameobject":
            target = str(args.get("target", "")).strip()
            pos = _position(data)
            if pos is not None:
                self.latest_positions[target.lower()] = pos
                if target.lower() == "player":
                    if self.movement_input_seen:
                        self.movement_after = pos
                    if self.jump_input_seen:
                        if self.jump_after is None or pos[1] > self.jump_after[1]:
                            self.jump_after = pos
                        self.jump_peak_y = max(self.jump_peak_y or pos[1], pos[1])
                elif target.lower() == "main camera" and self.movement_input_seen:
                    self.camera_after = pos
            components = data.get("components") or []
            observed = [
                str(item.get("type", "")) for item in components if isinstance(item, dict)
            ]
            if observed:
                self.observed_components[target] = observed
                self.observed_component_data[target] = {
                    str(item.get("type", "")): item.get("data")
                    for item in components
                    if isinstance(item, dict) and isinstance(item.get("data"), dict)
                }
        elif name == "unity_send_key":
            key = str(args.get("key", "")).lower()
            action = str(args.get("action", "tap")).lower()
            if key in {"rightarrow", "right", "d"} and action in {"press", "tap"}:
                self.movement_input_seen = True
                self.movement_before = self.latest_positions.get("player")
                self.camera_before = self.latest_positions.get("main camera")
            if key in {"space", "spacebar", "w", "uparrow"} and action in {"press", "tap"}:
                self.jump_input_seen = True
                self.jump_before = self.latest_positions.get("player")
            if action == "release":
                self.input_released = True
        elif name == "unity_get_input_state":
            self.input_released = not data.get("held") and not data.get("pendingReleases")
        elif name == "unity_screenshot":
            path = str(data.get("path", ""))
            if path and not os.path.isabs(path):
                path = os.path.join(self.project_dir, path)
            self.screenshot_path = os.path.abspath(path) if path else None
            self.screenshot_in_play = self.playing

    @staticmethod
    def _has_component(observed: Iterable[str], required: str) -> bool:
        required = required.lower()
        if required == "collider":
            return any(item.lower().split(".")[-1].endswith("collider") for item in observed)
        return any(item.lower() == required or item.lower().endswith("." + required)
                   for item in observed)

    def failures(self) -> list[str]:
        failed: list[str] = [f"tool_error:{item}" for item in self.tool_errors]
        # A request that talks about runtime behaviour but yields no measurable
        # condition must never be reported as verified. Previously the empty
        # failure list read as success without Play Mode ever running.
        if (
            self.spec.enabled
            and self.spec.behaviour_requested
            and not self.spec.behaviour_checks()
        ):
            failed.append("verification_spec_empty")
        failed.extend(f"policy_lint:{item}" for item in self.policy_violations)
        failed.extend(
            f"blocked:{stage}:{reason}"
            for stage, reasons in sorted(self.blocked_by.items())
            for reason in reasons
        )
        for path in self.spec.asset_paths:
            if not os.path.exists(os.path.join(self.project_dir, path)):
                failed.append(f"asset_missing:{path}")
        if not self.state_seen:
            failed.append("state_not_observed")
        if self.spec.scene_path and self.scene_path_seen != self.spec.scene_path:
            failed.append(f"wrong_active_scene:{self.scene_path_seen or 'unknown'}")
        if self.state_seen and not self.scene_clean:
            failed.append("scene_not_saved")
        if not self.compile_checked:
            failed.append("compile_not_checked")
        elif self.compile_error_count:
            failed.append(f"compile_errors:{self.compile_error_count}")
        for target, required in self.spec.required_components.items():
            observed = self.observed_components.get(target, [])
            for component in required:
                if not self._has_component(observed, component):
                    failed.append(f"component_missing:{target}:{component}")
        if self.spec.require_player_constraints:
            player_data = self.observed_component_data.get("Player", {})
            rigidbody = next(
                (data for name, data in player_data.items()
                 if name.lower().endswith(".rigidbody") or name.lower() == "rigidbody"),
                None,
            )
            constraints = rigidbody.get("constraints") if rigidbody else None
            if not isinstance(constraints, int):
                failed.append("rigidbody_constraints_not_observed")
            elif (constraints & 8) != 8 or (constraints & 112) != 112:
                failed.append(f"rigidbody_constraints_incomplete:{constraints}")
        if self.spec.require_camera_target:
            camera_data = self.observed_component_data.get("Main Camera", {})
            side_camera = next(
                (data for name, data in camera_data.items()
                 if name.lower().endswith("sidescrollercamera")),
                None,
            )
            target = side_camera.get("target") if side_camera else None
            if not isinstance(target, dict) or not target.get("instanceID"):
                failed.append("camera_target_null")
        if self.spec.require_gameplay:
            if "gameplay" in self.blocked_by:
                pass
            elif not self.played:
                failed.append("play_mode_not_tested")
            elif not self.play_active_confirmed:
                failed.append("play_mode_not_active_after_start")
            if "gameplay" not in self.blocked_by and self.play_ended_unexpectedly:
                failed.append("play_mode_ended_unexpectedly")
            if "gameplay" in self.blocked_by:
                pass
            elif not self.waited:
                failed.append("runtime_wait_missing")
            if "gameplay" in self.blocked_by:
                pass
            elif not self.runtime_checked:
                failed.append("runtime_console_not_checked")
            elif self.runtime_error_count:
                failed.append(f"runtime_errors:{self.runtime_error_count}")
        if self.spec.require_level_marker and not self.level_marker_seen:
            failed.append("level_loaded_marker_missing")
        if self.spec.require_movement:
            # "rightArrow" is the canonical label for the rightward sample; the
            # bidirectional "d" sample proves the same capability when the
            # request asked for A/D, so accept either.
            before = after = None
            for label in ("rightArrow", "d"):
                if (
                    self.motion_before.get(label) is not None
                    and self.motion_after.get(label) is not None
                ):
                    before, after = self.motion_before[label], self.motion_after[label]
                    break
            if before is None:
                before, after = self.movement_before, self.movement_after
            if "movement" in self.blocked_by:
                pass
            elif before is None or after is None:
                failed.append("player_movement_not_measured")
            elif after[0] - before[0] <= 1e-3:
                failed.append("player_did_not_move_right")
            elif after[0] - before[0] > self.spec.movement_max_distance():
                failed.append("player_moved_too_far")
        if self.spec.require_idle_stability:
            if "movement" in self.blocked_by:
                pass
            elif self.idle_before is None or self.idle_after is None:
                failed.append("idle_stability_not_measured")
            elif abs(self.idle_after[0] - self.idle_before[0]) > self.spec.idle_max_delta_x:
                failed.append("idle_drift_too_large")
        if self.spec.require_bidirectional:
            d = self.motion_before.get("d"), self.motion_after.get("d")
            a = self.motion_before.get("a"), self.motion_after.get("a")
            if "movement" in self.blocked_by:
                pass
            elif None in d:
                failed.append("d_movement_not_measured")
            elif d[1][0] - d[0][0] < self.spec.movement_min_distance:
                failed.append("d_did_not_move_right")
            elif d[1][0] - d[0][0] > self.spec.movement_max_distance(
                self.motion_duration.get("d")
            ):
                failed.append("d_moved_too_far")
            if "movement" in self.blocked_by:
                pass
            elif None in a:
                failed.append("a_movement_not_measured")
            elif a[1][0] - a[0][0] > -self.spec.movement_min_distance:
                failed.append("a_did_not_move_left")
            elif a[0][0] - a[1][0] > self.spec.movement_max_distance(
                self.motion_duration.get("a")
            ):
                failed.append("a_moved_too_far")
        if self.spec.require_jump:
            if "jump" in self.blocked_by:
                pass
            elif self.jump_before is None or self.jump_peak_y is None:
                failed.append("player_jump_not_measured")
            elif self.jump_peak_y - self.jump_before[1] < self.spec.jump_min_rise:
                failed.append("player_did_not_jump")
            if (
                self.spec.require_jump_landing
                and "jump" not in self.blocked_by
                and not self.jump_landed
            ):
                failed.append("player_did_not_land")
        if self.spec.require_camera_follow:
            camera_pair = next((
                (self.camera_motion_before.get(label), self.camera_motion_after.get(label))
                for label in ("d", "rightArrow")
                if self.camera_motion_before.get(label) is not None
            ), (self.camera_before, self.camera_after))
            if "camera" in self.blocked_by:
                pass
            elif None in camera_pair:
                failed.append("camera_follow_not_measured")
            elif camera_pair[1][0] - camera_pair[0][0] <= 1e-3:
                failed.append("camera_did_not_follow")
        if self.spec.require_camera_fixed_z:
            camera_pairs = [
                (before, self.camera_motion_after.get(label))
                for label, before in self.camera_motion_before.items()
                if self.camera_motion_after.get(label) is not None
            ]
            if "camera" in self.blocked_by:
                pass
            elif not camera_pairs:
                failed.append("camera_fixed_z_not_measured")
            elif any(abs(after[2] - before[2]) > 0.05 for before, after in camera_pairs):
                failed.append("camera_z_changed")
        if self.spec.require_boost:
            normal_before = self.motion_before.get("boost_normal")
            normal_after = self.motion_after.get("boost_normal")
            boost_before = self.motion_before.get("boost_shift")
            boost_after = self.motion_after.get("boost_shift")
            if "boost" in self.blocked_by:
                pass
            elif None in (normal_before, normal_after, boost_before, boost_after):
                failed.append("boost_not_measured")
            else:
                normal = abs(normal_after[0] - normal_before[0])
                boosted = abs(boost_after[0] - boost_before[0])
                if normal <= 1e-3 or boosted < normal * self.spec.boost_min_ratio:
                    failed.append("boost_distance_too_short")
            if self.spec.require_left_boost:
                left_before = self.motion_before.get("boost_left")
                left_after = self.motion_after.get("boost_left")
                if "boost" in self.blocked_by:
                    pass
                elif None in (left_before, left_after):
                    failed.append("left_boost_not_measured")
                else:
                    left = abs(left_after[0] - left_before[0])
                    if left_after[0] >= left_before[0] or normal <= 1e-3 or (
                        left < normal * self.spec.boost_min_ratio
                    ):
                        failed.append("left_boost_distance_too_short")
        if self.spec.require_screenshot:
            if "screenshot" in self.blocked_by:
                pass
            elif not self.screenshot_in_play or not self.screenshot_path:
                failed.append("play_screenshot_missing")
            elif not os.path.exists(self.screenshot_path):
                failed.append("screenshot_file_missing")
        if self.spec.require_gameplay:
            if (
                "gameplay" not in self.blocked_by
                and not self.input_released
                and (self.spec.require_movement or self.spec.require_jump)
            ):
                failed.append("simulated_input_not_released")
            if "gameplay" not in self.blocked_by and not self.final_stopped:
                failed.append("play_mode_not_stopped")
        return failed

    def missing_verification(self) -> list[str]:
        return self.failures()

    def measured_checks(self) -> list[str]:
        """Checks for which a real measurement exists, regardless of pass/fail."""
        def pair(before: dict, after: dict, *labels: str) -> bool:
            return any(
                before.get(label) is not None and after.get(label) is not None
                for label in labels
            )

        measured = {
            "gameplay": self.played and self.waited and self.runtime_checked,
            "movement": pair(self.motion_before, self.motion_after, "rightArrow")
                        or (self.movement_before is not None
                            and self.movement_after is not None),
            "bidirectional": pair(self.motion_before, self.motion_after, "d")
                             and pair(self.motion_before, self.motion_after, "a"),
            "idle_stability": self.idle_before is not None and self.idle_after is not None,
            "jump": self.jump_before is not None and self.jump_peak_y is not None,
            "camera_follow": pair(self.camera_motion_before, self.camera_motion_after,
                                  "d", "rightArrow")
                             or (self.camera_before is not None
                                 and self.camera_after is not None),
            "camera_fixed_z": any(
                self.camera_motion_after.get(label) is not None
                for label in self.camera_motion_before
            ),
            "camera_target": bool(self.observed_component_data.get("Main Camera")),
            "player_constraints": bool(self.observed_component_data.get("Player")),
            "boost": pair(self.motion_before, self.motion_after,
                          "boost_normal") and pair(self.motion_before,
                                                   self.motion_after, "boost_shift"),
            "left_boost": pair(self.motion_before, self.motion_after, "boost_left"),
            "level_marker": self.level_marker_seen,
            "screenshot": bool(self.screenshot_path) and self.screenshot_in_play,
        }
        # jump_landing rides along with the jump arc measurement.
        measured["jump_landing"] = measured["jump"]
        names = []
        for name in self.spec.requested_checks():
            if name.startswith("components:"):
                if self.observed_components.get(name.split(":", 1)[1]):
                    names.append(name)
            elif measured.get(name):
                names.append(name)
        return names

    def check_report(self) -> dict:
        """Requested vs actually measured checks, for the verification receipt."""
        requested = self.spec.requested_checks()
        measured = self.measured_checks()
        measured_set = set(measured)
        return {
            "requested_checks": requested,
            "measured_checks": measured,
            "skipped_checks": [name for name in requested if name not in measured_set],
        }

    def evidence(self) -> dict:
        def delta(before, after):
            return None if before is None or after is None else [
                round(after[i] - before[i], 6) for i in range(3)
            ]
        return {
            "active_scene": self.scene_path_seen,
            "scene_clean": self.scene_clean,
            "compile": {
                "checked": self.compile_checked, "error_count": self.compile_error_count,
                "unique_errors": self.compile_errors,
            },
            "runtime": {
                "played": self.played, "waited": self.waited,
                "checked": self.runtime_checked, "error_count": self.runtime_error_count,
                "unique_errors": self.runtime_errors,
                "level_loaded_marker": self.level_marker_seen,
            },
            "components": self.observed_components,
            "component_data": self.observed_component_data,
            "player_movement_delta": delta(self.movement_before, self.movement_after),
            "player_jump_delta": delta(self.jump_before, self.jump_after),
            "player_jump_peak_y": self.jump_peak_y,
            "camera_follow_delta": delta(self.camera_before, self.camera_after),
            "screenshot": self.screenshot_path,
            "screenshot_captured_in_play": self.screenshot_in_play,
            "input_released": self.input_released,
            "final_play_mode_stopped": self.final_stopped,
            "play_active_confirmed": self.play_active_confirmed,
            "play_ended_unexpectedly": self.play_ended_unexpectedly,
            "motion_deltas": {
                name: delta(before, self.motion_after.get(name))
                for name, before in self.motion_before.items()
            },
            "camera_motion_deltas": {
                name: delta(before, self.camera_motion_after.get(name))
                for name, before in self.camera_motion_before.items()
            },
            "motion_durations": self.motion_duration,
            "idle_delta": delta(self.idle_before, self.idle_after),
            "jump_landed": self.jump_landed,
            "blocked_by": self.blocked_by,
            "policy_lint": self.policy_violations,
            "tool_errors": self.tool_errors,
        }


# Which canonical check each failure code belongs to. Used to tell a genuine
# regression ("a check that passed now fails") apart from a first measurement
# ("a check that was blocked before is finally being measured, and it fails").
# Order matters: longer prefixes must precede the shorter ones they extend.
_FAILURE_CHECK_PREFIXES = (
    ("player_did_not_land", "jump_landing"),
    ("player_did_not_jump", "jump"),
    ("player_jump_not_measured", "jump"),
    ("player_did_not_move_right", "movement"),
    ("player_moved_too_far", "movement"),
    ("player_movement_not_measured", "movement"),
    ("d_did_not_move_right", "bidirectional"),
    ("d_moved_too_far", "bidirectional"),
    ("d_movement_not_measured", "bidirectional"),
    ("a_did_not_move_left", "bidirectional"),
    ("a_moved_too_far", "bidirectional"),
    ("a_movement_not_measured", "bidirectional"),
    ("idle_drift_too_large", "idle_stability"),
    ("idle_stability_not_measured", "idle_stability"),
    ("camera_did_not_follow", "camera_follow"),
    ("camera_follow_not_measured", "camera_follow"),
    ("camera_z_changed", "camera_fixed_z"),
    ("camera_fixed_z_not_measured", "camera_fixed_z"),
    ("camera_target_null", "camera_target"),
    ("rigidbody_constraints_", "player_constraints"),
    ("left_boost_distance_too_short", "left_boost"),
    ("left_boost_not_measured", "left_boost"),
    ("boost_distance_too_short", "boost"),
    ("boost_not_measured", "boost"),
    ("level_loaded_marker_missing", "level_marker"),
    ("play_screenshot_missing", "screenshot"),
    ("screenshot_file_missing", "screenshot"),
    ("runtime_errors:", "gameplay"),
    ("play_mode_not_tested", "gameplay"),
)

# Failure codes that carry a magnitude. Fewer errors than before is progress,
# not a new regression, so these are compared numerically instead of by string.
_COUNTED_FAILURES = ("compile_errors", "runtime_errors")


def failure_check_name(failure: str) -> str | None:
    """Canonical check a failure belongs to, or None for always-static checks.

    None means the check is never blocked (compile, scene save, asset presence),
    so its appearance is always a real regression.
    """
    if failure.startswith("component_missing:"):
        parts = failure.split(":")
        return f"components:{parts[1]}" if len(parts) > 2 else None
    for prefix, name in _FAILURE_CHECK_PREFIXES:
        if failure.startswith(prefix):
            return name
    return None


def failure_count(failure: str) -> tuple[str, int] | None:
    """Split ``compile_errors:3`` into ``("compile_errors", 3)``."""
    head, _, tail = failure.partition(":")
    if head in _COUNTED_FAILURES and tail.isdigit():
        return head, int(tail)
    return None


def fix_prompt(spec: VerificationSpec, failures: list[str], evidence: dict) -> str:
    allowed = ", ".join(spec.asset_paths) or "(요청에 명시된 기존 산출물만)"
    forbidden = (
        "LevelLoader, level JSON, StreamingAssets/Levels"
        if not spec.require_level_marker else "(없음)"
    )
    lint_guidance = []
    for failure in failures:
        if "undefined_compare_tag:" in failure:
            lint_guidance.append(
                "- undefined_compare_tag: 해당 CompareTag 호출이 포함된 분기 또는 "
                "OnTriggerEnter 메서드를 코드에서 완전히 삭제한다. 금지된 태그 이름을 "
                "주석에도 남기지 않고, 새 태그를 만들지 않는다."
            )
        if "fall_respawn_check_missing:" in failure:
            lint_guidance.append(
                "- fall_respawn_check_missing: Update/FixedUpdate에서 "
                "transform.position.y 임계값을 검사해 저장된 시작 위치로 복귀시킨다."
            )
        if "legacy_input_api:" in failure:
            lint_guidance.append(
                "- legacy_input_api: UnityEngine.Input 호출을 전부 제거하고 "
                "Keyboard.current 키 상태만 사용한다."
            )
        if "idle_velocity_not_zeroed:" in failure:
            lint_guidance.append(
                "- idle_velocity_not_zeroed: 입력이 0이어도 "
                "rb.linearVelocity.x를 0으로 매 프레임 대입하고 Y 속도는 보존한다."
            )
        if "camera_z_accumulates_offset:" in failure:
            lint_guidance.append(
                "- camera_z_accumulates_offset: Start/Awake에서 fixedZ = "
                "transform.position.z를 한 번 저장한다. LateUpdate의 목표 위치는 "
                "new Vector3(target.position.x + offset.x, "
                "target.position.y + offset.y, fixedZ)로 계산한다. 현재 "
                "transform.position.z 또는 fixedZ가 든 Vector3 뒤에 `+ offset`을 "
                "붙이지 않는다."
            )
        if "rigidbody_constraints_incomplete:" in failure:
            lint_guidance.append(
                "- rigidbody_constraints_incomplete: Player Rigidbody.constraints를 "
                "FreezePositionZ | FreezeRotationX | FreezeRotationY | "
                "FreezeRotationZ로 설정한다(정수 비트값 120). 기존 필수 비트를 "
                "단일 값으로 덮어쓰지 않는다."
            )
        if "camera_target_null" in failure:
            lint_guidance.append(
                "- camera_target_null: Main Camera의 SideScrollerCamera.target에 "
                "기존 Player Transform을 직렬화해 연결한다. undefined tag 검색에 "
                "의존하지 않는다."
            )
        if "a_did_not_move_left" in failure or "left_boost_distance_too_short" in failure:
            lint_guidance.append(
                "- 왼쪽 이동 실패: Player 시작 위치가 플랫폼 끝면/중복 콜라이더에 "
                "끼어 있는지 확인한다. 기존 시작 평지를 이동·확장해 시작점 좌우 "
                "각 6유닛 이상에 수직 장애물이 없는 연속 평지를 만들고, Player를 "
                "그 평지 중앙 위의 겹치지 않는 위치에 둔다. 새 씬을 만들지 않는다."
            )
        if failure in {
            "player_did_not_move_right",
            "d_did_not_move_right",
            "a_did_not_move_left",
        }:
            lint_guidance.append(
                "- 이동 입력 미전달: PlayerInput과 InputAction 바인딩이 실제로 설정되지 "
                "않은 씬에서는 OnMove(InputValue)가 호출되지 않는다. 이 경우 "
                "Keyboard.current를 null 검사한 뒤 aKey/dKey.isPressed를 매 프레임 "
                "직접 읽어 -1/0/1 축을 만들고, FixedUpdate에서 "
                "rb.linearVelocity.x = axis * moveSpeed로 대입하되 Y 속도는 보존한다. "
                "직접 Keyboard.current를 읽는 구현에는 PlayerInput, InputActionAsset, "
                "생성 controls, 별도 입력 handler가 전혀 필요하지 않으므로 추가하지 말고, "
                "연결되지 않은 해당 컴포넌트가 이미 있으면 제거한다. "
                "legacy UnityEngine.Input API는 사용하지 않는다."
            )
        if "moved_too_far" in failure:
            lint_guidance.append(
                "- 이동 거리 과다: 매 FixedUpdate에서 AddForce(..., ForceMode.Impulse)를 "
                "호출하면 속도가 누적돼 폭주한다. 지속 입력은 속도를 직접 설정하거나"
                "(rb.linearVelocity = new Vector3(input * moveSpeed, rb.linearVelocity.y, 0)) "
                "ForceMode.Force로 바꾸고, 최대 속도를 제한한다."
            )
        if "player_did_not_jump" in failure:
            lint_guidance.append(
                "- 점프 실패: CheckGrounded Raycast가 안정 착지 위치에서 확실히 "
                "Collider를 맞히도록 거리/시작점을 Collider.bounds 기반으로 고친다. "
                "Keyboard.current.spaceKey.isPressed는 Update에서 읽어 "
                "jumpRequested=true로 저장하고, FixedUpdate에서 isGrounded일 때 "
                "점프를 적용한 뒤 false로 소비한다. 브리지가 큐잉한 짧은 입력은 "
                "wasPressedThisFrame 전환이 gameplay Update 전에 사라질 수 있으므로 "
                "그 엣지에만 의존하지 않는다."
            )
    lint_section = "\n".join(dict.fromkeys(lint_guidance)) or "- 해당 없음"
    return f"""[독립 검증 실패 자동 수정 단계]
원래 요청:
{spec.request}

호스트 검증 실패 항목:
{json.dumps(failures, ensure_ascii=False, indent=2)}

수집된 측정값:
{json.dumps(evidence, ensure_ascii=False, indent=2, default=str)}

수정 허용 범위:
{allowed}

금지 범위:
{forbidden}

정적 정책 실패의 결정적 수정법:
{lint_section}

실패 항목만 고쳐라. 기존 파일과 오브젝트를 먼저 조회해 재사용하고 성공한 산출물을
재생성하지 마라. 편집은 Play Mode를 끝낸 뒤 수행하고 저장/컴파일 확인까지 마쳐라.
완료 판정은 다음 fresh 검증 단계가 하므로 수정 결과를 스스로 완료라고 선언하지 마라."""


def write_receipt(root_dir: str, spec: VerificationSpec, status: str, evidence: dict,
                  failures: list[str], attempts: list[dict], elapsed_seconds: float,
                  build_success: bool | None = None,
                  check_report: dict | None = None) -> str:
    now = datetime.now().astimezone()
    day = os.path.join(os.path.abspath(root_dir), now.strftime("%Y"), now.strftime("%m"),
                       now.strftime("%d"))
    os.makedirs(day, exist_ok=True)
    path = os.path.join(
        day, f"{now.strftime('%Y%m%d_%H%M%S_%f')[:-3]}_{uuid.uuid4().hex[:10]}.json"
    )
    payload = {
        "version": VERSION,
        "timestamp": now.isoformat(timespec="milliseconds"),
        "status": status,
        "request": spec.request,
        "build_stage_success": build_success,
        # Requested vs measured makes an empty verification visible at a glance
        # instead of hiding behind an empty failure list.
        **(check_report or {
            "requested_checks": spec.requested_checks(),
            "measured_checks": [],
            "skipped_checks": spec.requested_checks(),
        }),
        "spec": asdict(spec),
        "evidence": evidence,
        "failures": failures,
        "attempts": attempts,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    return path
