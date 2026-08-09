import json
import unittest

import config
import planner


def _milestone_dict(**overrides):
    base = {
        "title": "레벨 로더 설치",
        "goal": "unity_install_level_loader로 로더를 설치하고 컴파일을 검증한다",
        "deliverables": ["Assets/Scripts/LevelLoader.cs"],
        "verify": ["compile"],
    }
    base.update(overrides)
    return base


class LooksLargeTests(unittest.TestCase):
    def test_level_count_plus_build_verb(self):
        self.assertTrue(planner.looks_large("레벨 3개짜리 플랫포머 만들어줘"))
        self.assertTrue(planner.looks_large("make a platformer with 3 levels"))

    def test_genre_plus_build_verb(self):
        self.assertTrue(planner.looks_large("간단한 퍼즐 게임을 구현해줘"))

    def test_small_requests_are_not_large(self):
        self.assertFalse(planner.looks_large("큐브 하나 만들어"))
        self.assertFalse(planner.looks_large("씬 상태 확인해줘"))
        self.assertFalse(planner.looks_large("레벨 1 씬을 열어줘"))  # 생성 동사 없음


class ValidatePlanTests(unittest.TestCase):
    def test_unrequested_level_milestones_are_pruned_from_mvp_plan(self):
        plan = planner.Plan(request="", milestones=[
            planner.Milestone("m1", "스크립트", "Player 이동 스크립트 작성"),
            planner.Milestone("m2", "레벨 JSON", "level0.json부터 level2.json 작성",
                              deliverables=["Assets/StreamingAssets/Levels/level0.json"]),
            planner.Milestone("m3", "검증", "단일 씬 Play Mode 검증"),
        ])
        pruned = planner._prune_unrequested_level_system(plan, "새 씬에 2.5D 플랫포머 MVP", lambda _m: None)
        self.assertEqual([m.title for m in pruned.milestones], ["스크립트", "검증"])

    def test_normalises_ids_and_paths(self):
        plan = planner.validate_plan({
            "mode": "plan",
            "milestones": [
                _milestone_dict(deliverables=["Assets\\Scripts\\A.cs", "C:/outside/B.cs"]),
                _milestone_dict(title="레벨 작성"),
            ],
        })
        self.assertIsNotNone(plan)
        self.assertEqual([m.id for m in plan.milestones], ["m1", "m2"])
        # 백슬래시 정규화, Assets/ 밖 경로는 드롭
        self.assertEqual(plan.milestones[0].deliverables, ["Assets/Scripts/A.cs"])

    def test_single_mode_returns_none(self):
        self.assertIsNone(planner.validate_plan({"mode": "single"}))

    def test_one_milestone_returns_none(self):
        self.assertIsNone(planner.validate_plan({
            "mode": "plan", "milestones": [_milestone_dict()],
        }))

    def test_clamps_to_max_milestones(self):
        plan = planner.validate_plan({
            "mode": "plan",
            "milestones": [_milestone_dict(title=f"단계 {i}") for i in range(8)],
        })
        self.assertEqual(len(plan.milestones), config.PLAN_MAX_MILESTONES)

    def test_invalid_verify_values_are_dropped(self):
        plan = planner.validate_plan({
            "mode": "plan",
            "milestones": [
                _milestone_dict(verify=["compile", "explode"]),
                _milestone_dict(title="두 번째"),
            ],
        })
        self.assertEqual(plan.milestones[0].verify, ["compile"])


class ExtractJsonTests(unittest.TestCase):
    def test_code_fence_is_stripped(self):
        text = '```json\n{"mode": "plan", "milestones": []}\n```'
        self.assertEqual(planner._extract_json(text)["mode"], "plan")

    def test_prose_around_json(self):
        text = '계획은 다음과 같습니다: {"mode": "single"} 이상입니다.'
        self.assertEqual(planner._extract_json(text), {"mode": "single"})

    def test_garbage_returns_none(self):
        self.assertIsNone(planner._extract_json("no json here"))


class LedgerTests(unittest.TestCase):
    def test_collects_only_successful_results(self):
        import json
        ledger = planner.ArtifactLedger()
        ok = json.dumps({"status": "ok", "result": {"path": "Scene/Player"}})
        err = json.dumps({"status": "error", "error": "boom"})
        ledger.observe("unity_write_script", {"path": "Assets/Scripts/A.cs"}, ok)
        ledger.observe("unity_write_level", {"path": "Assets/StreamingAssets/Levels/level1.json"}, ok)
        ledger.observe("unity_write_script", {"path": "Assets/Scripts/Bad.cs"}, err)
        ledger.observe("unity_create_gameobject", {"name": "Player"}, ok)
        summary = ledger.summary()
        self.assertIn("Assets/Scripts/A.cs", summary)
        self.assertIn("level1.json", summary)
        self.assertNotIn("Bad.cs", summary)
        self.assertIn("Scene/Player", summary)

    def test_collects_success_with_host_suffix(self):
        import json
        ledger = planner.ArtifactLedger()
        ok_with_note = json.dumps({"status": "ok", "result": {}}) + "\n[host note]"
        ledger.observe("unity_write_script", {"path": "Assets/Scripts/A.cs"}, ok_with_note)
        self.assertIn("Assets/Scripts/A.cs", ledger.summary())

    def test_report_marks_failures(self):
        ledger = planner.ArtifactLedger()
        ledger.milestone_done("첫 단계", True)
        ledger.milestone_done("둘째 단계", False, "iteration limit")
        ledger.milestone_pending("셋째 단계")
        report = ledger.report()
        self.assertIn("✓ 첫 단계", report)
        self.assertIn("✗ 둘째 단계", report)
        self.assertIn("iteration limit", report)
        self.assertIn("· 셋째 단계 (미착수)", report)


if __name__ == "__main__":
    unittest.main()


class DegradationIsRecordedTests(unittest.IsolatedAsyncioTestCase):
    """큰 요청이 계획 없이 도는 것은 결과가 완전히 다른데 흔적이 없었다.

    `20260809_123343` — 플랫포머 한 스테이지 요청이 "큰 요청으로 판단해 실행
    계획을 먼저 세웁니다"를 찍고도 `mode = single`로 돌았고, 로그에 이유가
    없었다. 모델이 `mode: "single"`을 고르는 경로만 조용했다.
    """

    class _Resp:
        def __init__(self, content):
            self.message = type("M", (), {"content": content})()

    class _Client:
        def __init__(self, content):
            self._content = content

        async def chat(self, **kwargs):
            return DegradationIsRecordedTests._Resp(self._content)

    async def _reasons(self, content):
        seen = []
        plan = await planner.make_plan(
            self._Client(content), "m", "플랫포머 한 스테이지를 만들어줘",
            lambda msg: None, lambda code, detail=None: seen.append(code),
        )
        return plan, seen

    async def test_the_model_opting_out_is_recorded(self):
        plan, seen = await self._reasons('{"mode": "single", "milestones": []}')
        self.assertIsNone(plan)
        self.assertEqual(seen, ["model_chose_single"])

    async def test_unusable_json_is_recorded(self):
        plan, seen = await self._reasons("not json at all")
        self.assertIsNone(plan)
        self.assertEqual(seen[-1], "invalid_plan_json")

    async def test_a_usable_plan_records_nothing(self):
        plan, seen = await self._reasons(json.dumps({
            "mode": "plan",
            "milestones": [
                _milestone_dict(title="씬 만들기", deliverables=[],
                                goal="새 씬을 만든다", verify=["scene"]),
                _milestone_dict(title="플레이어 이동", deliverables=[],
                                goal="이동 스크립트를 쓴다", verify=["compile"]),
            ],
        }))
        self.assertIsNotNone(plan)
        self.assertEqual(seen, [])
