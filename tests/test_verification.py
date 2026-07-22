import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

import config
from agent import Agent
from verification import VerificationContract, VerificationSpec, write_receipt


def result(value):
    return json.dumps({"status": "ok", "result": value}, ensure_ascii=False)


class VerificationSpecTests(unittest.TestCase):
    def test_platformer_request_creates_behavioral_checklist(self):
        spec = VerificationSpec.from_request(
            "새 씬 Assets/Scenes/Game.unity 에 카메라가 Player를 따라가는 플랫포머 게임을 만들어줘"
        )
        self.assertTrue(spec.enabled)
        self.assertTrue(spec.require_movement)
        self.assertTrue(spec.require_jump)
        self.assertTrue(spec.require_camera_follow)
        self.assertTrue(spec.require_screenshot)
        self.assertEqual(spec.scene_path, "Assets/Scenes/Game.unity")
        self.assertEqual(spec.required_components["Player"], ["Rigidbody"])

    def test_read_only_question_does_not_enable_managed_build(self):
        spec = VerificationSpec.from_request("현재 Unity 버전이 뭐야?")
        self.assertFalse(spec.enabled)

    def test_asset_path_before_korean_postposition_is_extracted(self):
        spec = VerificationSpec.from_request("Assets/Scenes/Game.unity에서 게임을 만들어줘")
        self.assertEqual(spec.scene_path, "Assets/Scenes/Game.unity")

    def test_exact_script_names_are_required_when_paths_are_named(self):
        spec = VerificationSpec.from_request(
            "Assets/Scripts/PlayerMovement25D.cs와 Assets/Scripts/SideScrollerCamera.cs로 "
            "Player 이동 카메라 플랫포머를 만들어줘"
        )
        self.assertEqual(spec.required_components["Player"], ["Rigidbody", "PlayerMovement25D"])
        self.assertEqual(spec.required_components["Main Camera"], ["Camera", "SideScrollerCamera"])

    def test_ad_and_boost_are_explicit_behavioral_requirements(self):
        spec = VerificationSpec.from_request("A/D 좌우 이동과 Shift 부스트가 있는 platformer를 만들어줘")
        self.assertTrue(spec.require_bidirectional)
        self.assertTrue(spec.require_boost)


class EvidenceTests(unittest.TestCase):
    def test_measured_movement_jump_camera_and_receipt_pass(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as receipts:
            screenshot = os.path.join(project, "shot.png")
            with open(screenshot, "wb") as handle:
                handle.write(b"png")
            spec = VerificationSpec.from_request(
                "카메라가 Player를 따라가는 플랫포머 게임을 만들어줘"
            )
            contract = VerificationContract(spec, project)
            contract.observe("unity_get_state", {}, result({
                "isPlaying": False,
                "activeScene": {"path": "Assets/Scenes/Game.unity", "isDirty": False},
            }))
            contract.observe("unity_read_console", {"types": "error,exception"}, result({"entries": []}))
            contract.observe("unity_get_gameobject", {"target": "Player"}, result({
                "transform": {"position": [0, 1, 0]},
                "components": [{"type": "UnityEngine.Rigidbody"}, {"type": "PlayerMovement"}],
            }))
            contract.observe("unity_get_gameobject", {"target": "Main Camera"}, result({
                "transform": {"position": [0, 4, -10]},
                "components": [{"type": "UnityEngine.Camera"}, {"type": "SideScrollerCamera"}],
            }))
            contract.observe("unity_play_mode", {"action": "play"}, result({"isPlaying": True}))
            contract.observe("unity_get_state", {}, result({
                "isPlaying": True,
                "activeScene": {"path": "Assets/Scenes/Game.unity", "isDirty": False},
            }))
            contract.observe("unity_wait", {"seconds": 1}, result({"waited": 1}))
            contract.observe("unity_read_console", {"types": "error,exception"}, result({"entries": []}))
            contract.observe("unity_send_key", {"key": "rightArrow", "action": "press"}, result({}))
            contract.observe("unity_get_gameobject", {"target": "Player"}, result({
                "transform": {"position": [3, 1, 0]}, "components": [],
            }))
            contract.observe("unity_get_gameobject", {"target": "Main Camera"}, result({
                "transform": {"position": [2, 4, -10]}, "components": [],
            }))
            contract.observe("unity_get_gameobject", {"target": "Player"}, result({
                "transform": {"position": [3, 1, 0]}, "components": [],
            }))
            contract.observe("unity_send_key", {"key": "space", "action": "tap"}, result({}))
            contract.observe("unity_get_gameobject", {"target": "Player"}, result({
                "transform": {"position": [3, 2, 0]}, "components": [],
            }))
            contract.observe("unity_get_input_state", {}, result({"held": [], "pendingReleases": []}))
            contract.observe("unity_screenshot", {}, result({"path": screenshot}))
            contract.observe("unity_play_mode", {"action": "stop"}, result({"isPlaying": False}))
            contract.observe("unity_get_state", {}, result({
                "isPlaying": False,
                "activeScene": {"path": "Assets/Scenes/Game.unity", "isDirty": False},
            }))

            self.assertEqual(contract.failures(), [])
            evidence = contract.evidence()
            self.assertEqual(evidence["player_movement_delta"][0], 3.0)
            self.assertEqual(evidence["player_jump_delta"][1], 1.0)
            path = write_receipt(receipts, spec, "verified", evidence, [], [], 1.2, True)
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["status"], "verified")
            self.assertEqual(saved["version"], "1.9.1")
            self.assertEqual(evidence["compile"]["error_count"], 0)


class HostTools:
    def __init__(self, dirty=False):
        self.tool_mode = "full"
        self.ollama_tools = []
        self.modes = []
        self.calls = []
        self.dirty = dirty

    def set_tool_mode(self, mode):
        self.tool_mode = mode
        self.modes.append(mode)

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == "unity_get_state":
            return result({
                "isPlaying": False,
                "activeScene": {"path": "Assets/Scenes/Test.unity", "isDirty": self.dirty},
            })
        if name == "unity_read_console":
            return result({"entries": []})
        return result({})


class NoModelAgent(Agent):
    async def _chat(self, messages=None, use_tools=True):
        raise AssertionError("standalone host verification must not ask a model to self-certify")


class RepairAgent(Agent):
    def __init__(self, tools, shown):
        super().__init__(
            tools, shown.append, lambda *_: None, shown.append,
            enable_logging=False, enable_verification=True,
        )
        self.turns = iter([
            ("모델의 성급한 완료", []),
            ("", [("unity_save_scene", {})]),
            ("수정 모델의 완료 주장", []),
        ])

    async def _chat(self, messages=None, use_tools=True):
        content, calls = next(self.turns)
        if content:
            self.on_text(content)
        return content, calls


class HostOrchestrationTests(unittest.TestCase):
    def test_verify_command_uses_host_evidence_and_writes_receipt(self):
        with tempfile.TemporaryDirectory() as receipts:
            tools = HostTools()
            shown = []
            agent = NoModelAgent(
                tools, shown.append, lambda *_: None, shown.append,
                enable_logging=False, enable_verification=True,
            )
            with mock.patch.object(config, "VERIFICATION_RECEIPT_DIR", receipts):
                success = asyncio.run(agent.run_turn("현재 씬 기본 검증", tool_mode="verify"))

            self.assertTrue(success)
            self.assertTrue(os.path.exists(agent.last_verification_receipt_path))
            self.assertEqual([name for name, _ in tools.calls], [
                "unity_get_state", "unity_read_console", "unity_get_state",
            ])
            self.assertIn("호스트 독립 검증 통과", "".join(shown))

    def test_dirty_scene_cannot_be_declared_complete(self):
        with tempfile.TemporaryDirectory() as receipts:
            tools = HostTools(dirty=True)
            shown = []
            agent = NoModelAgent(
                tools, shown.append, lambda *_: None, shown.append,
                enable_logging=False, enable_verification=True,
            )
            with mock.patch.object(config, "VERIFICATION_RECEIPT_DIR", receipts):
                success = asyncio.run(agent.run_turn("현재 씬 기본 검증", tool_mode="verify"))

            self.assertFalse(success)
            self.assertIn("scene_not_saved", "".join(shown))
            with open(agent.last_verification_receipt_path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["status"], "failed")

    def test_builder_completion_is_hidden_until_host_repair_and_reverify(self):
        with tempfile.TemporaryDirectory() as receipts:
            tools = HostTools()
            shown = []
            agent = RepairAgent(tools, shown)
            with mock.patch.object(config, "VERIFICATION_RECEIPT_DIR", receipts), \
                 mock.patch.object(config, "PLANNER", "off"), \
                 mock.patch.object(config, "FIX_MAX_CYCLES", 1):
                success = asyncio.run(agent.run_turn("씬을 수정해줘"))

            rendered = "".join(shown)
            self.assertTrue(success)
            self.assertNotIn("성급한 완료", rendered)
            self.assertNotIn("수정 모델의 완료 주장", rendered)
            self.assertIn("호스트 독립 검증 통과", rendered)
            with open(agent.last_verification_receipt_path, encoding="utf-8") as handle:
                receipt = json.load(handle)
            self.assertEqual(len(receipt["attempts"]), 2)
            self.assertIn(
                "builder_produced_no_mutation_evidence",
                receipt["attempts"][0]["failures"],
            )


if __name__ == "__main__":
    unittest.main()
