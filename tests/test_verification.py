import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

import config
from agent import Agent
from local_tools import wait_seconds
from preflight import inspect_request
from policy_lint import apply_safe_repairs, lint_scripts
from verification import VerificationContract, VerificationSpec, write_receipt
from version import __version__


def result(value):
    return json.dumps({"status": "ok", "result": value}, ensure_ascii=False)


class VerificationSpecTests(unittest.TestCase):
    def test_short_runtime_sampling_wait_is_allowed(self):
        self.assertEqual(wait_seconds({"seconds": 0.15}), 0.15)
        with self.assertRaises(ValueError):
            wait_seconds({"seconds": 0.01})

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
        self.assertEqual(spec.required_components["Player"], ["Rigidbody", "Collider"])

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
        self.assertEqual(
            spec.required_components["Player"],
            ["Rigidbody", "Collider", "PlayerMovement25D"],
        )
        self.assertEqual(spec.required_components["Main Camera"], ["Camera", "SideScrollerCamera"])

    def test_ad_and_boost_are_explicit_behavioral_requirements(self):
        spec = VerificationSpec.from_request("A/D 좌우 이동과 Shift 부스트가 있는 platformer를 만들어줘")
        self.assertTrue(spec.require_bidirectional)
        self.assertTrue(spec.require_boost)

    def test_explicit_fixed_depth_and_target_require_structural_evidence(self):
        spec = VerificationSpec.from_request(
            "플랫포머 Player의 Z 이동과 회전을 고정하고 Main Camera는 Z는 고정, "
            "시작 직후 target이 null이 아니게 만들어줘"
        )
        self.assertTrue(spec.require_player_constraints)
        self.assertTrue(spec.require_camera_fixed_z)
        self.assertTrue(spec.require_camera_target)

    def test_conflicting_scene_paths_are_blocked_before_mutation(self):
        request = (
            "새 씬 Assets/Scenes/Platformer25D_MVP_22.unity 에 제작해줘.\n"
            "[Play Mode 합격 조건]\n"
            "2. 씬이 Assets/Scenes/Platformer25D_MVP.unity 로 저장"
        )
        result = inspect_request(request, "strict")
        self.assertFalse(result.allowed)
        self.assertEqual(result.blocking_issues[0].code, "conflicting_scene_paths")

    def test_acceptance_policy_selects_only_acceptance_scene(self):
        request = (
            "새 씬 Assets/Scenes/Platformer25D_MVP_22.unity 에 제작해줘.\n"
            "[Play Mode 합격 조건]\n"
            "2. 씬이 Assets/Scenes/Platformer25D_MVP.unity 로 저장"
        )
        result = inspect_request(request, "acceptance")
        self.assertTrue(result.allowed)
        self.assertEqual(
            result.canonical_scene_path, "Assets/Scenes/Platformer25D_MVP.unity"
        )
        self.assertNotIn("Assets/Scenes/Platformer25D_MVP_22.unity", result.asset_paths)
        self.assertNotIn("Platformer25D_MVP_22.unity", result.normalized_request)
        self.assertIn("Platformer25D_MVP.unity", result.normalized_request)


class BehaviourSpecExtractionTests(unittest.TestCase):
    """P0 회귀: 짧은 한국어 수정 요청이 검증 조건을 잃지 않아야 한다.

    실제 영수증 20260723_152344에서 이 요청이 require_jump=False,
    played=False인데도 status=verified로 기록됐다.
    """

    JUMP_FIX_REQUEST = (
        "문제: Player가 시작 후 착지한 뒤 Space 점프가 동작하지 않는다.\n"
        "새 요소는 생성하지말고, 점프가 잘 작동하도록 수정하고 검증한다."
    )

    def test_jump_fix_request_requires_jump_measurement(self):
        spec = VerificationSpec.from_request(self.JUMP_FIX_REQUEST)
        self.assertTrue(spec.enabled)
        self.assertTrue(spec.require_jump, "게임 키워드 없이도 점프를 측정해야 한다")
        self.assertTrue(spec.require_jump_landing, "착지 조건도 함께 추출돼야 한다")
        self.assertTrue(spec.require_gameplay, "행동 조건이 있으면 Play Mode가 필수")
        self.assertIn("jump", spec.behaviour_checks())

    def test_jump_fix_request_cannot_pass_without_play_mode(self):
        """영수증의 실제 실패 조건 재현: Play Mode 없이 verified가 되면 안 된다."""
        spec = VerificationSpec.from_request(self.JUMP_FIX_REQUEST)
        with tempfile.TemporaryDirectory() as project:
            contract = VerificationContract(spec=spec, project_dir=project)
            # 컴파일/씬 상태는 깨끗하지만 Play Mode는 돌지 않은 상태
            contract.state_seen = True
            contract.scene_clean = True
            contract.compile_checked = True
            failures = contract.failures()
        self.assertIn("play_mode_not_tested", failures)
        self.assertNotEqual(failures, [])

    def test_short_korean_movement_fix_requires_movement(self):
        spec = VerificationSpec.from_request("좌우 이동이 안 먹는다. 수정해줘")
        self.assertTrue(spec.require_movement)
        self.assertTrue(spec.require_gameplay)

    def test_boost_request_implies_movement_baseline(self):
        spec = VerificationSpec.from_request("부스트가 동작하지 않는다. 고쳐줘")
        self.assertTrue(spec.enabled, "'고쳐줘'도 수리 요청이므로 검증이 켜져야 한다")
        self.assertTrue(spec.require_boost)
        self.assertTrue(spec.require_movement, "부스트는 일반 이동 대비 비율로 측정한다")

    def test_repair_verbs_enable_managed_verification(self):
        for request in ("점프를 고쳐줘", "이동 버그를 해결해줘", "repair the jump logic"):
            with self.subTest(request=request):
                self.assertTrue(VerificationSpec.from_request(request).enabled)

    def test_camera_follow_without_game_word(self):
        spec = VerificationSpec.from_request("카메라가 Player를 따라가지 않는다. 수정해줘")
        self.assertTrue(spec.require_camera_follow)
        self.assertTrue(spec.require_gameplay)

    def test_remove_does_not_match_move(self):
        """영어 단어 경계: 'remove'가 이동 검증을 켜면 안 된다."""
        spec = VerificationSpec.from_request("remove the unused cube from the scene")
        self.assertFalse(spec.require_movement)
        self.assertFalse(spec.require_gameplay)

    def test_renaming_player_does_not_demand_play_mode(self):
        spec = VerificationSpec.from_request("Player 오브젝트 이름을 Hero로 수정해줘")
        self.assertFalse(spec.require_movement)
        self.assertFalse(spec.require_jump)
        self.assertFalse(spec.require_gameplay)

    def test_non_behavioural_edit_is_not_spec_empty(self):
        """동작과 무관한 수정 요청까지 verification_spec_empty로 막으면 안 된다."""
        spec = VerificationSpec.from_request("Assets/Scripts/Foo.cs의 오타를 수정해줘")
        self.assertFalse(spec.behaviour_requested)
        with tempfile.TemporaryDirectory() as project:
            os.makedirs(os.path.join(project, "Assets", "Scripts"))
            with open(os.path.join(project, "Assets", "Scripts", "Foo.cs"), "w") as handle:
                handle.write("using UnityEngine; public class Foo : MonoBehaviour {}")
            contract = VerificationContract(spec=spec, project_dir=project)
            contract.state_seen = True
            contract.scene_clean = True
            contract.compile_checked = True
            self.assertEqual(contract.failures(), [])

    def test_unmappable_behaviour_request_is_refused(self):
        """측정 조건을 못 뽑는 동작 요청은 성공 대신 spec_empty로 종료한다."""
        spec = VerificationSpec.from_request("적 AI 충돌 판정이 동작하지 않는다. 수정하고 검증해줘")
        self.assertTrue(spec.behaviour_requested)
        self.assertEqual(spec.behaviour_checks(), [])
        with tempfile.TemporaryDirectory() as project:
            contract = VerificationContract(spec=spec, project_dir=project)
            contract.state_seen = True
            contract.scene_clean = True
            contract.compile_checked = True
            failures = contract.failures()
        self.assertIn("verification_spec_empty", failures)

    def test_check_report_separates_measured_from_skipped(self):
        spec = VerificationSpec.from_request(self.JUMP_FIX_REQUEST)
        with tempfile.TemporaryDirectory() as project:
            contract = VerificationContract(spec=spec, project_dir=project)
            report = contract.check_report()
        self.assertIn("jump", report["requested_checks"])
        self.assertIn("jump", report["skipped_checks"])
        self.assertEqual(report["measured_checks"], [])

        with tempfile.TemporaryDirectory() as project:
            measured = VerificationContract(spec=spec, project_dir=project)
            measured.played = True
            measured.waited = True
            measured.runtime_checked = True
            measured.jump_before = (0.0, 1.0, 0.0)
            measured.jump_peak_y = 2.0
            report = measured.check_report()
        self.assertIn("jump", report["measured_checks"])
        self.assertIn("gameplay", report["measured_checks"])
        self.assertNotIn("jump", report["skipped_checks"])


class PolicyLintTests(unittest.TestCase):
    def test_platformer_policy_violations_are_found_before_play(self):
        with tempfile.TemporaryDirectory() as project:
            scripts = os.path.join(project, "Assets", "Scripts")
            settings = os.path.join(project, "ProjectSettings")
            os.makedirs(scripts)
            os.makedirs(settings)
            with open(os.path.join(settings, "TagManager.asset"), "w", encoding="utf-8") as handle:
                handle.write("tags:\n  - Custom\n")
            path = "Assets/Scripts/PlayerMovement25D.cs"
            with open(os.path.join(project, path), "w", encoding="utf-8") as handle:
                handle.write(
                    'using UnityEngine; public class PlayerMovement25D : MonoBehaviour {'
                    'void X(Collider c) { if (Input.GetKey(\"a\")) {} '
                    'if (c.CompareTag(\"Ground\")) {} } }'
                )
            request = (
                "legacy UnityEngine.Input API 사용 금지 Keyboard.current "
                "Rigidbody.linearVelocity CompareTag(\"Ground\") "
                "낙사 시 시작 위치로 복귀"
            )
            violations = lint_scripts(request, [path], project)
            self.assertIn(f"legacy_input_api:{path}", violations)
            self.assertIn(f"keyboard_current_missing:{path}", violations)
            self.assertIn(f"linear_velocity_missing:{path}", violations)
            self.assertIn(f"ground_compare_tag_forbidden:{path}", violations)
            self.assertIn(f"undefined_compare_tag:{path}:Ground", violations)
            self.assertIn(f"fall_respawn_check_missing:{path}", violations)

    def test_camera_current_z_plus_offset_is_rejected(self):
        with tempfile.TemporaryDirectory() as project:
            scripts = os.path.join(project, "Assets", "Scripts")
            os.makedirs(scripts)
            path = "Assets/Scripts/SideScrollerCamera.cs"
            with open(os.path.join(project, path), "w", encoding="utf-8") as handle:
                handle.write(
                    "using UnityEngine; public class SideScrollerCamera : MonoBehaviour {"
                    "Vector3 offset; Transform target; void LateUpdate() {"
                    "var p = new Vector3(target.position.x, target.position.y, "
                    "transform.position.z) + offset; transform.position = p; } }"
                )
            self.assertIn(
                f"camera_z_accumulates_offset:{path}",
                lint_scripts("Main Camera Z는 고정", [path], project),
            )

    def test_known_camera_z_offset_failure_is_repaired_deterministically(self):
        with tempfile.TemporaryDirectory() as project:
            scripts = os.path.join(project, "Assets", "Scripts")
            os.makedirs(scripts)
            path = "Assets/Scripts/SideScrollerCamera.cs"
            absolute = os.path.join(project, path)
            with open(absolute, "w", encoding="utf-8") as handle:
                handle.write(
                    "class SideScrollerCamera { void X() { var p = "
                    "new Vector3(target.position.x, target.position.y, fixedZ) + offset; } }"
                )
            changed = apply_safe_repairs(
                [f"policy_lint:camera_z_accumulates_offset:{path}"], project
            )
            self.assertEqual(changed, [path])
            with open(absolute, encoding="utf-8") as handle:
                repaired = handle.read()
            self.assertIn("target.position.x + offset.x", repaired)
            self.assertIn("target.position.y + offset.y, fixedZ)", repaired)
            self.assertNotIn("fixedZ) + offset", repaired)


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
                "components": [
                    {"type": "UnityEngine.Rigidbody"},
                    {"type": "UnityEngine.CapsuleCollider"},
                    {"type": "PlayerMovement"},
                ],
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
            self.assertEqual(saved["version"], __version__)
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
            ("", [(
                "unity_save_scene",
                {"path": "Assets/Scenes/Test.unity"},
            )]),
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
