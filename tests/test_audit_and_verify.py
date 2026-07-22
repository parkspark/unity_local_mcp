import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

import config
from agent import Agent
from audit_logging import ToolAuditLogger
from mcp_client import UnityTools


def _records(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class AuditLoggerTests(unittest.TestCase):
    def test_direct_unitytools_call_is_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = UnityTools.__new__(UnityTools)
            tools.tool_mode = "full"
            tools.audit_log_error = None
            tools._audit_log = ToolAuditLogger(tmp)
            tools.last_raw_result = ""

            async def fake_call(name, args):
                tools.last_raw_result = json.dumps({
                    "status": "ok", "result": {"name": name, "args": args, "full": "x" * 20}
                })
                return '{"status":"ok","result":"truncated"}'

            tools._call_impl = fake_call
            result = asyncio.run(tools.call("unity_ping", {"detail": "full"}))
            path = tools._audit_log.path
            tools._audit_log.close()

            self.assertEqual(json.loads(result)["status"], "ok")
            records = _records(path)
            started = next(r for r in records if r["event"] == "tool_call_started")
            finished = next(r for r in records if r["event"] == "tool_call_finished")
            self.assertEqual(started["name"], "unity_ping")
            self.assertEqual(started["arguments"], {"detail": "full"})
            self.assertEqual(finished["call_id"], started["call_id"])
            self.assertIn('"full": "xxxxxxxxxxxxxxxxxxxx"', finished["result"])

    def test_verify_mode_filters_and_blocks_mutations(self):
        tools = UnityTools.__new__(UnityTools)
        tools._audit_log = None
        tools._all_ollama_tools = [
            {"function": {"name": "unity_ping"}},
            {"function": {"name": "unity_play_mode"}},
            {"function": {"name": "unity_write_script"}},
        ]
        tools.set_tool_mode("verify")

        self.assertEqual(tools.names, ["unity_ping", "unity_play_mode"])
        result = asyncio.run(tools._call_impl("unity_write_script", {"path": "Assets/Scripts/X.cs"}))
        self.assertEqual(json.loads(result)["status"], "error")
        self.assertIn("verification-only", result)


class ModeTools:
    def __init__(self):
        self.tool_mode = "full"
        self.ollama_tools = []
        self.modes = []
        self.calls = []

    def set_tool_mode(self, mode):
        self.tool_mode = mode
        self.modes.append(mode)

    async def call(self, name, args):
        self.calls.append((name, args))
        return json.dumps({"status": "ok", "result": {}})


class VerifyScriptedAgent(Agent):
    def __init__(self, tools, callback):
        super().__init__(tools, callback, lambda *_: None, callback,
                         enable_logging=False, enable_verification=False)
        self.prompts = []

    async def _chat(self, messages=None, use_tools=True):
        self.prompts.append(messages[-1]["content"])
        self.on_text("legacy console cannot print — this")
        self.on_warn("warning — still non-fatal")
        return "verified", []


class VerifyModeTests(unittest.TestCase):
    def test_one_turn_verify_mode_is_forced_and_restored(self):
        tools = ModeTools()

        def cp949_failure(_text):
            raise UnicodeEncodeError("cp949", "—", 0, 1, "cannot encode")

        agent = VerifyScriptedAgent(tools, cp949_failure)
        with mock.patch.object(config, "PLANNER", "always"):
            success = asyncio.run(agent.run_turn("현재 씬만 확인", tool_mode="verify"))

        self.assertTrue(success)
        self.assertEqual(tools.modes, ["verify", "full"])
        self.assertIn("호스트 강제 검증 전용 모드", agent.prompts[0])

    def test_verify_mode_stops_play_after_model_finishes_early(self):
        tools = ModeTools()

        class PlayAgent(VerifyScriptedAgent):
            def __init__(self, tools):
                super().__init__(tools, lambda _text: None)
                self.turns = iter([
                    ("", [("unity_play_mode", {"action": "play"})]),
                    ("검증 종료", []),
                    ("호출 한도 종료 보고", []),
                ])

            async def _chat(self, messages=None, use_tools=True):
                return next(self.turns)

        agent = PlayAgent(tools)
        with mock.patch.object(config, "MAX_ITERS", 2):
            success = asyncio.run(agent.run_turn("상태만 확인", tool_mode="verify"))

        self.assertFalse(success)
        self.assertEqual(tools.calls, [
            ("unity_play_mode", {"action": "play"}),
            ("unity_play_mode", {"action": "stop"}),
        ])


if __name__ == "__main__":
    unittest.main()
