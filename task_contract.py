"""Deterministic safety and completion checks for one agent request.

The language model proposes tool calls; this module decides whether each call is
within the request's working set and tracks the minimum verification workflow.
It deliberately contains no model logic, so a guessed tool name cannot bypass
the policy.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable


# Stop at the first recognised file extension.  The previous broad expression
# could consume "Assets/Foo.cs and Assets/Bar.unity" as one invalid path.
_ASSET_PATH = re.compile(r"Assets/[^\r\n]*?\.(?:cs|unity|prefab|mat|json)\b", re.I)
_SCRIPT_PREFIX = "Assets/Scripts/"
_SCENE_PREFIX = "Assets/Scenes/"
_LEVELS_PREFIX = "Assets/StreamingAssets/Levels/"
_LOADER_SCRIPT = "Assets/Scripts/LevelLoader.cs"
_INPUT_SIM_WORDS = ("플레이 검증", "조작", "입력 테스트", "입력 시뮬레이", "keyboard", "send_key", "키 입력")
_SCENE_MUTATIONS = {
    "unity_create_gameobject", "unity_create_gameobjects", "unity_modify_gameobject",
    "unity_delete_gameobject", "unity_add_component", "unity_set_component_property",
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

    @classmethod
    def from_request(cls, request: str, known_scripts: Iterable[str] = ()) -> "TaskContract":
        request_lower = request.lower()
        require_input_sim = any(word in request_lower for word in _INPUT_SIM_WORDS)
        return cls(
            user_paths={_normalise_path(m.group(0)) for m in _ASSET_PATH.finditer(request)},
            session_scripts=set(known_scripts),
            require_screenshot=any(word in request_lower for word in ("screenshot", "스크린샷", "capture", "캡처")),
            require_play=require_input_sim,
            require_input_sim=require_input_sim,
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
            if name in {"unity_read_script", "unity_delete_script"} and path not in self.user_paths | self.session_scripts:
                return args, (
                    f"Policy blocked {name} for {path}: the user did not explicitly scope this existing script. "
                    "Only scripts created in this session or an Assets/... path named by the user may be read or deleted."
                )

        if name == "unity_create_scene":
            path = _normalise_path(args.get("path"))
            args["path"] = path
            if not path.startswith(_SCENE_PREFIX) or not path.lower().endswith(".unity"):
                return args, "Policy blocked scene creation: use an Assets/Scenes/*.unity path."

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
            self.refreshed_after_write = False
            self.compile_checked = False
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
