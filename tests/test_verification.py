import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import config
from agent import Agent
from local_tools import wait_seconds
from preflight import inspect_request
from policy_lint import apply_safe_repairs, lint_scripts
from verification import (
    VerificationContract, VerificationSpec, contact_already_gone,
    failure_check_name, failure_count, fix_prompt, write_receipt,
)
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

    def test_explicit_verification_request_with_no_checks_is_refused(self):
        """검증을 명시했는데 뽑아낼 검사가 하나도 없으면 성공으로 끝내면 안 된다.

        "10x20 격자 보드를 Awake에서 코드로 생성 ... 실제로 생성되는지 검증까지
        끝내줘"가 requested_checks=[] 상태로 `verified` 영수증을 남겼다. Play
        Mode에 들어가지도 않고 40초 만에 통과였다.
        """
        spec = VerificationSpec.from_request(
            "Assets/Scenes/Grid.unity 경로에 새 빈 씬을 생성하고, 10x20 격자 보드를 "
            "Awake에서 코드로 생성하는 MonoBehaviour를 만들어줘. 보드가 실제로 "
            "생성되는지 검증까지 끝내줘."
        )
        self.assertTrue(spec.verification_requested)
        # v1.11.16의 scene_objects는 이 요청에도 붙지만, 격자는 Awake에서 런타임에
        # 만들어지므로 Edit Mode 계층은 그것을 볼 수 없다. 이 검사 하나로 거절이
        # 풀리면 이 테스트가 지키는 영수증이 그대로 다시 나온다.
        self.assertEqual(spec.requested_checks(), ["scene_objects"])
        with tempfile.TemporaryDirectory() as project:
            scenes = os.path.join(project, "Assets", "Scenes")
            os.makedirs(scenes)
            with open(os.path.join(scenes, "Grid.unity"), "w") as handle:
                handle.write("scene")
            contract = VerificationContract(spec=spec, project_dir=project)
            contract.state_seen = True
            contract.scene_clean = True
            contract.compile_checked = True
            contract.scene_path_seen = spec.scene_path
            failures = contract.failures()
        self.assertIn("verification_spec_empty", failures)

    def test_edit_without_a_verification_request_stays_unblocked(self):
        """검증을 요구하지 않은 편집 요청까지 막으면 v1.11.2의 반대 오류가 된다."""
        spec = VerificationSpec.from_request("Assets/Scripts/Foo.cs의 오타를 수정해줘")
        self.assertFalse(spec.verification_requested)
        with tempfile.TemporaryDirectory() as project:
            os.makedirs(os.path.join(project, "Assets", "Scripts"))
            with open(os.path.join(project, "Assets", "Scripts", "Foo.cs"), "w") as handle:
                handle.write("using UnityEngine; public class Foo : MonoBehaviour {}")
            contract = VerificationContract(spec=spec, project_dir=project)
            contract.state_seen = True
            contract.scene_clean = True
            contract.compile_checked = True
            self.assertEqual(contract.failures(), [])

    def test_unmapped_requirement_is_named_when_movement_alone_is_measured(self):
        """부분 집합을 전체로 판정하는 것을 영수증에서 보이게 한다.

        v1.11.13은 검사가 *하나도* 없을 때만 막았다. 이동은 측정하면서 요청의
        핵심(점수·소멸·클리어)은 아무 검사도 없는 경우가 더 위험하다 —
        measured_checks가 비어 있지 않아 기존 가드에 걸리지 않는다.
        """
        spec = VerificationSpec.from_request(
            "Assets/Scenes/A.unity에 새 씬을 만들고 Player가 A/D로 이동하고 "
            "코인에 닿으면 점수가 1 올라가게 구현해줘. 실제로 되는지 검증까지 끝내줘."
        )
        self.assertIn("movement", spec.requested_checks())
        unmapped = spec.unmapped_requirements()
        self.assertTrue(unmapped)
        self.assertIn("점수", " ".join(unmapped))
        # 기록 단계다. 판정은 바꾸지 않는다.
        with tempfile.TemporaryDirectory() as project:
            contract = VerificationContract(spec=spec, project_dir=project)
            self.assertNotIn(
                "unmapped_requirement",
                " ".join(contract.failures()),
                "recording stage must not change the verdict",
            )

    def test_measured_shapes_report_no_unmapped_requirement(self):
        """통과가 확인된 형태에서 오탐이 나오면 안 된다."""
        for request in (
            "Assets/Scenes/A.unity 경로에 새 빈 씬을 생성하고, Player(캡슐)를 만들어 "
            "New Input System으로 A/D 좌우 이동과 Space 점프가 되도록 구현해줘. "
            "점프는 한 번 누를 때 한 번만 떠올라 다시 바닥에 착지해야 한다. "
            "이동과 점프가 실제로 되는지 검증까지 끝내줘.",
            "Assets/Scenes/B.unity 경로에 새 빈 씬을 생성하고, Player와 바닥을 만들어 "
            "A/D 좌우 이동을 구현하고, Main Camera가 Player를 따라오게 만들어줘. "
            "이동과 카메라 추종이 실제로 되는지 검증까지 끝내줘.",
            "Assets/Scenes/C.unity 경로에 새 빈 씬을 생성하고, Player와 바닥을 만들어 "
            "A/D 이동을 구현하고, D와 LeftShift를 함께 누르면 부스트로 더 빨라지게 "
            "해줘. 이동과 부스트가 실제로 되는지 검증까지 끝내줘.",
        ):
            with self.subTest(request=request[:30]):
                spec = VerificationSpec.from_request(request)
                self.assertEqual(spec.unmapped_requirements(), [])

    def test_asset_path_words_do_not_trigger_a_requirement(self):
        """씬 이름의 'Grid' 같은 조각이 요구사항으로 잡히면 안 된다."""
        spec = VerificationSpec.from_request(
            "Assets/Scenes/GridProceduralRepro1.unity 경로에 새 빈 씬을 생성하고 "
            "Player가 A/D로 이동하게 구현해줘. 이동이 실제로 되는지 검증해줘."
        )
        self.assertEqual(spec.unmapped_requirements(), [])

    def test_measured_vocabulary_does_not_vouch_for_an_unmeasured_clause(self):
        """이동 어휘가 같은 절의 측정되지 않는 부분까지 보증하고 있었다.

        "이동과 점수 획득이 되는지 검증해줘"는 `이동`이 측정되는 어휘라는 이유로
        미매핑에서 빠졌다. 점수를 재는 검사는 어휘에 없으므로 요청의 절반은
        아무도 보지 않는데 영수증의 ⚠ 줄은 비어 있었다.

        거부권을 낱말 가족이 아니라 **검사 이름**에 묶는 것이 이 결함의 교정이다
        (`_CHECK_COVERAGE`). 점수를 덮는 검사는 아직 없으므로 계속 보고된다.
        """
        spec = VerificationSpec.from_request(
            "Assets/Scenes/S.unity 경로에 새 씬을 생성하고, Player가 A/D로 "
            "이동하게 구현해줘. 이동과 점수 획득이 실제로 되는지 검증까지 끝내줘."
        )
        self.assertIn("점수", " ".join(spec.unmapped_requirements()))

    def test_a_clause_the_new_check_covers_stops_being_reported(self):
        """재는데도 계속 경고하면 ⚠ 줄 전체가 무시당한다."""
        spec = VerificationSpec.from_request(
            "Assets/Scenes/P.unity 경로에 새 씬을 생성하고, Player(캡슐)를 만들어 "
            "A/D 좌우 이동을 구현해줘. 적(빨간 캡슐)을 하나 만들어 좌우로 자동 "
            "순찰하게 해줘."
        )
        self.assertIn("autonomous_motion", spec.requested_checks())
        self.assertEqual(spec.unmapped_requirements(), [])

    def test_fall_respawn_wording_is_recorded(self):
        """policy_lint는 "낙사 시 시작 위치로 복귀" 리터럴에만 걸린다.

        "떨어지면 시작 위치로 되돌아오게"는 조건 표현이 있는데도 결과 어휘가
        마커에 없어 어느 계층에서도 잡히지 않았다.
        """
        spec = VerificationSpec.from_request(
            "Assets/Scenes/R.unity 경로에 새 씬을 생성하고, Player와 바닥을 만들어 "
            "A/D 이동을 구현해줘. 바닥 아래로 떨어지면 Player가 시작 위치로 "
            "되돌아오게 해줘."
        )
        self.assertIn("되돌아", " ".join(spec.unmapped_requirements()))

    def test_keyboard_is_not_a_board(self):
        """`보드`가 "키보드" 안에서, `board`가 "Keyboard.current" 안에서 걸렸다.

        구현 지시문이 요구 조항으로 잡혔고, 그 오탐이 위 거부권의 오탐과 우연히
        상쇄되며 두 결함을 함께 가리고 있었다.
        """
        spec = VerificationSpec.from_request(
            "Assets/Scenes/K.unity 경로에 새 씬을 생성하고 Player가 A/D로 "
            "이동하게 구현해줘. New Input System의 Keyboard.current만 사용하고 "
            "키보드 null 체크를 넣어라. 이동이 실제로 되는지 검증해줘."
        )
        self.assertEqual(spec.unmapped_requirements(), [])

    def test_receipt_records_unmapped_requirements(self):
        spec = VerificationSpec.from_request(
            "Assets/Scenes/A.unity에 새 씬을 만들고 Player가 A/D로 이동하고 "
            "총알이 적에 맞으면 적이 사라지게 구현해줘. 검증까지 끝내줘."
        )
        with tempfile.TemporaryDirectory() as receipts:
            path = write_receipt(receipts, spec, "verified", {}, [], [], 1.0)
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        self.assertTrue(payload["unmapped_requirements"])
        self.assertIn("사라지", " ".join(payload["unmapped_requirements"]))

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

    def _jump_failures(self, rise: float) -> list[str]:
        spec = VerificationSpec.from_request(self.JUMP_FIX_REQUEST)
        with tempfile.TemporaryDirectory() as project:
            contract = VerificationContract(spec=spec, project_dir=project)
            contract.state_seen = True
            contract.scene_clean = True
            contract.compile_checked = True
            contract.played = True
            contract.waited = True
            contract.runtime_checked = True
            contract.jump_before = (0.0, 1.0, 0.0)
            contract.jump_peak_y = 1.0 + rise
            return contract.failures()

    def test_jump_rise_within_bounds_passes(self):
        self.assertNotIn("player_jumped_too_high", self._jump_failures(5.0))
        self.assertNotIn("player_did_not_jump", self._jump_failures(5.0))

    def test_stacked_impulse_jump_is_not_reported_as_success(self):
        """A latch that re-arms while the key is held launches ~4x too high.

        v1.11.8 measured a +19.96 unit rise that never landed and still wrote
        status=verified, because jump had a minimum but no maximum.
        """
        failures = self._jump_failures(19.962933)
        self.assertIn("player_jumped_too_high", failures)
        self.assertEqual(failure_check_name("player_jumped_too_high"), "jump")

    def test_jump_checklist_states_the_upper_bound(self):
        spec = VerificationSpec.from_request(self.JUMP_FIX_REQUEST)
        self.assertTrue(
            any(f"{spec.jump_max_rise:.0f} 이내" in line for line in spec.checklist())
        )


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

    def test_the_null_check_the_gate_teaches_satisfies_the_lint(self):
        """호스트 규칙 둘이 서로 싸우고 있었다.

        `task_contract`의 교정 스니펫은 지역 변수를 거치는 형태를 가르치는데
        (`var keyboard = Keyboard.current; if (keyboard == null) return;`),
        린트는 `Keyboard.current == null`이라는 문자 그대로의 형태만 인정했다.
        게이트가 A를 가르치고 린트가 B를 요구하니 repair가 수렴할 수 없다 —
        실측 `20260809_124847`에서 이 위반이 repair 3사이클을 그대로 통과해
        Play Mode 측정이 전부 blocked됐다.
        """
        shapes = {
            "게이트가 가르치는 형태": (
                "var keyboard = Keyboard.current; if (keyboard == null) return;"
            ),
            "직접 비교": "if (Keyboard.current == null) return;",
            "역방향 비교": "var kb = Keyboard.current; if (kb != null) { }",
            "타입 선언": "Keyboard kb = Keyboard.current; if (kb is null) return;",
        }
        for label, body in shapes.items():
            with self.subTest(label):
                with tempfile.TemporaryDirectory() as project:
                    scripts = os.path.join(project, "Assets", "Scripts")
                    os.makedirs(scripts)
                    path = "Assets/Scripts/PlayerMovement25D.cs"
                    with open(os.path.join(project, path), "w", encoding="utf-8") as handle:
                        handle.write(
                            "using UnityEngine; public class PlayerMovement25D "
                            ": MonoBehaviour { void Update() { " + body + " } }"
                        )
                    self.assertNotIn(
                        f"keyboard_null_check_missing:{path}",
                        lint_scripts("Keyboard.current", [path], project),
                    )

    def test_reading_the_keyboard_without_any_null_check_still_fails(self):
        with tempfile.TemporaryDirectory() as project:
            scripts = os.path.join(project, "Assets", "Scripts")
            os.makedirs(scripts)
            path = "Assets/Scripts/PlayerMovement25D.cs"
            with open(os.path.join(project, path), "w", encoding="utf-8") as handle:
                handle.write(
                    "using UnityEngine; public class PlayerMovement25D : MonoBehaviour {"
                    "void Update() { var keyboard = Keyboard.current; "
                    "float a = keyboard.dKey.isPressed ? 1f : 0f; } }"
                )
            self.assertIn(
                f"keyboard_null_check_missing:{path}",
                lint_scripts("Keyboard.current", [path], project),
            )

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


class ConsoleClassificationTests(unittest.TestCase):
    """A runtime error left in the console must not become a compile error.

    Classifying by "was Play Mode running when we asked" mislabelled
    `Tag: Ground is not defined` — thrown from OnCollisionEnter — as two
    compile errors after Play stopped. `compile_not_ready` then blocked every
    check and the run rolled back to a worse state.
    """

    RUNTIME_ENTRY = {
        "type": "Error",
        "message": "Tag: Ground is not defined.",
        "stackTrace": (
            "UnityEngine.GameObject:CompareTag (string)\n"
            "PlayerMovement:OnCollisionEnter (UnityEngine.Collision)\n"
            "UnityEngine.Physics:OnSceneContact (UnityEngine.PhysicsScene,intptr,int)\n"
        ),
    }
    COMPILE_ENTRY = {
        "type": "Error",
        "message": "Assets/Scripts/PlayerMovement.cs(21,9): error CS1002: ; expected",
        "stackTrace": "",
    }

    def _contract(self):
        spec = VerificationSpec.from_request("A/D 이동을 구현해줘")
        return VerificationContract(spec, "")

    def _play_then_stop(self, contract):
        contract.observe("unity_play_mode", {"action": "play"}, result({"isPlaying": True}))
        contract.observe("unity_wait", {"seconds": 0.5}, result({"waited_seconds": 0.5}))
        contract.observe("unity_play_mode", {"action": "stop"}, result({"isPlaying": False}))

    def test_runtime_error_read_after_stop_is_not_a_compile_error(self):
        contract = self._contract()
        self._play_then_stop(contract)
        contract.observe(
            "unity_read_console",
            {"types": "error,exception"},
            result({"entries": [self.RUNTIME_ENTRY]}),
        )
        self.assertEqual(contract.compile_error_count, 0)
        self.assertEqual(contract.compile_errors, [])

    def test_that_runtime_error_is_still_counted_as_a_runtime_failure(self):
        contract = self._contract()
        self._play_then_stop(contract)
        contract.observe(
            "unity_read_console",
            {"types": "error,exception"},
            result({"entries": [self.RUNTIME_ENTRY]}),
        )
        self.assertEqual(contract.runtime_error_count, 1)

    def test_real_compiler_diagnostic_is_still_a_compile_error(self):
        contract = self._contract()
        contract.observe(
            "unity_read_console",
            {"types": "error"},
            result({"entries": [self.COMPILE_ENTRY]}),
        )
        self.assertEqual(contract.compile_error_count, 1)

    def test_mixed_entries_are_split_by_origin(self):
        contract = self._contract()
        self._play_then_stop(contract)
        contract.observe(
            "unity_read_console",
            {"types": "error"},
            result({"entries": [self.RUNTIME_ENTRY, self.COMPILE_ENTRY]}),
        )
        self.assertEqual(contract.compile_error_count, 1)
        self.assertEqual(
            contract.compile_errors[0]["message"], self.COMPILE_ENTRY["message"]
        )
        self.assertEqual(contract.runtime_error_count, 1)


class EvidenceTests(unittest.TestCase):
    def test_play_transition_false_before_first_active_is_not_an_unexpected_end(self):
        spec = VerificationSpec.from_request("A/D 이동과 Space 점프를 구현해줘")
        contract = VerificationContract(spec, "")
        state = {
            "activeScene": {
                "path": "Assets/Scenes/Game.unity",
                "isDirty": False,
            }
        }

        contract.observe(
            "unity_play_mode", {"action": "play"}, result({"isPlaying": True})
        )
        contract.observe(
            "unity_get_state", {}, result({"isPlaying": False, **state})
        )
        self.assertFalse(contract.play_ended_unexpectedly)
        self.assertFalse(contract.final_stopped)

        contract.observe(
            "unity_get_state", {}, result({"isPlaying": True, **state})
        )
        contract.observe(
            "unity_get_state", {}, result({"isPlaying": False, **state})
        )
        self.assertTrue(contract.play_ended_unexpectedly)
        self.assertTrue(contract.final_stopped)

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
            contract.observe("unity_get_hierarchy", {"max_depth": 3}, result({
                "scenes": [{
                    "name": "Game",
                    "path": "Assets/Scenes/Game.unity",
                    "rootObjects": [
                        {"name": "Main Camera", "active": True,
                         "components": ["Transform", "Camera"]},
                        {"name": "Directional Light", "active": True,
                         "components": ["Transform", "Light"]},
                        {"name": "Player", "active": True,
                         "components": ["Transform", "MeshFilter", "MeshRenderer",
                                        "CapsuleCollider", "Rigidbody"]},
                        {"name": "Ground", "active": True,
                         "components": ["Transform", "MeshRenderer"]},
                    ],
                }],
            }))
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
    def __init__(self, dirty=False, roots=None):
        self.tool_mode = "full"
        self.ollama_tools = []
        self.modes = []
        self.calls = []
        self.call_modes = []
        self.dirty = dirty
        self.roots = roots

    def set_tool_mode(self, mode):
        self.tool_mode = mode
        self.modes.append(mode)

    async def call(self, name, args):
        self.calls.append((name, args))
        self.call_modes.append((self.tool_mode, name, args))
        if name == "unity_get_state":
            return result({
                "isPlaying": False,
                "activeScene": {"path": "Assets/Scenes/Test.unity", "isDirty": self.dirty},
            })
        if name == "unity_read_console":
            return result({"entries": []})
        if name == "unity_get_hierarchy" and self.roots is not None:
            return result({"scenes": [{
                "name": "Test", "path": "Assets/Scenes/Test.unity",
                "rootObjects": list(self.roots),
            }]})
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


class MovementSpecTests(unittest.TestCase):
    """v1.11.4 회귀: 검증이 요청에 없는 키를 쓰거나 폭주 물리를 통과시키던 문제."""

    def test_ad_scheme_is_extracted_from_request(self):
        spec = VerificationSpec.from_request(
            "New Input System으로 A/D 좌우 이동과 Space 점프가 되도록 구현해줘"
        )
        self.assertEqual(spec.move_right_key, "d")
        self.assertEqual(spec.move_left_key, "a")
        self.assertTrue(spec.movement_keys_explicit)

    def test_arrow_scheme_is_extracted(self):
        spec = VerificationSpec.from_request("방향키로 좌우 이동하게 만들어줘")
        self.assertEqual(spec.move_right_key, "rightArrow")
        self.assertTrue(spec.movement_keys_explicit)

    def test_unspecified_scheme_is_not_explicit(self):
        spec = VerificationSpec.from_request("플랫포머 게임 만들어줘")
        self.assertEqual(spec.move_right_key, "rightArrow")
        self.assertFalse(spec.movement_keys_explicit)

    def _movement_contract(self, request, project, label, before, after):
        spec = VerificationSpec.from_request(request)
        contract = VerificationContract(spec, project)
        contract.state_seen = True
        contract.scene_clean = True
        contract.compile_checked = True
        contract.played = contract.waited = contract.runtime_checked = True
        contract.play_active_confirmed = True
        contract.input_released = contract.final_stopped = True
        contract.observed_components["Player"] = [
            "UnityEngine.Rigidbody", "UnityEngine.CapsuleCollider",
        ]
        contract.motion_before[label] = before
        contract.motion_after[label] = after
        contract.motion_duration[label] = 1.0
        return contract

    def test_runaway_movement_is_rejected(self):
        """실측 E2E에서 1초에 131유닛 이동이 통과하던 문제."""
        with tempfile.TemporaryDirectory() as project:
            contract = self._movement_contract(
                "플랫포머에서 이동을 고쳐줘", project,
                "rightArrow", (0.0, 1.0, 0.0), (131.8, 1.0, 0.0),
            )
            self.assertIn("player_moved_too_far", contract.failures())

    def test_plausible_movement_passes(self):
        with tempfile.TemporaryDirectory() as project:
            contract = self._movement_contract(
                "플랫포머에서 이동을 고쳐줘", project,
                "rightArrow", (0.0, 1.0, 0.0), (5.0, 1.0, 0.0),
            )
            failures = contract.failures()
            self.assertNotIn("player_moved_too_far", failures)
            self.assertNotIn("player_did_not_move_right", failures)

    def test_ad_request_accepts_the_d_sample_for_movement(self):
        """A/D 요청에서 rightArrow 표본이 없어도 d 표본으로 이동을 인정한다."""
        with tempfile.TemporaryDirectory() as project:
            contract = self._movement_contract(
                "A/D 좌우 이동을 고쳐줘", project,
                "d", (0.0, 1.0, 0.0), (4.0, 1.0, 0.0),
            )
            contract.motion_before["a"] = (4.0, 1.0, 0.0)
            contract.motion_after["a"] = (0.0, 1.0, 0.0)
            contract.motion_duration["a"] = 1.0
            failures = contract.failures()
            self.assertNotIn("player_did_not_move_right", failures)
            self.assertNotIn("player_movement_not_measured", failures)

    def test_runaway_bidirectional_movement_is_rejected(self):
        with tempfile.TemporaryDirectory() as project:
            contract = self._movement_contract(
                "A/D 좌우 이동을 고쳐줘", project,
                "d", (0.0, 1.0, 0.0), (126.7, 1.0, 0.0),
            )
            contract.motion_before["a"] = (126.7, 1.0, 0.0)
            contract.motion_after["a"] = (-5.1, 1.0, 0.0)
            contract.motion_duration["a"] = 1.0
            failures = contract.failures()
            self.assertIn("d_moved_too_far", failures)
            self.assertIn("a_moved_too_far", failures)

    def test_jump_key_is_extracted_from_request(self):
        self.assertEqual(
            VerificationSpec.from_request("W키로 점프하게 만들어줘").jump_key, "w"
        )
        self.assertEqual(
            VerificationSpec.from_request("위쪽 방향키로 점프하게 만들어줘").jump_key,
            "upArrow",
        )
        # 기본값과 명시적 space 요청 모두 space
        self.assertEqual(
            VerificationSpec.from_request("플랫포머 게임 만들어줘").jump_key, "space"
        )
        self.assertEqual(
            VerificationSpec.from_request("Space 점프를 고쳐줘").jump_key, "space"
        )

    def test_jump_checklist_uses_the_requested_key(self):
        spec = VerificationSpec.from_request("W키로 점프하게 만들어줘")
        self.assertTrue(any("w 입력 전후" in c for c in spec.checklist()))

    def test_moved_too_far_maps_to_its_check(self):
        self.assertEqual(failure_check_name("player_moved_too_far"), "movement")
        self.assertEqual(failure_check_name("d_moved_too_far"), "bidirectional")
        self.assertEqual(failure_check_name("a_moved_too_far"), "bidirectional")


class StubContract:
    """Minimal stand-in so a repair cycle can be driven without Unity."""

    def __init__(self, failures, measured):
        self._failures = list(failures)
        self._measured = list(measured)

    def failures(self):
        return list(self._failures)

    def measured_checks(self):
        return list(self._measured)

    def evidence(self):
        return {}

    def check_report(self):
        return {
            "requested_checks": self._measured,
            "measured_checks": self._measured,
            "skipped_checks": [],
        }


class ScriptedVerificationAgent(Agent):
    """Replays a fixed sequence of verification outcomes across repair cycles."""

    def __init__(self, contracts, edits=None, scene_edits=None, scene_path=None):
        super().__init__(
            HostTools(), lambda *_: None, lambda *_: None, lambda *_: None,
            enable_logging=False, enable_verification=True,
        )
        self.contracts = iter(contracts)
        # Per-cycle file writes so a rollback has something real to undo.
        self.edits = iter(edits or [])
        self.scene_edits = iter(scene_edits or [])
        self.scene_path = scene_path
        self._turn_mutation_count = 1  # repair loop demands mutation evidence
        self.cycles_run = 0

    async def _collect_verification(self, spec):
        self.tools.set_tool_mode("verify")
        return next(self.contracts)

    async def _react_loop(self, messages, contract, max_iters, ledger=None):
        self.cycles_run += 1
        edit = next(self.edits, None)
        if edit:
            path = os.path.join(config.UNITY_PROJECT_DIR, "Assets", "Scripts", "Player.cs")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(edit)
        scene_edit = next(self.scene_edits, None)
        if scene_edit and self.scene_path:
            path = os.path.join(
                config.UNITY_PROJECT_DIR, *self.scene_path.split("/")
            )
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(scene_edit)
        return True, "", 1


def _run_orchestration(
    contracts, request="점프를 고쳐줘", edits=None, seed=None, scene_seed=None,
    scene_edits=None,
):
    spec = VerificationSpec.from_request(request)
    agent = ScriptedVerificationAgent(
        contracts, edits, scene_edits, scene_path=spec.scene_path
    )
    with tempfile.TemporaryDirectory() as project, \
         tempfile.TemporaryDirectory() as receipts, \
         mock.patch.object(config, "UNITY_PROJECT_DIR", project), \
         mock.patch.object(config, "VERIFICATION_RECEIPT_DIR", receipts):
        if seed is not None:
            path = os.path.join(project, "Assets", "Scripts", "Player.cs")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(seed)
        if scene_seed is not None and spec.scene_path:
            scene_path = os.path.join(project, *spec.scene_path.split("/"))
            os.makedirs(os.path.dirname(scene_path), exist_ok=True)
            with open(scene_path, "w", encoding="utf-8") as handle:
                handle.write(scene_seed)
        success = asyncio.run(
            agent._run_verification_orchestration(spec, None, time.monotonic(), True)
        )
        receipt = json.load(open(agent.last_verification_receipt_path, encoding="utf-8"))
        final_script = None
        script_path = os.path.join(project, "Assets", "Scripts", "Player.cs")
        if os.path.exists(script_path):
            with open(script_path, encoding="utf-8") as handle:
                final_script = handle.read()
    return success, receipt, agent, final_script


class BuilderStageCompletionTests(unittest.TestCase):
    def _run_builder_audit(self, contracts, model_loop_completed=True):
        spec = VerificationSpec.from_request(
            "Assets/Scenes/Test.unity에서 A/D 이동과 Space 점프를 구현해줘"
        )
        agent = ScriptedVerificationAgent(contracts)
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as receipts, \
             mock.patch.object(config, "UNITY_PROJECT_DIR", project), \
             mock.patch.object(config, "VERIFICATION_RECEIPT_DIR", receipts):
            success = asyncio.run(
                agent._run_verification_orchestration(
                    spec, model_loop_completed, time.monotonic(), True
                )
            )
            with open(agent.last_verification_receipt_path, encoding="utf-8") as handle:
                receipt = json.load(handle)
        return success, receipt, agent

    def test_builder_save_barrier_runs_before_first_host_verification(self):
        success, receipt, agent = self._run_builder_audit([
            StubContract([], ["gameplay", "movement", "bidirectional", "jump"]),
        ])
        self.assertTrue(success)
        self.assertTrue(receipt["build_stage_success"])
        self.assertEqual(
            agent.tools.call_modes[:2],
            [
                ("full", "unity_get_state", {}),
                ("full", "unity_save_scene", {"path": "Assets/Scenes/Test.unity"}),
            ],
        )
        self.assertEqual(len(receipt["attempts"]), 1)

    def test_builder_stage_is_false_when_first_host_verification_needs_repair(self):
        success, receipt, _ = self._run_builder_audit([
            StubContract(
                ["player_did_not_jump"],
                ["gameplay", "movement", "bidirectional", "jump"],
            ),
            StubContract([], ["gameplay", "movement", "bidirectional", "jump"]),
        ])
        self.assertTrue(success)
        self.assertFalse(receipt["build_stage_success"])
        self.assertEqual(len(receipt["attempts"]), 2)

    def test_builder_stage_uses_first_host_audit_when_model_loop_exhausts(self):
        success, receipt, _ = self._run_builder_audit(
            [StubContract([], ["gameplay", "movement", "bidirectional", "jump"])],
            model_loop_completed=False,
        )
        self.assertTrue(success)
        self.assertTrue(receipt["build_stage_success"])


class RegressionDetectionTests(unittest.TestCase):
    """P1 회귀: 최초 측정을 회귀로 오판해 repair 예산을 조기 소진하던 문제.

    실측 E2E(20260729_094913)에서 repair 1회 만에 verification_regressed로
    중단됐다. 원인은 compile/Rigidbody가 고쳐져 점프·이동이 '처음으로 측정
    가능해진' 것을 '새로 생긴 실패'로 본 것이다.
    """

    def test_failure_check_name_maps_behaviour_failures(self):
        self.assertEqual(failure_check_name("player_did_not_jump"), "jump")
        self.assertEqual(failure_check_name("player_did_not_land"), "jump_landing")
        self.assertEqual(failure_check_name("player_did_not_move_right"), "movement")
        self.assertEqual(failure_check_name("d_did_not_move_right"), "bidirectional")
        self.assertEqual(failure_check_name("a_did_not_move_left"), "bidirectional")
        self.assertEqual(failure_check_name("camera_did_not_follow"), "camera_follow")
        self.assertEqual(failure_check_name("boost_distance_too_short"), "boost")
        self.assertEqual(
            failure_check_name("left_boost_distance_too_short"), "left_boost"
        )
        self.assertEqual(failure_check_name("component_missing:Player:Rigidbody"),
                         "components:Player")
        # Static checks are never blocked, so they have no owning check.
        self.assertIsNone(failure_check_name("scene_not_saved"))
        self.assertIsNone(failure_check_name("compile_errors:2"))

    def test_failure_count_parses_only_counted_codes(self):
        self.assertEqual(failure_count("compile_errors:3"), ("compile_errors", 3))
        self.assertEqual(failure_count("runtime_errors:1"), ("runtime_errors", 1))
        self.assertIsNone(failure_count("scene_not_saved"))
        self.assertIsNone(failure_count("component_missing:Player:Rigidbody"))

    def test_first_measurement_after_unblock_is_not_a_regression(self):
        success, receipt, agent, _ = _run_orchestration([
            # 1차: 컴파일이 깨져 행동 검사가 전부 차단된 상태
            StubContract(
                ["compile_errors:1", "blocked:jump:compile_not_ready",
                 "component_missing:Player:Rigidbody"],
                [],
            ),
            # repair 후: 처음으로 점프를 측정했고, 실패했다 → 회귀가 아님
            StubContract(["player_did_not_jump"], ["gameplay", "jump"]),
            # 두 번째 repair가 실제로 점프를 고친다
            StubContract([], ["gameplay", "jump"]),
        ])
        self.assertTrue(success, "최초 측정 실패로 중단하면 안 된다")
        self.assertNotIn("verification_regressed", receipt["failures"])
        self.assertEqual(agent.cycles_run, 2, "repair가 2회 돌아야 한다")

    def test_genuine_regression_still_stops(self):
        success, receipt, agent, _ = _run_orchestration([
            # 점프는 이미 측정되어 통과 중이었다
            StubContract(["scene_not_saved"], ["gameplay", "jump"]),
            # repair 후 통과하던 점프가 깨졌다 → 진짜 회귀
            StubContract(["player_did_not_jump"], ["gameplay", "jump"]),
        ])
        self.assertFalse(success)
        self.assertIn("verification_regressed", receipt["failures"])
        self.assertEqual(agent.cycles_run, 1)

    def test_fewer_compile_errors_is_progress_not_regression(self):
        success, receipt, _, _ = _run_orchestration([
            StubContract(["compile_errors:3"], []),
            StubContract(["compile_errors:1"], []),
            StubContract([], []),
        ])
        self.assertTrue(success)
        self.assertNotIn("verification_regressed", receipt["failures"])

    def test_rising_compile_errors_is_a_regression(self):
        success, receipt, _, _ = _run_orchestration([
            StubContract(["compile_errors:1"], []),
            StubContract(["compile_errors:5"], []),
        ])
        self.assertFalse(success)
        self.assertIn("verification_regressed", receipt["failures"])

    def test_newly_broken_static_check_is_a_regression(self):
        """repair가 씬을 저장하지 않고 끝내면 그것은 진짜 회귀다."""
        success, receipt, _, _ = _run_orchestration([
            StubContract(["player_did_not_jump"], ["gameplay", "jump"]),
            StubContract(["scene_not_saved"], ["gameplay", "jump"]),
        ])
        self.assertFalse(success)
        self.assertIn("verification_regressed", receipt["failures"])

    def test_changed_failure_mode_within_same_check_can_continue(self):
        """실측 20260729_115957: 폭주 이동을 줄인 뒤 과소 이동이 되자 회귀로 멈췄다."""
        success, receipt, agent, _ = _run_orchestration(
            [
                StubContract(
                    ["d_moved_too_far", "a_moved_too_far"],
                    ["gameplay", "movement", "bidirectional"],
                ),
                StubContract(
                    ["d_did_not_move_right", "a_did_not_move_left"],
                    ["gameplay", "movement", "bidirectional"],
                ),
                StubContract([], ["gameplay", "movement", "bidirectional"]),
            ],
            request="A/D 좌우 이동을 구현해줘",
        )
        self.assertTrue(success)
        self.assertNotIn("verification_regressed", receipt["failures"])
        self.assertEqual(agent.cycles_run, 2)


class RepairAbortTests(unittest.TestCase):
    """모델 호출 하나가 끊겨 완료된 측정이 통째로 사라졌다.

    2026-08-05 실행에서 검증이 `undefined_compare_tag:...:Coin`을 정확히 잡아
    놓고, repair 첫 사이클의 Ollama 호출이 끊기자(`XML syntax error ... unexpected
    EOF`) 예외가 영수증 기록 지점을 건너뛰었다. 기록된 실행 166개 중 4건이 같은
    방식으로 완료된 측정을 잃었다.
    """

    class _Exploding(ScriptedVerificationAgent):
        async def _react_loop(self, messages, contract, max_iters, ledger=None):
            raise RuntimeError("XML syntax error on line 3: unexpected EOF")

    def _run(self):
        spec = VerificationSpec.from_request("점프를 고쳐줘")
        agent = self._Exploding(
            [StubContract(["player_did_not_jump"], ["gameplay", "jump"])]
        )
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as receipts, \
             mock.patch.object(config, "UNITY_PROJECT_DIR", project), \
             mock.patch.object(config, "VERIFICATION_RECEIPT_DIR", receipts):
            success = asyncio.run(
                agent._run_verification_orchestration(spec, None, time.monotonic(), True)
            )
            with open(agent.last_verification_receipt_path, encoding="utf-8") as handle:
                return success, json.load(handle)

    def test_the_measurement_survives_a_dead_model_call(self):
        success, receipt = self._run()
        self.assertFalse(success)
        # 예외 전에 끝난 측정이 그대로 남아 있어야 한다
        self.assertEqual(receipt["measured_checks"], ["gameplay", "jump"])
        self.assertIn("player_did_not_jump", receipt["failures"])

    def test_the_abort_is_named_in_the_receipt(self):
        _, receipt = self._run()
        self.assertIn("repair_aborted:RuntimeError", receipt["failures"])

    def test_the_abort_is_not_counted_as_a_project_defect(self):
        """롤백 판정이 이것을 결함으로 세면 멀쩡한 사이클을 되돌린다."""
        scored = Agent._repair_score(
            ["repair_aborted:RuntimeError", "player_did_not_jump"],
            measured_checks=["gameplay", "jump"],
            requested_checks=["gameplay", "jump"],
        )
        only_defect = Agent._repair_score(
            ["player_did_not_jump"],
            measured_checks=["gameplay", "jump"],
            requested_checks=["gameplay", "jump"],
        )
        self.assertEqual(scored, only_defect)


class RepairRollbackTests(unittest.TestCase):
    """P1: 자동 수정이 상태를 악화시키면 최선 상태로 되돌린다.

    실측 E2E(20260729_094913)에서 사이클 3이 실패 1건이었는데 사이클 4가
    2건으로 끝나, 더 나쁜 상태가 프로젝트에 남았다.
    """

    def test_worse_final_state_is_rolled_back_to_best(self):
        success, receipt, _, final = _run_orchestration(
            [
                # verify 1: 2건
                StubContract(["player_did_not_move_right", "player_did_not_jump"],
                             ["gameplay", "movement", "jump"]),
                # reverify 2: 1건 — 최선 상태
                StubContract(["player_did_not_move_right"],
                             ["gameplay", "movement", "jump"]),
                # reverify 3: 점프가 다시 깨짐 → 회귀
                StubContract(["player_did_not_move_right", "player_did_not_jump"],
                             ["gameplay", "movement", "jump"]),
                # rollback 후 재검증
                StubContract(["player_did_not_move_right"],
                             ["gameplay", "movement", "jump"]),
            ],
            seed="original",
            edits=["fixed jump", "broke jump again"],
        )
        self.assertFalse(success)
        self.assertIn("verification_rolled_back", receipt["failures"])
        # 회귀를 만든 편집이 디스크에서 사라지고 최선 상태가 남는다
        self.assertEqual(final, "fixed jump")
        rollback = [a for a in receipt["attempts"] if a["phase"] == "rollback"]
        self.assertEqual(len(rollback), 1)
        self.assertEqual(rollback[0]["restored_from"], "reverify 2")
        self.assertIn("Assets/Scripts/Player.cs", rollback[0]["restored_files"])

    def test_improving_run_is_not_rolled_back(self):
        success, receipt, _, final = _run_orchestration(
            [
                StubContract(["player_did_not_move_right", "player_did_not_jump"],
                             ["gameplay", "movement", "jump"]),
                StubContract([], ["gameplay", "movement", "jump"]),
            ],
            seed="original",
            edits=["fixed everything"],
        )
        self.assertTrue(success)
        self.assertNotIn("verification_rolled_back", receipt["failures"])
        self.assertEqual(final, "fixed everything")

    def test_rollback_can_be_disabled(self):
        with mock.patch.object(config, "REPAIR_ROLLBACK", False):
            success, receipt, _, final = _run_orchestration(
                [
                    StubContract(["player_did_not_move_right", "player_did_not_jump"],
                                 ["gameplay", "movement", "jump"]),
                    StubContract(["player_did_not_move_right"],
                                 ["gameplay", "movement", "jump"]),
                    StubContract(["player_did_not_move_right", "player_did_not_jump"],
                                 ["gameplay", "movement", "jump"]),
                ],
                seed="original",
                edits=["fixed jump", "broke jump again"],
            )
        self.assertFalse(success)
        self.assertNotIn("verification_rolled_back", receipt["failures"])
        self.assertEqual(final, "broke jump again", "롤백이 꺼지면 악화 상태가 남는다")

    def test_score_prefers_fewer_real_defects(self):
        self.assertLess(
            Agent._repair_score(["player_did_not_jump"]),
            Agent._repair_score(["player_did_not_jump", "player_did_not_move_right"]),
        )

    def test_score_ignores_orchestration_markers(self):
        """실측 20260729_102619 회귀: 루프 마커가 점수를 부풀려 올바른 수정을 되돌렸다.

        no_verification_progress는 프로젝트 파일의 결함이 아니라 루프의 판단이므로
        어느 사이클의 파일을 남길지 고르는 데 영향을 주면 안 된다.
        """
        for marker in (
            "no_verification_progress", "task_time_budget_exhausted",
            "verification_regressed", "builder_produced_no_mutation_evidence",
        ):
            with self.subTest(marker=marker):
                self.assertEqual(
                    Agent._repair_score(["player_did_not_jump", marker]),
                    Agent._repair_score(["player_did_not_jump"]),
                )

    def test_unmeasured_checks_are_not_counted_twice(self):
        """"재지 못했다"는 실패는 결함이 아니라 측정 공백이고 첫 키가 이미 센다.

        2026-08-05 실행(영수증 20260805_102127)에서 두 번 세는 바람에 롤백이
        역전됐다. repair가 정책 위반을 고친 상태(실제 결함 0 · blocked 0)를 두고,
        위반이 남고 12개가 blocked된 최초 상태로 되돌렸다 — 되돌린 근거가
        `player_movement_not_measured` 류 5건이었고 그것들은 미측정 4에 이미
        포함돼 있었다.
        """
        requested = ["gameplay", "movement", "bidirectional", "jump"]
        measured = []          # 아무것도 못 쟀다 — 미측정 4
        gaps = [
            "runtime_console_not_checked", "player_movement_not_measured",
            "d_movement_not_measured", "a_movement_not_measured",
            "player_jump_not_measured",
        ]
        self.assertEqual(
            Agent._repair_score(gaps, measured, requested),
            Agent._repair_score([], measured, requested),
        )

    def test_a_repaired_state_beats_one_that_still_violates_policy(self):
        """실측된 역전 그대로 — 고쳐진 쪽이 이겨야 한다."""
        requested = ["gameplay", "movement", "bidirectional", "jump"]
        measured = []
        before = Agent._repair_score(
            ["policy_lint:undefined_compare_tag:Assets/Scripts/CoinCollector.cs:Coin"]
            + [f"blocked:{stage}:policy_lint_failed"
               for stage in ("gameplay", "movement", "jump", "boost")],
            measured, requested,
        )
        after = Agent._repair_score(
            ["runtime_console_not_checked", "player_movement_not_measured",
             "d_movement_not_measured", "a_movement_not_measured",
             "player_jump_not_measured"],
            measured, requested,
        )
        self.assertLess(after, before)

    def test_score_prefers_more_measured_state_on_a_tie(self):
        self.assertLess(
            Agent._repair_score(["player_did_not_jump"]),
            Agent._repair_score(["player_did_not_jump", "blocked:boost:scene_not_ready"]),
        )

    def test_score_prefers_measured_failures_over_blocked_unknown_state(self):
        requested = ["gameplay", "movement", "jump"]
        self.assertLess(
            Agent._repair_score(
                ["player_did_not_move_right", "player_did_not_jump"],
                requested,
                requested,
            ),
            Agent._repair_score(
                [
                    "blocked:gameplay:scene_not_ready",
                    "blocked:movement:scene_not_ready",
                    "blocked:jump:scene_not_ready",
                ],
                [],
                requested,
            ),
        )

    def test_blocked_initial_state_is_not_restored_over_measured_repairs(self):
        success, receipt, agent, final = _run_orchestration(
            [
                StubContract(
                    [
                        "blocked:gameplay:scene_not_ready",
                        "blocked:jump:scene_not_ready",
                        "scene_not_saved",
                    ],
                    [],
                ),
                StubContract(["player_did_not_jump"], ["gameplay", "jump"]),
                StubContract(["player_did_not_jump"], ["gameplay", "jump"]),
                StubContract(["player_did_not_jump"], ["gameplay", "jump"]),
            ],
            seed="blocked original",
            edits=["measured repair 1", "measured repair 2", "measured repair 3"],
        )
        self.assertFalse(success)
        self.assertNotIn("verification_rolled_back", receipt["failures"])
        self.assertEqual(final, "measured repair 3")
        self.assertFalse(
            any(
                mode == "verify" and name == "unity_refresh_assets"
                for mode, name, _ in agent.tools.call_modes
            )
        )

    def test_no_rollback_when_only_a_loop_marker_differs(self):
        """수정이 진전을 못 냈을 뿐인데 되돌려서 모델의 수정을 버리면 안 된다."""
        success, receipt, _, final = _run_orchestration(
            [
                StubContract(["player_did_not_jump"], ["gameplay", "jump"]),
                StubContract(["player_did_not_jump"], ["gameplay", "jump"]),
                StubContract(["player_did_not_jump"], ["gameplay", "jump"]),
            ],
            seed="broken",
            edits=["model attempted fix", "model attempted fix 2"],
        )
        self.assertFalse(success)
        self.assertNotIn("verification_rolled_back", receipt["failures"])
        self.assertNotEqual(final, "broken", "모델의 수정이 보존돼야 한다")

    def test_rollback_refresh_temporarily_restores_full_tool_mode(self):
        _, receipt, agent, _ = _run_orchestration(
            [
                StubContract(
                    ["player_did_not_move_right", "player_did_not_jump"],
                    ["gameplay", "movement", "jump"],
                ),
                StubContract(
                    ["player_did_not_move_right"],
                    ["gameplay", "movement", "jump"],
                ),
                StubContract(
                    ["player_did_not_move_right", "player_did_not_jump"],
                    ["gameplay", "movement", "jump"],
                ),
                StubContract(
                    ["player_did_not_move_right"],
                    ["gameplay", "movement", "jump"],
                ),
            ],
            request="Assets/Scenes/Test.unity 씬에서 이동과 점프를 고쳐줘",
            seed="original",
            edits=["fixed jump", "broke jump again"],
            scene_seed="original scene",
            scene_edits=["fixed scene", "broken scene"],
        )
        self.assertIn("verification_rolled_back", receipt["failures"])
        refresh_modes = [
            mode for mode, name, _ in agent.tools.call_modes
            if name == "unity_refresh_assets"
        ]
        self.assertEqual(refresh_modes, ["full"])
        open_calls = [
            (mode, args) for mode, name, args in agent.tools.call_modes
            if name == "unity_open_scene"
        ]
        self.assertEqual(
            open_calls,
            [("full", {"path": "Assets/Scenes/Test.unity"})],
        )


class RepairPromptGuidanceTests(unittest.TestCase):
    def test_movement_failure_explains_unwired_input_callbacks(self):
        spec = VerificationSpec.from_request(
            "A/D 좌우 이동과 Space 점프를 구현해줘"
        )
        prompt = fix_prompt(
            spec,
            [
                "player_did_not_move_right",
                "d_did_not_move_right",
                "a_did_not_move_left",
            ],
            {},
        )
        self.assertIn("OnMove(InputValue)가 호출되지 않는다", prompt)
        self.assertIn("Keyboard.current", prompt)
        self.assertIn("aKey/dKey.isPressed", prompt)
        self.assertIn("PlayerInput, InputActionAsset", prompt)
        self.assertIn("해당 컴포넌트가 이미 있으면 제거", prompt)
        self.assertIn("legacy UnityEngine.Input API는 사용하지 않는다", prompt)

    def test_jump_failure_requires_rising_edge_latch(self):
        spec = VerificationSpec.from_request("Space 점프를 구현해줘")
        prompt = fix_prompt(spec, ["player_did_not_jump"], {})
        self.assertIn("pressed && !jumpHeld", prompt)
        self.assertIn("jumpRequested = true", prompt)
        self.assertIn("jumpHeld = pressed", prompt)
        self.assertIn("wasPressedThisFrame", prompt)

    def test_excessive_jump_failure_blames_repeated_impulse(self):
        spec = VerificationSpec.from_request("Space 점프를 구현해줘")
        prompt = fix_prompt(spec, ["player_jumped_too_high"], {})
        self.assertIn("impulse", prompt)
        self.assertIn("pressed && !jumpHeld", prompt)


class UserWordingExtractionTests(unittest.TestCase):
    """사용자가 실제로 입력한 문장 하나가 드러낸 추출 공백 넷.

    "3층짜리 플랫폼을 만들어. ad키로 좌우로 움직이고, space가 점프, 좌쉬프트키를
    누르면 순간적으로 방향키 방향으로 가속하게해. 카메라를 추가해서 플레이어를
    추적하게해."

    이 요청은 검사 4개만 뽑혔고 이동을 **방향키로** 측정하려 했다. A/D로 올바르게
    구현한 게임을 하네스가 떨어뜨리는 상태였다.
    """

    REQUEST = (
        "3층짜리 플랫폼을 만들어. ad키로 좌우로 움직이고, space가 점프, "
        "좌쉬프트키를 누르면 순간적으로 방향키 방향으로 가속하게해. "
        "카메라를 추가해서 플레이어를 추적하게해. "
    )

    def setUp(self):
        self.spec = VerificationSpec.from_request(self.REQUEST)

    def test_ad_without_a_separator_is_the_ad_scheme(self):
        """'ad키'에는 구분자가 없어 A/D 정규식이 놓쳤고, '방향키 방향으로 가속'의
        '방향키'가 대신 잡혀 화살표 스킴으로 측정하려 했다."""
        self.assertEqual((self.spec.move_right_key, self.spec.move_left_key), ("d", "a"))
        self.assertTrue(self.spec.movement_keys_explicit)

    def test_bidirectional_survives_the_ad_spelling(self):
        """'ad키'에는 \\ba\\b도 \\bd\\b도 없어 양방향 검사가 빠졌다."""
        self.assertTrue(self.spec.require_bidirectional)

    def test_korean_shift_spelling_enables_boost(self):
        """부스트 문맥어가 라틴 'shift'뿐이라 '좌쉬프트키'를 놓쳤다."""
        self.assertTrue(self.spec.require_boost)

    def test_tracking_wording_enables_camera_follow(self):
        """추종 어휘에 '추적'이 없어 카메라 검사가 하나도 만들어지지 않았다."""
        self.assertTrue(self.spec.require_camera_follow)
        self.assertIn("Main Camera", self.spec.required_components)

    def test_floor_count_is_reported_as_unmapped(self):
        """층수를 세는 검사는 어휘에 없다. 없다는 사실이 보여야 한다."""
        unmapped = self.spec.unmapped_requirements()
        self.assertTrue(unmapped)
        self.assertIn("3층", " ".join(unmapped))

    def test_every_other_requirement_is_covered(self):
        for name in ("movement", "bidirectional", "jump", "boost", "camera_follow"):
            with self.subTest(check=name):
                self.assertIn(name, self.spec.requested_checks())


class CameraViewpointTests(unittest.TestCase):
    """플레이어에 붙은 시점 카메라는 추종 검사를 완벽하게 통과한다.

    사용자가 직접 돌려보고 "카메라가 관찰이 아니라 시점으로 되어 있다"고 보고했다.
    변위 비교만으로는 구분할 수 없다 — 붙어 있으면 델타가 정확히 같다.
    """

    def _contract(self, camera_start, camera_end, player_start, player_end):
        spec = VerificationSpec.from_request(
            "A/D 이동과 카메라가 Player를 추적하게 구현해줘"
        )
        contract = VerificationContract(spec=spec, project_dir="")
        contract.motion_before["d"] = player_start
        contract.motion_after["d"] = player_end
        contract.camera_motion_before["d"] = camera_start
        contract.camera_motion_after["d"] = camera_end
        return contract

    def test_camera_sitting_on_the_player_is_not_observing_it(self):
        contract = self._contract(
            (0.0, 1.0, 0.0), (5.0, 1.0, 0.0), (0.0, 1.0, 0.0), (5.0, 1.0, 0.0)
        )
        self.assertIn("camera_is_player_viewpoint", contract.failures())

    def test_camera_behind_and_above_the_player_passes(self):
        contract = self._contract(
            (0.0, 3.0, -10.0), (5.0, 3.0, -10.0), (0.0, 1.0, 0.0), (5.0, 1.0, 0.0)
        )
        failures = contract.failures()
        self.assertNotIn("camera_is_player_viewpoint", failures)
        self.assertNotIn("camera_did_not_follow", failures)

    def test_gap_is_recorded_in_evidence_so_a_receipt_can_be_reread(self):
        contract = self._contract(
            (0.0, 3.0, -10.0), (5.0, 3.0, -10.0), (0.0, 1.0, 0.0), (5.0, 1.0, 0.0)
        )
        self.assertAlmostEqual(contract.evidence()["camera_player_gap"], 10.198, places=2)

    def test_the_failure_explains_the_offset_instead_of_naming_a_label(self):
        spec = VerificationSpec.from_request("카메라가 Player를 추적하게 해줘")
        prompt = fix_prompt(spec, ["camera_is_player_viewpoint"], {})
        self.assertIn("자식", prompt)
        self.assertIn("offset", prompt)


class BoostRepairGuidanceTests(unittest.TestCase):
    def test_boost_failure_names_the_velocity_assignment_that_erases_the_dash(self):
        """실측된 실패 형태를 그대로 짚어야 한다.

        모델은 대시를 AddForce impulse로 줬는데 같은 FixedUpdate가
        rb.linearVelocity를 통째로 대입해 지웠다(측정 비율 1.04, repair 2회 실패).
        기존 fix_prompt에는 boost 실패에 대한 안내가 아예 없었다.
        """
        spec = VerificationSpec.from_request("A/D 이동과 LeftShift 부스트를 구현해줘")
        prompt = fix_prompt(spec, ["boost_distance_too_short"], {"motion_deltas": {}})
        self.assertIn("linearVelocity", prompt)
        self.assertIn("leftShiftKey", prompt)
        self.assertIn(str(spec.boost_min_ratio), prompt)

    def test_a_dash_that_launches_the_player_off_screen_is_rejected(self):
        """실측: 0.5초에 140유닛(일반 이동의 56배)을 간 대시가 통과했다.

        플레이어가 화면 밖으로 나가 뒤이은 카메라 측정까지 망가졌는데도 부스트는
        통과였다 — 하한만 있었기 때문이다. 이동은 v1.11.4, 점프는 v1.11.9에서 같은
        이유로 상한을 받았다.
        """
        spec = VerificationSpec.from_request("A/D 이동과 LeftShift 부스트를 구현해줘")
        contract = VerificationContract(spec=spec, project_dir="")
        contract.motion_before["boost_normal"] = (0.0, 1.0, 0.0)
        contract.motion_after["boost_normal"] = (2.5, 1.0, 0.0)
        contract.motion_before["boost_shift"] = (0.0, 1.0, 0.0)
        contract.motion_after["boost_shift"] = (140.1, 1.0, 0.0)
        self.assertIn("boost_moved_too_far", contract.failures())

    def test_a_dash_within_the_observed_range_still_passes(self):
        """기록된 정상 부스트는 1.0~6.6배였다. 그 범위를 막으면 안 된다."""
        spec = VerificationSpec.from_request("A/D 이동과 LeftShift 부스트를 구현해줘")
        for boosted in (5.2, 16.1):
            with self.subTest(boosted=boosted):
                contract = VerificationContract(spec=spec, project_dir="")
                contract.motion_before["boost_normal"] = (0.0, 1.0, 0.0)
                contract.motion_after["boost_normal"] = (2.44, 1.0, 0.0)
                contract.motion_before["boost_shift"] = (0.0, 1.0, 0.0)
                contract.motion_after["boost_shift"] = (boosted, 1.0, 0.0)
                failures = contract.failures()
                self.assertNotIn("boost_moved_too_far", failures)
                self.assertNotIn("boost_distance_too_short", failures)

    def test_excessive_boost_guidance_names_the_accumulating_impulse(self):
        spec = VerificationSpec.from_request("A/D 이동과 LeftShift 부스트를 구현해줘")
        prompt = fix_prompt(spec, ["boost_moved_too_far"], {})
        self.assertIn("누적", prompt)
        self.assertIn(str(spec.boost_max_ratio), prompt)

    def test_boost_failure_also_questions_an_obstacle_in_the_path(self):
        """실측: Platform이 플레이어 옆(x=3~7)에 놓여 오른쪽 이동이 2.06에서 멈췄다.

        반대 방향은 4.96이었다. 막힌 방향에서는 부스트가 동작해도 비율이 1에
        붙으므로, 코드를 고쳐서는 통과할 수 없다.
        """
        spec = VerificationSpec.from_request("A/D 이동과 LeftShift 부스트를 구현해줘")
        prompt = fix_prompt(spec, ["boost_distance_too_short"], {"motion_deltas": {}})
        self.assertIn("장애물", prompt)
        self.assertIn("8유닛", prompt)


class JumpGeometryGuidanceTests(unittest.TestCase):
    def test_jump_failure_also_questions_the_layout_not_only_the_code(self):
        """3층을 같은 X에 쌓아 Player 머리 위가 위층 바닥이었다.

        스크립트의 래치·접지는 정상이었고 상승량이 정확히 0.0이었다. 코드만
        고치라는 안내로는 두 번의 repair가 원인을 못 짚었다.
        """
        spec = VerificationSpec.from_request("3층짜리 플랫폼을 만들고 space로 점프하게 해줘")
        prompt = fix_prompt(spec, ["player_did_not_jump"], {"motion_deltas": {}})
        self.assertIn("천장", prompt)
        self.assertIn("어긋나게", prompt)


class BuilderCallClassificationTests(unittest.TestCase):
    """빌더가 예산을 어디에 쓰는지 로그에서 세게 한다.

    측정된 실행 38건 중 37건이 MAX_ITERS를 소진했고, 한 실행은 빌더 32회 중 16회를
    자기 입력 테스트에 썼다(호스트가 직후 독립적으로 다시 잰다). 무엇을 줄일지
    정하기 전에 분포가 로그에 있어야 한다.
    """

    def _agent(self):
        agent = Agent.__new__(Agent)
        agent._builder_call_kinds = {}
        return agent

    def test_calls_are_split_by_what_they_accomplish(self):
        agent = self._agent()
        agent._classify_builder_call("unity_create_gameobject", None)
        agent._classify_builder_call("unity_write_script", None)
        agent._classify_builder_call("unity_send_key", None)
        agent._classify_builder_call("unity_wait", None)
        agent._classify_builder_call("unity_get_gameobject", None)
        agent._classify_builder_call("unity_write_script", "Policy blocked ...")
        self.assertEqual(
            agent._builder_call_kinds,
            {"build": 2, "self_check": 3, "rejected": 1},
        )

    def test_a_blocked_call_is_rejected_not_build(self):
        """차단된 호출은 예산을 쓰지만 아무것도 만들지 않는다."""
        agent = self._agent()
        agent._classify_builder_call("unity_create_scene", "Policy blocked ...")
        self.assertEqual(agent._builder_call_kinds, {"rejected": 1})


def hierarchy(*roots, path="Assets/Scenes/Game.unity"):
    return result({"scenes": [{"name": "Game", "path": path, "rootObjects": list(roots)}]})


def obj(name, *components, active=True, children=None):
    node = {"name": name, "active": active, "components": list(components)}
    if children:
        node["children"] = children
    return node


DEFAULTS = (
    obj("Main Camera", "Transform", "Camera", "AudioListener"),
    obj("Directional Light", "Transform", "Light"),
)


class SceneContentTests(unittest.TestCase):
    """생성 요청이 아무것도 측정하지 않고 통과했다.

    "댕댕이 모양의 모델링 생성해줘"가 requested_checks 0개로 22초 만에 `verified`가
    났다(영수증 20260801_202621_246_08b6891343). 런타임 어휘도 검증 어휘도 없어
    v1.11.13과 v1.11.14의 empty-spec 가드를 둘 다 비껴갔고, 호스트는 컴파일 0건과
    씬 저장만 보고 통과시켰다. 개가 만들어졌는지는 아무도 보지 않았다.
    """

    def test_a_modeling_request_now_carries_one_measurable_check(self):
        spec = VerificationSpec.from_request("댕댕이 모양의 모델링 생성해줘")
        self.assertEqual(spec.requested_checks(), ["scene_objects"])

    def test_default_objects_alone_are_not_something_that_was_created(self):
        spec = VerificationSpec.from_request("댕댕이 모양의 모델링 생성해줘")
        contract = VerificationContract(spec=spec, project_dir="")
        contract.observe("unity_get_hierarchy", {}, hierarchy(*DEFAULTS))
        self.assertEqual(contract.created_objects(), [])
        self.assertIn("scene_has_no_created_objects", contract.failures())

    def test_the_objects_that_run_actually_built_are_measured(self):
        spec = VerificationSpec.from_request("댕댕이 모양의 모델링 생성해줘")
        contract = VerificationContract(spec=spec, project_dir="")
        contract.observe("unity_get_hierarchy", {}, hierarchy(
            *DEFAULTS,
            obj("DogBody", "Transform", "MeshFilter", "MeshRenderer"),
            obj("DogHead", "Transform", "MeshFilter", "MeshRenderer"),
        ))
        self.assertEqual(contract.created_objects(), ["DogBody", "DogHead"])
        self.assertNotIn("scene_has_no_created_objects", contract.failures())
        self.assertIn("scene_objects", contract.measured_checks())

    def test_an_unread_hierarchy_is_a_gap_not_a_pass(self):
        """계층을 읽지 못했으면 '없다'가 아니라 '재지 못했다'로 끝나야 한다."""
        spec = VerificationSpec.from_request("댕댕이 모양의 모델링 생성해줘")
        contract = VerificationContract(spec=spec, project_dir="")
        self.assertIn("scene_contents_not_observed", contract.failures())
        self.assertNotIn("scene_objects", contract.measured_checks())

    def test_a_script_only_request_is_not_asked_for_scene_contents(self):
        """씬에 아무것도 남기지 않는 것이 정상인 요청까지 잡으면 오탐이다."""
        for request in (
            "PlayerMovement.cs 스크립트를 만들어줘",
            "빨간색 머티리얼을 만들어줘",
            "Assets/Scripts/Foo.cs의 오타를 수정해줘",
        ):
            with self.subTest(request=request):
                spec = VerificationSpec.from_request(request)
                self.assertFalse(spec.require_scene_objects)

    def test_scene_objects_alone_does_not_lift_the_empty_spec_refusal(self):
        """v1.11.13의 거절이 이 검사 하나로 풀리면 안 된다.

        격자는 Awake에서 런타임에 생성되므로 Edit Mode 계층은 그것을 볼 수 없다.
        호스트 오브젝트 하나가 씬에 있다는 사실은 보드가 만들어졌다는 증거가 아니다.
        """
        spec = VerificationSpec.from_request(
            "Assets/Scenes/Grid.unity 경로에 새 빈 씬을 생성하고, 10x20 격자 보드를 "
            "Awake에서 코드로 생성하는 MonoBehaviour를 만들어줘. 보드가 실제로 "
            "생성되는지 검증까지 끝내줘."
        )
        self.assertEqual(spec.requested_checks(), ["scene_objects"])
        contract = VerificationContract(spec=spec, project_dir="")
        contract.observe("unity_get_hierarchy", {}, hierarchy(
            *DEFAULTS, obj("BoardBuilder", "Transform", "BoardBuilder")
        ))
        self.assertIn("verification_spec_empty", contract.failures())


class BuilderScriptLintScopeTests(unittest.TestCase):
    """정적 검사가 요청에 적힌 경로 밖을 보지 못했다.

    "새 씬을 만들고 ~를 구현해줘"는 스크립트 경로를 말하지 않으므로
    `spec.asset_paths`에 씬 하나만 들어오고, 빌더가 스스로 만든 스크립트는 한 번도
    검사되지 않았다. 2026-08-03 코인 실행이 `CompareTag("Coin")`을 담은 채
    `verified`로 나갔다 — 프로젝트 태그가 비어 있어 트리거가 걸리는 순간
    `Tag: Coin is not defined`를 던진다.
    """

    REQUEST = (
        "Assets/Scenes/Ix.unity 경로에 새 씬을 생성하고, Player(캡슐)를 만들어 "
        "A/D 이동을 구현해줘. 코인에 닿으면 점수가 1 올라가게 해줘."
    )
    SCRIPT = (
        "using UnityEngine;\n"
        "public class PlayerMovement : MonoBehaviour {\n"
        "  int score;\n"
        "  void OnTriggerEnter(Collider other) {\n"
        "    if (other.CompareTag(\"Coin\")) { score++; Destroy(other.gameObject); }\n"
        "  }\n"
        "}\n"
    )

    def _contract(self, session_scripts):
        project = tempfile.mkdtemp()
        scripts = os.path.join(project, "Assets", "Scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "PlayerMovement.cs"), "w", encoding="utf-8") as h:
            h.write(self.SCRIPT)
        spec = VerificationSpec.from_request(self.REQUEST)
        self.assertNotIn("Assets/Scripts/PlayerMovement.cs", spec.asset_paths)
        return VerificationContract(
            spec=spec, project_dir=project, session_scripts=set(session_scripts)
        )

    def test_a_builder_written_script_is_linted(self):
        contract = self._contract({"Assets/Scripts/PlayerMovement.cs"})
        self.assertTrue(
            any(v.startswith("undefined_compare_tag") for v in contract.policy_violations),
            contract.policy_violations,
        )

    def test_without_the_session_list_the_defect_is_invisible(self):
        """이 대비가 없으면 위 테스트가 무엇을 지키는지 알 수 없다."""
        contract = self._contract(set())
        self.assertEqual(contract.policy_violations, [])

    def test_the_repair_prompt_keeps_the_feature_and_swaps_the_condition(self):
        """예전 안내는 분기를 통째로 삭제하라고 했다.

        접지 판정에는 맞았지만 그 분기가 사용자가 요청한 기능(코인 획득)인 경우
        결함 대신 기능이 사라진다.
        """
        spec = VerificationSpec.from_request(self.REQUEST)
        prompt = fix_prompt(
            spec,
            ["policy_lint:undefined_compare_tag:Assets/Scripts/PlayerMovement.cs:Coin"],
            {},
        )
        self.assertIn("GetComponent<Coin>()", prompt)
        self.assertIn("삭제하지 말고", prompt)


class BlockedMovementPathTests(unittest.TestCase):
    """장애물에 막힌 방향을 입력 코드의 결함으로 보고했다.

    v1.11.15 §5가 실측했다 — 시작 지점 옆에 플랫폼이 있으면 그 방향 거리가 짧아지고,
    호스트는 `a_did_not_move_left`를 낸다. repair는 멀쩡한 스크립트를 고치러 가고,
    **코드를 고쳐서는 절대 통과할 수 없다.** 안내만 넣은 뒤에도 재발했다.

    기록된 d/a 동시 측정 119건의 비율 분포가 판정 근거다 — 정상 0.96~1.00,
    막힘 0.00~0.42, 그 사이는 비어 있다.
    """

    def _contract(self, dx, ax, duration=1.0):
        spec = VerificationSpec.from_request(
            "Assets/Scenes/M.unity 경로에 새 씬을 생성하고 Player와 바닥을 만들어 "
            "A/D 좌우 이동을 구현해줘"
        )
        contract = VerificationContract(spec=spec, project_dir="")
        contract.motion_duration["d"] = contract.motion_duration["a"] = duration
        contract.motion_before["d"] = (0.0, 1.0, 0.0)
        contract.motion_after["d"] = (dx, 1.0, 0.0)
        contract.motion_before["a"] = (0.0, 1.0, 0.0)
        contract.motion_after["a"] = (ax, 1.0, 0.0)
        return contract

    def test_the_misdiagnosed_sample_now_names_the_layout(self):
        """실측 D +0.47 / A −4.69 — 기존에는 `d_did_not_move_right`였다."""
        failures = self._contract(0.47, -4.69).failures()
        self.assertIn("movement_path_blocked:d", failures)
        self.assertNotIn("d_did_not_move_right", failures)

    def test_an_obstructed_layout_that_used_to_pass_now_fails(self):
        """실측 D +2.06 / A −5.05 — 둘 다 최소 거리를 넘어 조용히 통과했다.

        영수증 20260730_113527이 그 상태로 `verified`였다.
        """
        self.assertIn("movement_path_blocked:d", self._contract(2.06, -5.05).failures())

    def test_both_directions_failing_is_still_an_input_defect(self):
        """양쪽이 같이 못 가면 장애물이 아니라 코드다. 여기서 갈라지면 안 된다."""
        for dx, ax in ((0.0, 0.0), (1.44, -1.46)):
            with self.subTest(dx=dx):
                failures = self._contract(dx, ax).failures()
                self.assertFalse(
                    any(f.startswith("movement_path_blocked") for f in failures),
                    failures,
                )
                self.assertIn("d_did_not_move_right", failures)
                self.assertIn("a_did_not_move_left", failures)

    def test_runaway_physics_is_not_reported_as_a_wall(self):
        """실측 D +2.5 / A −115.19 — 비율은 낮지만 원인이 다르다."""
        failures = self._contract(2.5, -115.19, duration=0.5).failures()
        self.assertFalse(
            any(f.startswith("movement_path_blocked") for f in failures), failures
        )
        self.assertIn("a_moved_too_far", failures)

    def test_healthy_and_borderline_runs_are_left_alone(self):
        """정상 구간(0.96~1.00)과 애매한 0.70은 건드리지 않는다."""
        for dx, ax in ((4.95, -4.95), (4.86, -5.07), (3.53, -5.05)):
            with self.subTest(dx=dx, ax=ax):
                self.assertFalse(
                    any(f.startswith("movement_path_blocked")
                        for f in self._contract(dx, ax).failures())
                )

    def test_the_repair_prompt_forbids_touching_the_script(self):
        spec = VerificationSpec.from_request("A/D 좌우 이동을 구현해줘")
        prompt = fix_prompt(spec, ["movement_path_blocked:d"], {})
        self.assertIn("배치 문제", prompt)
        self.assertIn("입력 코드를 고치지 마라", prompt)
        self.assertIn("unity_modify_gameobject", prompt)

    def test_it_belongs_to_the_bidirectional_check(self):
        """회귀 판정이 이것을 새 종류의 실패로 보면 롤백이 어긋난다."""
        self.assertEqual(
            failure_check_name("movement_path_blocked:d"), "bidirectional"
        )


class ContactCandidateRecordingTests(unittest.TestCase):
    """접촉 반응(A0)의 설계 입력. 판정하지 않고 거리만 기록한다.

    막힌 지점은 "호스트가 접촉을 일으킬 수 없다"가 아니라 **"안 사라졌을 때 고장인지
    안 닿은 건지 모른다"**는 비대칭이었다. 플레이어가 실제로 간 지점들을 남기면 그
    비대칭이 거리로 표현된다.
    """

    def _contract(self, before, after, samples=((0.0, 1.0, 0.0), (5.0, 1.0, 0.0))):
        spec = VerificationSpec.from_request(
            "Assets/Scenes/C.unity 경로에 새 씬을 생성하고 Player와 바닥을 만들어 "
            "A/D 이동을 구현해줘"
        )
        contract = VerificationContract(spec=spec, project_dir="")
        contract.player_samples.extend(samples)
        contract.contact_before.update(before)
        contract.contact_after.update(after)
        return contract

    def test_an_object_the_player_reached_and_that_vanished_is_recorded(self):
        report = self._contract(
            {"Coin_1": {"position": (2.0, 2.0, 0.0), "active": True, "missing": False}},
            {"Coin_1": {"position": None, "active": False, "missing": False}},
        ).contact_report()
        self.assertEqual(report[0]["object"], "Coin_1")
        self.assertTrue(report[0]["disappeared"])
        # (2,2,0)에서 (0,1,0)까지 sqrt(5) ≈ 2.236
        self.assertAlmostEqual(report[0]["nearest_player_distance"], 2.236, places=2)

    def test_destroyed_objects_count_as_disappeared_too(self):
        """SetActive(false)와 Destroy를 둘 다 잡아야 한다."""
        report = self._contract(
            {"Coin_1": {"position": (2.0, 2.0, 0.0), "active": True, "missing": False}},
            {"Coin_1": {"position": None, "active": None, "missing": True}},
        ).contact_report()
        self.assertTrue(report[0]["disappeared"])

    def test_distance_separates_reachable_from_unreachable(self):
        """비대칭을 푸는 지점 — 멀리 있었으면 안 사라진 것을 고장이라 할 수 없다."""
        report = self._contract(
            {"Coin_far": {"position": (40.0, 2.0, 0.0), "active": True, "missing": False}},
            {"Coin_far": {"position": (40.0, 2.0, 0.0), "active": True, "missing": False}},
        ).contact_report()
        self.assertGreater(report[0]["nearest_player_distance"], 30)
        self.assertFalse(report[0]["disappeared"])

    def test_the_jump_peak_is_part_of_where_the_player_went(self):
        """실측 Coin_2(y 4)는 점프로 닿았다. 이동 표본만 보면 놓친다."""
        near_ground = self._contract(
            {"Coin_2": {"position": (0.0, 4.0, 0.0), "active": True, "missing": False}},
            {},
        )
        self.assertGreater(near_ground.contact_report()[0]["nearest_player_distance"], 2.9)
        with_jump = self._contract(
            {"Coin_2": {"position": (0.0, 4.0, 0.0), "active": True, "missing": False}},
            {},
            samples=((0.0, 1.0, 0.0), (5.0, 1.0, 0.0), (0.0, 4.2, 0.0)),
        )
        self.assertLess(with_jump.contact_report()[0]["nearest_player_distance"], 0.5)

    def test_no_samples_means_no_distance_not_a_zero(self):
        contract = self._contract(
            {"Coin_1": {"position": (2.0, 2.0, 0.0), "active": True, "missing": False}},
            {},
            samples=(),
        )
        self.assertIsNone(contract.contact_report()[0]["nearest_player_distance"])

    def test_the_middle_of_a_sweep_counts_as_visited(self):
        """표본은 구간 양 끝에만 찍힌다. 점만 보면 지나간 곳을 놓친다.

        실측: 코인 (2, 2, 0) 바로 아래를 지나갔는데 거리가 2.052로 기록됐다.
        D 측정의 (0,1,0) → (4.95,1,0)을 선분으로 보면 수직 거리 1.0이다.
        """
        contract = self._contract(
            {"Coin_1": {"position": (2.0, 2.0, 0.0), "active": True, "missing": False}},
            {},
            samples=(),
        )
        contract.motion_before["d"] = (0.0, 1.0, 0.0)
        contract.motion_after["d"] = (4.95, 1.0, 0.0)
        self.assertAlmostEqual(
            contract.contact_report()[0]["nearest_player_distance"], 1.0, places=3
        )

    def test_separate_measurements_are_not_joined_into_a_fake_path(self):
        """구간 사이에는 restart_play로 스폰에 되돌아간다.

        D 측정의 끝과 A 측정의 시작을 이으면 실제로 지나가지 않은 경로가 생긴다.
        """
        contract = self._contract({}, {}, samples=())
        contract.motion_before["d"] = (0.0, 1.0, 0.0)
        contract.motion_after["d"] = (5.0, 1.0, 0.0)
        contract.motion_before["a"] = (0.0, 1.0, 0.0)
        contract.motion_after["a"] = (-5.0, 1.0, 0.0)
        paths = contract.player_paths()
        self.assertIn(((0.0, 1.0, 0.0), (5.0, 1.0, 0.0)), paths)
        self.assertIn(((0.0, 1.0, 0.0), (-5.0, 1.0, 0.0)), paths)
        # (5,1,0) → (0,1,0) 같은 지어낸 연결이 없어야 한다
        self.assertEqual(len(paths), 2)

    def test_the_jump_arc_is_a_vertical_segment(self):
        contract = self._contract({}, {}, samples=())
        contract.jump_before = (3.0, 1.0, 0.0)
        contract.jump_peak_y = 6.0
        self.assertEqual(
            contract.player_paths(), [((3.0, 1.0, 0.0), (3.0, 6.0, 0.0))]
        )

    def test_structural_objects_are_separated_from_interaction_targets(self):
        """첫 15건 표본에서 바닥·발판이 코인과 섞여 거리 분포를 읽을 수 없었다.

        붙은 스크립트가 둘을 가른다 — 코인은 마커를 갖고 바닥은 갖지 않는다.
        """
        contract = self._contract(
            {"Coin_1": {"position": (2.0, 2.0, 0.0), "active": True, "missing": False},
             "Ground": {"position": (0.0, -0.5, 0.0), "active": True, "missing": False}},
            {},
        )
        contract.observe("unity_get_hierarchy", {}, hierarchy(
            *DEFAULTS,
            obj("Player", "Transform", "MeshRenderer", "Rigidbody"),
            obj("Coin_1", "Transform", "MeshFilter", "MeshRenderer", "BoxCollider", "Coin"),
            obj("Ground", "Transform", "MeshFilter", "MeshRenderer", "BoxCollider"),
        ))
        by_name = {c["object"]: c for c in contract.contact_report()}
        self.assertEqual(by_name["Coin_1"]["scripts"], ["Coin"])
        self.assertEqual(by_name["Ground"]["scripts"], [])

    def _sized(self, position, scale, player_scale=(1.0, 1.0, 1.0)):
        contract = self._contract(
            {"Item": {"position": position, "active": True,
                      "missing": False, "scale": scale}},
            {},
            samples=(),
        )
        contract.motion_before["d"] = (0.0, 1.0, 0.0)
        contract.motion_after["d"] = (4.95, 1.0, 0.0)
        if player_scale is not None:
            contract.latest_scales["player"] = player_scale
        return contract.contact_report()[0]

    def test_surface_gap_subtracts_both_half_extents(self):
        """실측 0.773의 정체 — 1×1×1 플레이어와 0.5 코인은 맞닿아도 중심 거리 0.75다.

        경로 바로 위에 있던 `Item`이 "안 닿았다"처럼 읽힌 이유가 이것이다. 중심
        거리로는 닿은 것과 떨어진 것을 가를 수 없다.
        """
        record = self._sized((1.5, 1.75, 0.0), (0.5, 0.5, 0.5))
        self.assertAlmostEqual(record["nearest_player_distance"], 0.75, places=3)
        self.assertAlmostEqual(record["surface_gap"], 0.0, places=3)

    def test_an_overlap_is_negative_not_clamped(self):
        """겹친 정도를 남긴다. 0에서 자르면 '살짝 스쳤다'와 '관통했다'가 같아진다."""
        record = self._sized((1.5, 1.75, 0.0), (0.5, 0.5, 0.5),
                             player_scale=(1.0, 2.0, 1.0))
        self.assertAlmostEqual(record["surface_gap"], -0.5, places=3)

    def test_a_distant_object_keeps_a_positive_gap(self):
        record = self._sized((1.5, 5.0, 0.0), (0.5, 0.5, 0.5))
        self.assertAlmostEqual(record["surface_gap"], 3.25, places=3)

    def test_the_gap_is_none_when_a_size_is_unknown(self):
        """모르는 것을 기본값으로 채우면 없는 접촉을 지어낸다."""
        self.assertIsNone(self._sized((1.5, 1.75, 0.0), None)["surface_gap"])
        self.assertIsNone(
            self._sized((1.5, 1.75, 0.0), (0.5, 0.5, 0.5), player_scale=None)
            ["surface_gap"]
        )

    def test_the_scale_is_recorded_beside_the_distance(self):
        record = self._sized((1.5, 1.75, 0.0), (0.5, 0.5, 0.5))
        self.assertEqual(record["scale"], [0.5, 0.5, 0.5])

    def test_the_half_extent_follows_the_direction_of_approach(self):
        """납작한 바닥은 위에서 볼 때와 옆에서 볼 때 반폭이 다르다."""
        floor = (5.0, 0.5, 5.0)   # 10×1×10 바닥의 반폭
        self.assertAlmostEqual(
            VerificationContract._box_radius(floor, (0.0, 1.0, 0.0)), 0.5, places=3
        )
        self.assertAlmostEqual(
            VerificationContract._box_radius(floor, (1.0, 0.0, 0.0)), 5.0, places=3
        )

    def test_the_players_worst_tilt_is_recorded(self):
        """구르는 캡슐은 세로 반폭이 절반이 된다 — 간격을 액면대로 읽으면 안 된다.

        실측 A/B: 같은 간격 −0.199에서 회전 자유는 획득 실패, 회전 잠금은 성공.
        기록된 실행 150개 중 101개(67%)에서 플레이어가 15도 넘게 기운다.
        """
        contract = self._contract({}, {}, samples=())

        def see(euler):
            contract.observe("unity_get_gameobject", {"target": "Player"}, result({
                "name": "Player", "activeSelf": True,
                "transform": {"position": [0.0, 1.5, 0.0], "localScale": [1.0, 1.0, 1.0],
                              "eulerAngles": euler},
            }))

        see([0.0, 0.0, 0.0])
        self.assertAlmostEqual(contract.player_max_tilt, 0.0, places=3)
        see([0.0, 0.0, 173.27])
        # 180도 뒤집힌 것은 수직이다 — 173도는 그로부터 6.73도 기운 것
        self.assertAlmostEqual(contract.player_max_tilt, 6.73, places=2)
        see([0.0, 0.0, 90.0])
        self.assertAlmostEqual(contract.player_max_tilt, 90.0, places=3)
        # 최대치는 내려가지 않는다
        see([0.0, 0.0, 0.0])
        self.assertAlmostEqual(contract.player_max_tilt, 90.0, places=3)
        self.assertEqual(contract.evidence()["player_max_tilt"], 90.0)

    def test_measured_collider_extents_beat_the_scale_guess(self):
        """Unity 캡슐은 scale 1에서 높이가 2다. localScale로 추정하면 세로가 절반이다.

        플레이어는 거의 항상 캡슐이므로 그 오차가 모든 접촉 거리에 실려 있었다.
        """
        contract = self._contract({}, {}, samples=())
        contract.observe("unity_get_gameobject", {"target": "Player"}, result({
            "name": "Player", "activeSelf": True,
            "transform": {"position": [0.0, 1.0, 0.0], "localScale": [1.0, 1.0, 1.0]},
            "components": [{"type": "UnityEngine.CapsuleCollider",
                            "data": {"isTrigger": False, "enabled": True,
                                     "extents": [0.5, 1.0, 0.5]}}],
        }))
        self.assertEqual(contract.latest_extents["player"], (0.5, 1.0, 0.5))
        # 추정(0.5)이 아니라 실측(1.0)이 쓰인다
        self.assertEqual(contract._half_extents("Player", (1.0, 1.0, 1.0)), (0.5, 1.0, 0.5))
        # 콜라이더를 못 본 오브젝트는 예전대로 localScale의 절반
        self.assertEqual(contract._half_extents("Coin", (1.0, 1.0, 1.0)), (0.5, 0.5, 0.5))

    def test_the_scale_comes_from_the_observed_transform(self):
        contract = self._contract({}, {}, samples=())
        contract.observe(
            "unity_get_gameobject",
            {"target": "Player"},
            result({
                "name": "Player", "activeSelf": True,
                "transform": {"position": [0.0, 1.0, 0.0],
                              "localScale": [1.0, 2.0, 1.0]},
            }),
        )
        self.assertEqual(contract.latest_scales["player"], (1.0, 2.0, 1.0))

    def test_a_sample_that_saw_a_disappearance_is_not_overwritten(self):
        """구간마다 Play Mode가 정지되고, 정지는 런타임 Destroy를 되돌린다.

        매번 덮어쓰면 **마지막 구간에서 사라진 것만** 남는다. 기록 전체에서
        `disappeared: true`가 하나(`Coin_2`, 점프로 닿았고 점프가 마지막 구간)뿐이었던
        이유가 그것이다. 실측 6회 전부 아이템이 0.25~1.0 겹쳤는데 `false`였다.
        """
        self.assertTrue(contact_already_gone({"missing": True, "active": None}))
        self.assertTrue(contact_already_gone({"missing": False, "active": False}))
        self.assertFalse(contact_already_gone({"missing": False, "active": True}))
        self.assertFalse(contact_already_gone(None))
        # 아직 활성 여부를 못 읽은 표본은 사라진 것이 아니다
        self.assertFalse(contact_already_gone({"missing": False, "active": None}))

    def test_recording_never_changes_the_verdict(self):
        """A0는 아직 검사가 아니다. 기록이 실패를 만들면 규칙을 어기는 것이다."""
        recorded = self._contract(
            {"Coin_1": {"position": (2.0, 2.0, 0.0), "active": True, "missing": False}},
            {"Coin_1": {"position": (2.0, 2.0, 0.0), "active": True, "missing": False}},
        )
        bare = self._contract({}, {})
        self.assertEqual(recorded.failures(), bare.failures())
        self.assertIsNotNone(recorded.evidence()["contact_candidates"])


class ContactVanishJudgementTests(unittest.TestCase):
    """A0 승격 — 뚫고 지나갔는데 반응이 없으면 실패다.

    근거는 회전을 잠근 통제 스윕이다(v1.11.33). 같은 씬에서 아이템 높이만 바꿨다.

        간격 +0.100 → 사라짐 False / −0.050 → True / −0.198 → True

    경계는 0이고, 판정은 −0.25보다 안쪽만 한다.
    """

    REQUEST = ("Assets/Scenes/C.unity 경로에 새 씬을 생성하고 Player와 바닥을 만들어 "
               "A/D 이동을 구현하고, 아이템에 닿으면 아이템이 사라지게 해줘")

    def _contract(self, request=None, gap_position=(3.0, 1.5, 0.0), tilt=0.0):
        spec = VerificationSpec.from_request(request or self.REQUEST)
        contract = VerificationContract(spec=spec, project_dir="")
        contract.observe("unity_get_hierarchy", {}, hierarchy(
            *DEFAULTS,
            obj("Player", "Transform", "MeshRenderer", "Rigidbody", "CapsuleCollider"),
            obj("Item", "Transform", "MeshRenderer", "SphereCollider", "Pickup"),
            obj("Ground", "Transform", "MeshRenderer", "BoxCollider"),
        ))
        contract.player_max_tilt = tilt
        contract.latest_scales["player"] = (1.0, 1.0, 1.0)
        contract.latest_extents["player"] = (0.5, 1.0, 0.5)
        contract.motion_before["d"] = (0.0, 1.5, 0.0)
        contract.motion_after["d"] = (4.95, 1.5, 0.0)
        for name, pos in (("Item", gap_position), ("Ground", (0.0, 0.0, 0.0))):
            contract.contact_before[name] = {
                "position": pos, "active": True, "missing": False,
                "scale": (1.0, 1.0, 1.0),
            }
            contract.contact_after[name] = dict(contract.contact_before[name])
        return contract

    def test_the_request_shape_turns_the_check_on(self):
        self.assertIn("contact_vanish", VerificationSpec.from_request(
            self.REQUEST).requested_checks())

    def test_a_deep_overlap_with_no_reaction_fails(self):
        contract = self._contract()
        self.assertEqual(contract.unreacted_contacts(), ["Item"])
        self.assertIn("contact_target_did_not_react:Item", contract.failures())

    def test_an_object_that_vanished_is_not_a_failure(self):
        contract = self._contract()
        contract.contact_after["Item"] = {"position": None, "active": None,
                                          "missing": True, "scale": (1.0, 1.0, 1.0)}
        self.assertEqual(contract.unreacted_contacts(), [])

    def test_the_boundary_is_not_judged(self):
        """경계는 0이지만 −0.25 바깥은 판정하지 않는다. 실측 −0.199는 정상이었다."""
        # 플레이어 경로 y=1.5, 반폭 1.0 + 아이템 0.5 → y 3.0이 간격 0
        self.assertEqual(self._contract(gap_position=(3.0, 2.9, 0.0))
                         .unreacted_contacts(), [])
        self.assertEqual(self._contract(gap_position=(3.0, 3.1, 0.0))
                         .unreacted_contacts(), [])

    def test_a_tilted_player_is_not_judged(self):
        """구르는 캡슐은 세로 반폭이 절반이 되고 호스트는 겹침을 과장한다."""
        self.assertEqual(self._contract(tilt=45.0).unreacted_contacts(), [])
        contract = self._contract(tilt=45.0)
        self.assertNotIn("contact_vanish", contract.measured_checks())

    def test_the_tilt_must_be_known(self):
        contract = self._contract()
        contract.player_max_tilt = None
        self.assertEqual(contract.unreacted_contacts(), [])

    def test_structures_are_never_judged(self):
        """바닥은 늘 겹친다 — 스크립트가 없다는 것이 유일한 방벽이다."""
        contract = self._contract()
        self.assertNotIn("Ground", contract.unreacted_contacts())

    def test_a_stomp_request_is_out_of_scope(self):
        """호스트 입력은 좌우 이동과 제자리 점프뿐이라 밟기를 못 만든다."""
        contract = self._contract(request=(
            "Assets/Scenes/C.unity 경로에 새 씬을 생성하고 Player와 바닥을 만들어 "
            "A/D 이동을 구현하고, 적을 위에서 밟으면 적이 사라지게 해줘"))
        self.assertFalse(contract.spec.require_contact_vanish)
        self.assertEqual(contract.unreacted_contacts(), [])

    def test_a_clear_state_request_is_out_of_scope(self):
        """깃발은 클리어 상태를 만들지 소멸하지 않는다."""
        contract = self._contract(request=(
            "Assets/Scenes/C.unity 경로에 새 씬을 생성하고 Player와 바닥을 만들어 "
            "A/D 이동을 구현하고, 깃발에 닿으면 클리어 상태가 되게 해줘"))
        self.assertFalse(contract.spec.require_contact_vanish)

    def test_the_failure_reaches_its_own_check_and_a_remedy(self):
        self.assertEqual(
            failure_check_name("contact_target_did_not_react:Item"), "contact_vanish")
        prompt = fix_prompt(self._contract().spec,
                            ["contact_target_did_not_react:Item"], {})
        self.assertIn("GetComponent", prompt)
        self.assertIn("isTrigger", prompt)


class ContactWiringTests(unittest.TestCase):
    """접촉이 성립하지 않은 채 만들어진다 — 재생 6건 + 라이브 2건.

    A0의 표본이 안 쌓이는 이유가 측정 쪽(v1.11.28)과 빌더 쪽 둘이었다. 빌더 쪽은
    다시 둘로 갈린다 — 트리거를 아예 안 만들거나(A4), 트리거에 Rigidbody를 붙여
    아이템이 월드 밖으로 떨어지거나(A5).
    """

    def _contract(self, script_body="void OnTriggerEnter(Collider o) {}"):
        self.project = tempfile.mkdtemp()
        scripts = os.path.join(self.project, "Assets", "Scripts")
        os.makedirs(scripts, exist_ok=True)
        with open(os.path.join(scripts, "Pickup.cs"), "w", encoding="utf-8") as handle:
            handle.write("public class Pickup : MonoBehaviour {%s}" % script_body)
        spec = VerificationSpec.from_request(
            "Assets/Scenes/C.unity 경로에 새 씬을 생성하고 Player와 바닥을 만들어 "
            "A/D 이동을 구현하고, 아이템에 닿으면 아이템이 사라지게 해줘"
        )
        contract = VerificationContract(spec=spec, project_dir=self.project)
        contract.session_scripts.add("Assets/Scripts/Pickup.cs")
        contract.observe("unity_get_hierarchy", {}, hierarchy(
            *DEFAULTS,
            obj("Player", "Transform", "MeshRenderer", "Rigidbody", "CapsuleCollider"),
            obj("Item", "Transform", "MeshRenderer", "SphereCollider", "Pickup"),
        ))
        contract.contact_before["Item"] = {
            "position": (3.0, 1.0, 0.0), "active": True, "missing": False,
            "scale": (1.0, 1.0, 1.0),
        }
        return contract

    def _see_collider(self, contract, target, kind, is_trigger):
        contract.observe("unity_get_gameobject", {"target": target}, result({
            "name": target, "activeSelf": True,
            "transform": {"position": [3.0, 1.0, 0.0], "localScale": [1.0, 1.0, 1.0]},
            "components": [{"type": "UnityEngine." + kind,
                            "data": {"isTrigger": is_trigger, "enabled": True}}],
        }))

    def test_a_handler_with_no_trigger_anywhere_is_a_dead_branch(self):
        contract = self._contract()
        self._see_collider(contract, "Item", "SphereCollider", False)
        self._see_collider(contract, "Player", "CapsuleCollider", False)
        self.assertEqual(contract.dead_trigger_handlers(), ["Item"])

    def test_a_trigger_on_either_side_is_enough(self):
        """OnTriggerEnter는 두 콜라이더 중 하나만 트리거여도 호출된다."""
        on_item = self._contract()
        self._see_collider(on_item, "Item", "SphereCollider", True)
        self._see_collider(on_item, "Player", "CapsuleCollider", False)
        self.assertEqual(on_item.dead_trigger_handlers(), [])
        on_player = self._contract()
        self._see_collider(on_player, "Item", "SphereCollider", False)
        self._see_collider(on_player, "Player", "CapsuleCollider", True)
        self.assertEqual(on_player.dead_trigger_handlers(), [])

    def test_a_handler_on_the_player_is_seen_too(self):
        """기록된 6건 중 4건이 이 형태다 — 핸들러가 PlayerMovement.cs에 있다.

        접촉 후보만 보면 통째로 놓친다. 라이브에서 실제로 놓쳤다(r10).
        """
        contract = self._contract()
        with open(os.path.join(self.project, "Assets", "Scripts",
                               "PlayerMovement.cs"), "w", encoding="utf-8") as handle:
            handle.write("public class PlayerMovement { void OnTriggerEnter(Collider o) {} }")
        contract.session_scripts.add("Assets/Scripts/PlayerMovement.cs")
        contract.observe("unity_get_hierarchy", {}, hierarchy(
            *DEFAULTS,
            obj("Player", "Transform", "CapsuleCollider", "PlayerMovement"),
            obj("Item", "Transform", "SphereCollider"),
        ))
        self._see_collider(contract, "Item", "SphereCollider", False)
        self._see_collider(contract, "Player", "CapsuleCollider", False)
        self.assertEqual(contract.dead_trigger_handlers(), ["Player"])

    def test_a_collision_handler_is_not_required_to_use_a_trigger(self):
        """OnCollisionEnter로 구현한 정상적인 획득을 결함으로 만들면 안 된다."""
        contract = self._contract(script_body="void OnCollisionEnter(Collision c) {}")
        self._see_collider(contract, "Item", "SphereCollider", False)
        self._see_collider(contract, "Player", "CapsuleCollider", False)
        self.assertEqual(contract.dead_trigger_handlers(), [])

    def test_a_script_from_an_earlier_session_is_read_too(self):
        """`--repair-existing`은 빌더를 건너뛰어 session_scripts가 비어 있다.

        그것만 보면 기존 씬을 재검증할 때 검사가 조용히 꺼진다.
        """
        contract = self._contract()
        contract.session_scripts.clear()
        self._see_collider(contract, "Item", "SphereCollider", False)
        self._see_collider(contract, "Player", "CapsuleCollider", False)
        self.assertEqual(contract.dead_trigger_handlers(), ["Item"])

    def test_an_unobserved_collider_is_not_a_failure(self):
        """모르는 것을 근거로 실패시키지 않는다 — 구버전 브리지는 isTrigger를 안 준다."""
        contract = self._contract()
        self.assertEqual(contract.dead_trigger_handlers(), [])

    def test_an_object_that_fell_out_of_the_world_is_reported(self):
        """실측: 트리거 + Rigidbody → y 1.0 → −44.63. 도착했을 때 그 자리에 없다."""
        contract = self._contract()
        contract.lowest_y["item"] = -44.63
        self.assertEqual(contract.fallen_contact_objects(), ["Item"])

    def test_a_small_drop_is_not_a_fall(self):
        """수직으로 움직이는 발판을 결함으로 만들지 않는다."""
        contract = self._contract()
        contract.lowest_y["item"] = -5.0
        self.assertEqual(contract.fallen_contact_objects(), [])

    def test_structures_without_scripts_are_not_judged(self):
        contract = self._contract()
        contract.contact_before["Ground"] = {
            "position": (0.0, -0.5, 0.0), "active": True, "missing": False,
        }
        contract.lowest_y["ground"] = -80.0
        self.assertEqual(contract.fallen_contact_objects(), [])

    def test_both_findings_reach_the_failure_list_and_the_remedy(self):
        contract = self._contract()
        self._see_collider(contract, "Item", "SphereCollider", False)
        self._see_collider(contract, "Player", "CapsuleCollider", False)
        contract.lowest_y["item"] = -44.63
        failures = contract.failures()
        self.assertIn("trigger_handler_without_trigger_collider:Item", failures)
        self.assertIn("contact_object_fell_away:Item", failures)
        self.assertEqual(
            failure_check_name("trigger_handler_without_trigger_collider:Item"),
            "gameplay",
        )
        prompt = fix_prompt(contract.spec, failures, {})
        self.assertIn("isTrigger", prompt)
        self.assertIn("Rigidbody를 붙이지 않는다", prompt)


class AutonomousMotionTests(unittest.TestCase):
    """스스로 움직여야 하는 것이 움직이는지 보는 검사가 없었다.

    2026-08-03 실행에서 "적을 좌우로 자동 순찰하게"가 65초에 `verified`로 나갔다.
    검사 7개를 전부 실측했지만 전부 플레이어 이동 축이었고, 적이 움직이는지는
    아무도 보지 않았다. 유일한 신호는 영수증의 ⚠ 미매핑 줄뿐이었다.
    """

    def _spec(self):
        return VerificationSpec.from_request(
            "Assets/Scenes/P.unity 경로에 새 씬을 생성하고, Player(캡슐)와 바닥을 "
            "만들어 A/D 이동을 구현해줘. 적(빨간 캡슐)을 하나 만들어 좌우로 자동 "
            "순찰하게 해줘."
        )

    def _contract(self, before, after):
        contract = VerificationContract(spec=self._spec(), project_dir="")
        contract.observe("unity_get_hierarchy", {}, hierarchy(
            *DEFAULTS,
            obj("Player", "Transform", "MeshRenderer", "Rigidbody", "CapsuleCollider"),
            obj("GroundPlatform", "Transform", "MeshRenderer"),
            obj("Enemy", "Transform", "MeshRenderer", "EnemyPatrol"),
        ))
        contract.autonomous_before.update(before)
        contract.autonomous_after.update(after)
        return contract

    def test_the_check_is_attached_to_a_patrol_request(self):
        spec = self._spec()
        self.assertTrue(spec.require_autonomous_motion)
        self.assertIn("autonomous_motion", spec.requested_checks())

    def test_it_needs_play_mode_so_it_is_not_a_static_check(self):
        """Edit Mode로 끝나는 검사로 분류되면 빈 spec 가드가 무너진다."""
        self.assertIn("autonomous_motion", self._spec().behaviour_checks())

    def test_the_player_is_not_a_candidate(self):
        """호스트가 입력으로 직접 미는 대상은 '스스로' 움직인 것이 아니다."""
        contract = self._contract({}, {})
        self.assertNotIn("Player", contract.autonomous_candidates())
        self.assertIn("Enemy", contract.autonomous_candidates())

    def test_a_standing_still_enemy_fails(self):
        contract = self._contract(
            {"Enemy": (3.0, 1.0, 0.0), "GroundPlatform": (0.0, 0.0, 0.0)},
            {"Enemy": (3.0, 1.0, 0.0), "GroundPlatform": (0.0, 0.0, 0.0)},
        )
        self.assertIn("no_object_moved_on_its_own", contract.failures())

    def test_a_patrolling_enemy_passes_and_is_named_in_the_receipt(self):
        contract = self._contract(
            {"Enemy": (3.0, 1.0, 0.0), "GroundPlatform": (0.0, 0.0, 0.0)},
            {"Enemy": (4.6, 1.0, 0.0), "GroundPlatform": (0.0, 0.0, 0.0)},
        )
        self.assertNotIn("no_object_moved_on_its_own", contract.failures())
        self.assertIn("autonomous_motion", contract.measured_checks())
        evidence = contract.evidence()["autonomous_motion"]
        self.assertEqual(evidence["object"], "Enemy")
        self.assertAlmostEqual(evidence["distance"], 1.6, places=3)

    def test_an_unsampled_scene_is_a_gap_not_a_pass(self):
        contract = self._contract({}, {})
        self.assertIn("autonomous_motion_not_measured", contract.failures())
        self.assertNotIn("autonomous_motion", contract.measured_checks())

    def test_a_plain_movement_request_does_not_get_the_check(self):
        """플레이어만 움직이는 요청에 붙으면 통과하던 형태가 전부 떨어진다."""
        spec = VerificationSpec.from_request(
            "A/D 좌우 이동과 Space 점프가 되는 플랫포머를 만들어줘"
        )
        self.assertFalse(spec.require_autonomous_motion)

    def test_the_repair_prompt_points_at_the_unattached_component(self):
        prompt = fix_prompt(self._spec(), ["no_object_moved_on_its_own"], {})
        self.assertIn("add_component", prompt)


class PlayerVisibilityTests(unittest.TestCase):
    """렌더러 없는 Player에 `verified`가 찍혔다.

    영수증 20260731_033338_195_471258ba7b은 검사 13개를 전부 실측 통과했는데 Game
    View에 플레이어가 없었다. 그 실행의 계층(세션 20260731_032824_506)에서 Player는
    Transform·Rigidbody·BoxCollider·PlayerController뿐 — MeshFilter도 MeshRenderer도
    없는 빈 GameObject였다. 물리는 완벽했으므로 이동·점프·부스트·카메라가 전부
    통과했고, `components["Player"]`는 Rigidbody와 Collider만 요구했다.
    """

    def _spec(self):
        return VerificationSpec.from_request(
            "A/D로 이동하고 space로 점프하는 플랫포머 게임을 만들어줘"
        )

    def test_the_check_is_attached_wherever_a_player_is_required(self):
        spec = self._spec()
        self.assertTrue(spec.require_visible_player)
        self.assertIn("player_visible", spec.requested_checks())

    def test_the_recorded_invisible_player_now_fails(self):
        contract = VerificationContract(spec=self._spec(), project_dir="")
        contract.observe("unity_get_hierarchy", {}, hierarchy(
            *DEFAULTS,
            obj("Player", "Transform", "Rigidbody", "BoxCollider", "PlayerController"),
        ))
        self.assertIn("player_has_no_renderer", contract.failures())
        self.assertIs(contract.evidence()["player_has_renderer"], False)

    def test_a_capsule_player_passes(self):
        contract = VerificationContract(spec=self._spec(), project_dir="")
        contract.observe("unity_get_hierarchy", {}, hierarchy(
            *DEFAULTS,
            obj("Player", "Transform", "MeshFilter", "MeshRenderer",
                "CapsuleCollider", "Rigidbody"),
        ))
        self.assertNotIn("player_has_no_renderer", contract.failures())
        self.assertIn("player_visible", contract.measured_checks())

    def test_a_mesh_on_a_child_still_counts_as_visible(self):
        """몸통과 머리로 나뉜 Player도 화면에는 보인다. 자기 자신만 보면 오탐이다."""
        contract = VerificationContract(spec=self._spec(), project_dir="")
        contract.observe("unity_get_hierarchy", {}, hierarchy(
            *DEFAULTS,
            obj("Player", "Transform", "Rigidbody", "CapsuleCollider",
                children=[obj("Body", "Transform", "MeshFilter", "SkinnedMeshRenderer")]),
        ))
        self.assertNotIn("player_has_no_renderer", contract.failures())

    def test_a_missing_player_is_reported_as_unmeasured(self):
        contract = VerificationContract(spec=self._spec(), project_dir="")
        contract.observe("unity_get_hierarchy", {}, hierarchy(*DEFAULTS))
        failures = contract.failures()
        self.assertIn("player_visibility_not_measured", failures)
        self.assertNotIn("player_has_no_renderer", failures)

    def test_the_repair_prompt_names_the_missing_mesh_not_the_physics(self):
        prompt = fix_prompt(self._spec(), ["player_has_no_renderer"], {})
        self.assertIn("MeshRenderer", prompt)
        self.assertIn("프리미티브", prompt)

    def test_both_new_checks_are_static_so_the_play_mode_guard_keeps_its_meaning(self):
        """Edit Mode로 끝나는 검사가 Play Mode 증명으로 계산되면 안 된다."""
        spec = self._spec()
        self.assertNotIn("scene_objects", spec.behaviour_checks())
        self.assertNotIn("player_visible", spec.behaviour_checks())


class SceneContentOrchestrationTests(unittest.TestCase):
    """검사가 spec에만 있고 호스트가 계층을 읽지 않으면 영원히 미측정으로 끝난다."""

    def _run(self, request, roots):
        with tempfile.TemporaryDirectory() as receipts:
            tools = HostTools(roots=roots)
            shown = []
            agent = NoModelAgent(
                tools, shown.append, lambda *_: None, shown.append,
                enable_logging=False, enable_verification=True,
            )
            with mock.patch.object(config, "VERIFICATION_RECEIPT_DIR", receipts):
                success = asyncio.run(agent.run_turn(request, tool_mode="verify"))
            with open(agent.last_verification_receipt_path, encoding="utf-8") as handle:
                receipt = json.load(handle)
            return success, [name for name, _ in tools.calls], receipt

    def test_a_creation_request_makes_the_host_read_the_hierarchy(self):
        success, calls, receipt = self._run(
            "댕댕이 모양의 모델링 생성해줘",
            [obj("Main Camera", "Transform", "Camera"),
             obj("Dog", "Transform", "MeshFilter", "MeshRenderer")],
        )
        self.assertIn("unity_get_hierarchy", calls)
        self.assertEqual(receipt["measured_checks"], ["scene_objects"])
        self.assertEqual(receipt["evidence"]["scene_objects"], ["Dog"])
        self.assertTrue(success)

    def test_an_empty_scene_is_no_longer_verified(self):
        """이 요청이 22초 만에 measured_checks 0개로 통과했던 자리다."""
        success, _, receipt = self._run(
            "댕댕이 모양의 모델링 생성해줘",
            [obj("Main Camera", "Transform", "Camera"),
             obj("Directional Light", "Transform", "Light")],
        )
        self.assertFalse(success)
        self.assertIn("scene_has_no_created_objects", receipt["failures"])


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


class IdleVelocityRuleTests(unittest.TestCase):
    """규칙이 철자가 아니라 동작을 봐야 한다.

    실측 `20260809_132831`에서 모델이 **"to satisfy policy check"라는 주석까지 달아
    가며** 지역 벡터를 거쳐 수평 속도를 0으로 만들었는데도 규칙이 거부했고,
    repair 4사이클이 같은 위반을 반복하며 Play Mode 측정 11개가 전부 blocked됐다.
    """

    TRIGGER = "if (moveX != 0) { }"

    def _violations(self, body):
        with tempfile.TemporaryDirectory() as project:
            os.makedirs(os.path.join(project, "Assets", "Scripts"))
            path = "Assets/Scripts/PlayerMovement25D.cs"
            with open(os.path.join(project, path), "w", encoding="utf-8") as handle:
                handle.write(
                    "using UnityEngine; public class PlayerMovement25D : MonoBehaviour {"
                    "Rigidbody rb; void Move() { " + self.TRIGGER + body + " } }"
                )
            return lint_scripts("무입력 0.5초", [path], project), path

    def test_the_shapes_that_actually_zero_the_axis_pass(self):
        shapes = {
            "지역 벡터 경유": (
                "Vector3 velocity = rb.linearVelocity; velocity.x = 0f; "
                "rb.linearVelocity = velocity;"
            ),
            "직접 생성": "rb.linearVelocity = new Vector3(0f, rb.linearVelocity.y, 0f);",
            "Vector3.zero": "rb.linearVelocity = Vector3.zero;",
        }
        for label, body in shapes.items():
            with self.subTest(label):
                violations, path = self._violations(body)
                self.assertNotIn(f"idle_velocity_not_zeroed:{path}", violations)

    def test_never_zeroing_the_axis_still_fails(self):
        violations, path = self._violations(
            "Vector3 velocity = rb.linearVelocity; velocity.x = moveX * 5f; "
            "rb.linearVelocity = velocity;"
        )
        self.assertIn(f"idle_velocity_not_zeroed:{path}", violations)
