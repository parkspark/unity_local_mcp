import asyncio
import json
import unittest

from agent import Agent


class FakeTools:
    def __init__(self):
        self.calls = []

    async def call(self, name, args):
        self.calls.append((name, args))
        return json.dumps({"status": "ok", "result": {}})


class ScriptedAgent(Agent):
    def __init__(self, tools, turns, events):
        super().__init__(
            tools,
            lambda _text: None,
            lambda name, args, result: events.append((name, args, result)),
            events.append,
        )
        self.turns = iter(turns)

    async def _chat(self, use_tools=True):
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


if __name__ == "__main__":
    unittest.main()
