import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

import config
import planner
from agent import Agent


class FakeTools:
    def __init__(self):
        self.calls = []

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

    def test_missing_deliverable_fails_retries_then_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = _plan([
                planner.Milestone(id="m1", title="스크립트 작성", goal="Missing.cs를 작성",
                                  deliverables=["Assets/Scripts/Missing.cs"]),
                planner.Milestone(id="m2", title="다음 작업", goal="실행되면 안 된다"),
            ])
            tools = FakeTools()
            events = []
            agent = ScriptedAgent(tools, [
                ("done", []),           # 시도 1: 파일을 만들지 않고 완료 선언
                ("done again", []),     # 재시도: 여전히 안 만듦
            ], events, plan=plan)

            with mock.patch.object(config, "UNITY_PROJECT_DIR", tmp), \
                 mock.patch.object(config, "PLANNER", "always"), \
                 mock.patch.object(config, "MILESTONE_RETRIES", 1):
                asyncio.run(agent.run_turn("플랫포머 게임을 만들어 줘"))

            # 재시도 프롬프트에 실패 원인이 들어간다
            self.assertTrue(any("deliverables not created" in p for p in agent.chat_prompts))
            # m2는 실행되지 않는다 (ping은 m1 시도 2회만)
            self.assertEqual(len([n for n, _ in tools.calls if n == "unity_ping"]), 2)
            self.assertIn("✗", agent.history[-1]["content"])
            self.assertIn("· 다음 작업 (미착수)", agent.history[-1]["content"])

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
