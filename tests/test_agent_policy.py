import asyncio
import json
import os
import tempfile
import types
import unittest
from unittest import mock

import config
import ollama
import planner
from agent import Agent, _milestone_prompt, _retryable_model_error


class FakeTools:
    def __init__(self):
        self.calls = []
        self.ollama_tools = []

    async def call(self, name, args):
        self.calls.append((name, args))
        return json.dumps({"status": "ok", "result": {}})


class ScriptedAgent(Agent):
    def __init__(self, tools, turns, events, plan=None):
        super().__init__(
            tools,
            lambda _text: None,
            lambda name, args, result: events.append((name, args, result)),
            events.append,
            enable_logging=False,
            enable_verification=False,
        )
        self.turns = iter(turns)
        self.plan = plan
        self.chat_prompts = []  # 각 _chat 호출 시점의 (마지막 user 메시지) 스냅샷

    async def _make_plan(self, user_text):
        if self.plan is not None:
            self.plan.request = user_text
        return self.plan

    async def _chat(self, messages=None, use_tools=True):
        msgs = self.history if messages is None else messages
        users = [m["content"] for m in msgs if m.get("role") == "user"]
        self.chat_prompts.append(users[-1] if users else "")
        return next(self.turns)


class AgentPolicyTests(unittest.TestCase):
    def test_retryable_ollama_transport_error_is_retried_once(self):
        class FlakyClient:
            def __init__(self):
                self.calls = 0

            async def chat(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise ollama.ResponseError(
                        "XML syntax error: unexpected EOF", status_code=-1
                    )

                async def chunks():
                    yield types.SimpleNamespace(
                        message=types.SimpleNamespace(
                            content="recovered", tool_calls=[]
                        )
                    )

                return chunks()

        warnings = []
        agent = Agent(
            FakeTools(), lambda _text: None, lambda *_: None, warnings.append,
            enable_logging=False, enable_verification=False,
        )
        agent.client = FlakyClient()
        with mock.patch.object(config, "STREAM", True), \
             mock.patch.object(config, "MODEL_CALL_RETRIES", 1):
            content, calls = asyncio.run(agent._chat([]))

        self.assertEqual(content, "recovered")
        self.assertEqual(calls, [])
        self.assertEqual(agent.client.calls, 2)
        self.assertTrue(any("재시도" in warning for warning in warnings))

    def test_non_retryable_ollama_error_is_not_retried(self):
        self.assertFalse(
            _retryable_model_error(
                ollama.ResponseError("bad request", status_code=400)
            )
        )

    def test_policy_blocks_native_menu_call_before_tool_execution(self):
        tools = FakeTools()
        events = []
        agent = ScriptedAgent(tools, [
            ("", [("unity_execute_menu_item", {"menu_path": "File/New Scene"})]),
            ("done", []),
        ], events)

        asyncio.run(agent.run_turn("새 씬을 만들어 줘"))

        self.assertEqual(tools.calls, [])
        self.assertTrue(any("Policy blocked" in str(event) for event in events))

    def test_agent_continues_until_compile_verification_is_complete(self):
        tools = FakeTools()
        events = []
        agent = ScriptedAgent(tools, [
            ("", [("unity_write_script", {"path": "Assets/Scripts/TestGame.cs", "content": "class TestGame {}"})]),
            ("premature completion", []),
            ("", [("unity_refresh_assets", {})]),
            ("", [("unity_read_console", {"types": "error,exception"})]),
            ("verified", []),
        ], events)

        asyncio.run(agent.run_turn("새 테스트 게임을 만들어 줘"))

        self.assertEqual([name for name, _ in tools.calls], [
            "unity_write_script", "unity_refresh_assets", "unity_read_console",
        ])
        self.assertTrue(any("Verification is incomplete" in str(event) for event in events))


def _plan(milestones):
    return planner.Plan(request="", milestones=milestones)


class PlanExecutionTests(unittest.TestCase):
    def test_retry_prompt_reuses_partial_successes(self):
        plan = _plan([
            planner.Milestone(
                id="m1", title="게임 구성", goal="스크립트와 씬 구성",
                deliverables=["Assets/Scripts/Foo.cs"],
            ),
        ])
        plan.request = "게임을 만들어 줘"
        ledger = planner.ArtifactLedger()
        ledger.observe(
            "unity_write_script",
            {"path": "Assets/Scripts/Foo.cs"},
            '{"status":"ok","result":{"written":"Assets/Scripts/Foo.cs"}}',
        )

        prompt = _milestone_prompt(plan, 0, ledger, "tool-call iteration limit reached")

        self.assertIn("Assets/Scripts/Foo.cs", prompt)
        self.assertIn("이미 성공한 결과", prompt)
        self.assertIn("이전 검증의 미완료 부분부터 이어서", prompt)

    def test_two_milestones_run_with_ledger_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "Assets", "Scripts"))
            open(os.path.join(tmp, "Assets", "Scripts", "Foo.cs"), "w").close()
            plan = _plan([
                planner.Milestone(id="m1", title="스크립트 작성", goal="Foo.cs를 작성",
                                  deliverables=["Assets/Scripts/Foo.cs"], verify=["compile"]),
                planner.Milestone(id="m2", title="마무리 확인", goal="상태 확인만"),
            ])
            tools = FakeTools()
            events = []
            agent = ScriptedAgent(tools, [
                # m1
                ("", [("unity_write_script", {"path": "Assets/Scripts/Foo.cs", "content": "class Foo {}"})]),
                ("", [("unity_refresh_assets", {})]),
                ("", [("unity_read_console", {"types": "error,exception"})]),
                ("done", []),
                # m2
                ("확인 완료", []),
            ], events, plan=plan)

            with mock.patch.object(config, "UNITY_PROJECT_DIR", tmp), \
                 mock.patch.object(config, "PLANNER", "always"):
                asyncio.run(agent.run_turn("플랫포머 게임을 만들어 줘"))

            # 마일스톤 경계마다 unity_ping으로 연결을 확인한다
            self.assertEqual([n for n, _ in tools.calls if n == "unity_ping"], ["unity_ping"] * 2)
            # m2의 프롬프트에 m1 산출물(ledger)과 완료 표시가 주입된다
            m2_prompt = agent.chat_prompts[-1]
            self.assertIn("Assets/Scripts/Foo.cs", m2_prompt)
            self.assertIn("완료", m2_prompt)
            # 최종 보고가 히스토리에 남는다
            self.assertIn("✓", agent.history[-1]["content"])

    def test_missing_deliverable_fails_retries_then_the_plan_continues(self):
        """v1.12.1 이전에는 여기서 계획 전체를 버렸다.

        기록된 plan 모드 15회에서 계획된 71개 중 39개가 시작조차 못 했고
        11/15회가 그렇게 중단됐다. 라이브에서도 m3이 한도를 치자 m4·m5가
        날아갔다(`20260809_132123`) — 둘 다 실패한 마일스톤에 의존하지 않았다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            plan = _plan([
                planner.Milestone(id="m1", title="스크립트 작성", goal="Missing.cs를 작성",
                                  deliverables=["Assets/Scripts/Missing.cs"]),
                planner.Milestone(id="m2", title="다음 작업", goal="실패해도 실행된다"),
            ])
            tools = FakeTools()
            events = []
            agent = ScriptedAgent(tools, [
                ("done", []),           # 시도 1: 파일을 만들지 않고 완료 선언
                ("done again", []),     # 재시도: 여전히 안 만듦
                ("m2 done", []),        # m2는 그래도 실행된다
            ], events, plan=plan)

            with mock.patch.object(config, "UNITY_PROJECT_DIR", tmp), \
                 mock.patch.object(config, "PLANNER", "always"), \
                 mock.patch.object(config, "MILESTONE_RETRIES", 1):
                asyncio.run(agent.run_turn("플랫포머 게임을 만들어 줘"))

            # 재시도 프롬프트에 실패 원인이 들어간다
            self.assertTrue(any("deliverables not created" in p for p in agent.chat_prompts))
            # m1 두 번 + m2 한 번
            self.assertEqual(len([n for n, _ in tools.calls if n == "unity_ping"]), 3)
            self.assertIn("✗", agent.history[-1]["content"])
            self.assertIn("✓ 다음 작업", agent.history[-1]["content"])
            self.assertNotIn("미착수", agent.history[-1]["content"])

    def test_plan_budget_charges_actual_iterations_not_full_milestone_limit(self):
        plan = _plan([
            planner.Milestone(id="m1", title="빠른 첫 단계", goal="확인", max_iters=2),
            planner.Milestone(id="m2", title="빠른 둘째 단계", goal="확인", max_iters=2),
        ])
        tools = FakeTools()
        events = []
        agent = ScriptedAgent(tools, [("m1 done", []), ("m2 done", [])], events, plan=plan)

        with mock.patch.object(config, "PLANNER", "always"), \
             mock.patch.object(config, "PLAN_MAX_TOTAL_ITERS", 2), \
             mock.patch.object(config, "MILESTONE_RETRIES", 0):
            asyncio.run(agent.run_turn("플랫포머 게임을 만들어 줘"))

        # Each milestone used one iteration. Reserving max_iters up front would
        # incorrectly exhaust the total budget after m1 and skip m2.
        self.assertEqual(len([n for n, _ in tools.calls if n == "unity_ping"]), 2)
        self.assertIn("✓ 빠른 둘째 단계", agent.history[-1]["content"])

    def test_small_request_skips_planner(self):
        tools = FakeTools()
        events = []
        agent = ScriptedAgent(tools, [("큐브를 만들었습니다", [])], events, plan=None)
        # plan=None이어도 looks_large가 False면 _make_plan 자체가 호출되지 않아야 한다
        agent._make_plan = None  # 호출되면 TypeError
        asyncio.run(agent.run_turn("큐브 하나 상태 확인"))
        self.assertEqual(tools.calls, [])


if __name__ == "__main__":
    unittest.main()


class PlanContinuesAfterAFailedMilestoneTests(unittest.IsolatedAsyncioTestCase):
    """하나가 실패했다고 계획 전체를 버리면 뒤가 통째로 사라진다.

    기록된 plan 모드 15회에서 계획된 71개 중 39개가 시작조차 못 했고 11/15회가
    이렇게 중단됐다. 라이브에서도 m3이 한도를 치자 m4·m5가 날아갔는데
    (`20260809_132123`), 그 둘은 실패한 마일스톤에 의존하지 않았다.
    """

    def _agent(self):
        agent = Agent.__new__(Agent)
        agent.history = []
        agent.on_warn = lambda msg: None
        agent.on_text = lambda msg: None
        agent.on_milestone = lambda idx, total, title: None
        agent._log = lambda *a, **k: None
        return agent

    async def test_the_later_milestones_still_run(self):
        plan = planner.validate_plan({
            "mode": "plan",
            "milestones": [
                {"title": "m%d" % i, "goal": "g", "deliverables": [], "verify": []}
                for i in (1, 2, 3)
            ],
        })
        agent = self._agent()
        started = []

        async def run(_plan, idx, _ledger, prev_error=None, max_iters=None):
            started.append(idx)
            # 가운데 마일스톤만 실패시킨다
            if idx == 1:
                return False, "tool-call iteration limit reached", 5
            return True, "", 3

        agent._run_milestone = run
        ok = await agent._run_plan("요청", plan)

        self.assertEqual(started.count(0), 1)
        self.assertIn(2, started, "실패 뒤의 마일스톤이 실행되어야 한다")
        self.assertFalse(ok, "실패가 있으면 계획 전체는 성공이 아니다")

    async def test_an_exhausted_budget_still_stops(self):
        plan = planner.validate_plan({
            "mode": "plan",
            "milestones": [
                {"title": "m%d" % i, "goal": "g", "deliverables": [], "verify": []}
                for i in (1, 2, 3)
            ],
        })
        agent = self._agent()
        started = []

        async def run(_plan, idx, _ledger, prev_error=None, max_iters=None):
            started.append(idx)
            return False, "tool-call iteration limit reached", config.PLAN_MAX_TOTAL_ITERS
        agent._run_milestone = run
        await agent._run_plan("요청", plan)
        self.assertEqual(started, [0], "예산이 바닥나면 멈춘다")


class RetryKnowsWhatAlreadyExistsTests(unittest.TestCase):
    """재시도는 히스토리를 새로 시작한다 — 이미 만든 파일을 모르면 또 만든다.

    라이브 3회 모두 남은 예산 0으로 끝났다(`20260809_132123`·`132831`·`133614`).
    무엇이 있고 무엇이 없는지는 호스트가 디스크에서 결정적으로 안다.
    """

    def _plan(self):
        return _plan([
            planner.Milestone(id="m1", title="스크립트", goal="둘을 만든다",
                              deliverables=["Assets/Scripts/A.cs", "Assets/Scripts/B.cs"]),
        ])

    def test_the_first_attempt_lists_everything(self):
        prompt = _milestone_prompt(self._plan(), 0, planner.ArtifactLedger())
        self.assertIn("이 마일스톤이 만들어야 하는 파일", prompt)
        self.assertNotIn("이미 만들어져 있는 파일", prompt)

    def test_a_retry_separates_what_exists_from_what_is_missing(self):
        prompt = _milestone_prompt(
            self._plan(), 0, planner.ArtifactLedger(), prev_error="한도 소진",
            missing=["Assets/Scripts/B.cs"],
        )
        self.assertIn("이미 만들어져 있는 파일", prompt)
        self.assertIn("Assets/Scripts/A.cs", prompt.split("이미 만들어져 있는 파일")[1])
        self.assertIn("아직 없는 파일", prompt)

    def test_a_retry_with_everything_present_says_so(self):
        prompt = _milestone_prompt(
            self._plan(), 0, planner.ArtifactLedger(), prev_error="한도 소진",
            missing=[],
        )
        self.assertIn("파일은 모두 있다", prompt)
        self.assertNotIn("아직 없는 파일", prompt)


class LimitWithCompleteDeliverablesTests(unittest.IsolatedAsyncioTestCase):
    """한도를 쳤는데 만들어야 할 파일이 전부 있으면 재시도는 낭비다.

    재생 근거: 한도를 친 마일스톤 10건 중 산출물이 전부 있던 것은 1건. 드물게
    발동하고, 발동하면 재시도 한 번(12 이터레이션)을 아낀다.
    """

    async def _limit_case(self, deliverables, created):
        with tempfile.TemporaryDirectory() as tmp:
            for rel in created:
                path = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "w", encoding="utf-8").close()
            plan = _plan([planner.Milestone(id="m1", title="t", goal="g",
                                            deliverables=deliverables)])
            agent = Agent.__new__(Agent)
            agent._log = lambda *a, **k: None
            agent.known_session_scripts = set()
            agent.tools = types.SimpleNamespace(
                call=lambda name, args: asyncio.sleep(0, result="{}")
            )

            async def loop(*a, **k):
                return False, "tool-call iteration limit reached", 12
            agent._react_loop = loop
            with mock.patch.object(config, "UNITY_PROJECT_DIR", tmp):
                return await agent._run_milestone(plan, 0, planner.ArtifactLedger())

    async def test_all_present_counts_as_done(self):
        ok, note, _used = await self._limit_case(
            ["Assets/Scripts/A.cs"], ["Assets/Scripts/A.cs"])
        self.assertTrue(ok)
        self.assertEqual(note, "")

    async def test_a_missing_file_still_fails(self):
        ok, note, _used = await self._limit_case(["Assets/Scripts/A.cs"], [])
        self.assertFalse(ok)
        self.assertIn("iteration limit", note)

    async def test_a_milestone_without_deliverables_is_not_auto_passed(self):
        ok, _note, _used = await self._limit_case([], [])
        self.assertFalse(ok)
