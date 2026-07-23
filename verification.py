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
    "build", "create", "make", "implement", "update", "fix", "add", "remove",
)
_GAME_WORDS = ("게임", "플랫포머", "횡스크롤", "platformer", "side-scroller", "game")
_MOVEMENT_WORDS = ("플랫포머", "횡스크롤", "플레이어", "player", "이동", "movement")
_JUMP_WORDS = ("플랫포머", "점프", "jump", "platformer")
_CAMERA_WORDS = ("카메라", "camera", "따라", "추종", "follow")
_LEVEL_WORDS = ("levelloader", "level json", "레벨 json", "데이터 주도", "data-driven")
_BOOST_WORDS = ("부스트", "boost", "dash", "대시", "shift")

MUTATION_TOOLS = {
    "unity_create_gameobject", "unity_create_gameobjects", "unity_modify_gameobject",
    "unity_delete_gameobject", "unity_add_component", "unity_remove_component",
    "unity_set_component_property", "unity_create_material", "unity_create_scene",
    "unity_open_scene", "unity_save_scene", "unity_refresh_assets", "unity_write_script",
    "unity_delete_script", "unity_install_level_loader", "unity_write_level",
    "unity_execute_menu_item",
}


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
    boost_duration: float = 0.5
    boost_min_ratio: float = 1.4
    jump_min_rise: float = 0.5
    required_components: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_request(cls, request: str, force: bool = False) -> "VerificationSpec":
        lower = request.lower()
        preflight = inspect_request(request, config.SCENE_PATH_POLICY)
        assets = preflight.asset_paths
        game = any(word in lower for word in _GAME_WORDS)
        movement = game and any(word in lower for word in _MOVEMENT_WORDS)
        jump = game and any(word in lower for word in _JUMP_WORDS)
        # A camera merely mentioned in a non-game request should not trigger an
        # input measurement. Games requesting follow/tracking do.
        camera = game and any(word in lower for word in _CAMERA_WORDS)
        level = any(word in lower for word in _LEVEL_WORDS)
        script_classes = [os.path.splitext(os.path.basename(path))[0] for path in assets
                          if path.lower().endswith(".cs")]
        components: dict[str, list[str]] = {}
        if movement:
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
        return cls(
            request=preflight.normalized_request,
            enabled=force or any(word in lower for word in _BUILD_WORDS),
            asset_paths=assets,
            scene_path=preflight.canonical_scene_path,
            require_gameplay=game or level,
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
            require_boost=movement and any(word in lower for word in _BOOST_WORDS),
            require_bidirectional=movement and bool(
                re.search(r"\ba\b", lower) and re.search(r"\bd\b", lower)
            ),
            require_level_marker=level,
            require_screenshot=game,
            require_idle_stability="무입력 0.5초" in lower or "idle 0.5" in lower,
            require_jump_landing="다시 바닥에 착지" in lower or "land" in lower,
            require_left_boost="a+leftshift" in lower.replace(" ", ""),
            required_components=components,
        )

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
            checks.append("rightArrow 입력 전후 Player X가 실제로 증가")
        if self.require_idle_stability:
            checks.append(
                f"무입력 {self.idle_duration}초 Player X 변화가 "
                f"{self.idle_max_delta_x} 이하"
            )
        if self.require_jump:
            checks.append("space 입력 전후 Player Y가 실제로 증가")
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
                f"{self.movement_min_distance} 이상 이동"
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
            if self.played and not self.playing:
                self.final_stopped = True
                if not self.final_stop_requested:
                    self.play_ended_unexpectedly = True
        elif name == "unity_play_mode":
            action = str(args.get("action", "")).lower()
            if action == "play":
                self.played = True
                self.playing = True
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
            before = self.motion_before.get("rightArrow", self.movement_before)
            after = self.motion_after.get("rightArrow", self.movement_after)
            if "movement" in self.blocked_by:
                pass
            elif before is None or after is None:
                failed.append("player_movement_not_measured")
            elif after[0] - before[0] <= 1e-3:
                failed.append("player_did_not_move_right")
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
            if "movement" in self.blocked_by:
                pass
            elif None in a:
                failed.append("a_movement_not_measured")
            elif a[1][0] - a[0][0] > -self.spec.movement_min_distance:
                failed.append("a_did_not_move_left")
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
        if "player_did_not_jump" in failure:
            lint_guidance.append(
                "- 점프 실패: CheckGrounded Raycast가 안정 착지 위치에서 확실히 "
                "Collider를 맞히도록 거리/시작점을 Collider.bounds 기반으로 고친다. "
                "Space wasPressedThisFrame과 isGrounded 조건은 유지한다."
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
                  build_success: bool | None = None) -> str:
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
