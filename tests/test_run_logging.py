import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

import config
from agent import Agent
from run_logging import RunLogger


class FakeTools:
    ollama_tools = []

    async def call(self, name, args):
        return json.dumps({"status": "ok", "result": {"echo": args}})


class LoggedScriptedAgent(Agent):
    def __init__(self, turns, enable_logging=True):
        super().__init__(
            FakeTools(),
            lambda _text: None,
            lambda _name, _args, _result: None,
            lambda _message: None,
            enable_logging=enable_logging,
            enable_verification=False,
        )
        self.turns = iter(turns)

    async def _chat(self, messages=None, use_tools=True):
        value = next(self.turns)
        if isinstance(value, BaseException):
            raise value
        content, calls = value
        self._log(
            "assistant_response",
            content=content,
            tool_calls=[{"name": name, "arguments": args} for name, args in calls],
            tools_enabled=use_tools,
        )
        return content, calls


def _records(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class RunLoggerTests(unittest.TestCase):
    def test_writes_human_and_jsonl_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = RunLogger(tmp, "큐브 하나 만들어 줘", "test-model")
            logger.event("tool_result", name="unity_ping", arguments={}, result="ok")
            logger.close("completed")

            self.assertTrue(os.path.exists(logger.text_path))
            self.assertTrue(os.path.exists(logger.jsonl_path))
            events = [record["event"] for record in _records(logger.jsonl_path)]
            self.assertEqual(events, ["run_started", "tool_result", "run_finished"])
            with open(logger.text_path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("큐브 하나 만들어 줘", text)
            self.assertIn("unity_ping", text)

    def test_agent_turn_logs_tools_and_completed_outcome(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(config, "RUN_LOG_DIR", tmp), \
             mock.patch.object(config, "PLANNER", "off"):
            agent = LoggedScriptedAgent([
                ("", [("unity_ping", {"detail": "full"})]),
                ("완료", []),
            ])
            success = asyncio.run(agent.run_turn("로그 통합 테스트"))

            self.assertTrue(success)
            self.assertIsNotNone(agent.last_run_log_paths)
            text_path, jsonl_path = agent.last_run_log_paths
            self.assertTrue(os.path.exists(text_path))
            records = _records(jsonl_path)
            self.assertEqual(records[0]["event"], "run_started")
            self.assertEqual(records[0]["request"], "로그 통합 테스트")
            tool = next(record for record in records if record["event"] == "tool_result")
            self.assertEqual(tool["arguments"], {"detail": "full"})
            self.assertEqual(records[-1]["outcome"], "completed")

    def test_agent_turn_logs_exception_outcome(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(config, "RUN_LOG_DIR", tmp), \
             mock.patch.object(config, "PLANNER", "off"):
            agent = LoggedScriptedAgent([RuntimeError("model failed")])
            with self.assertRaisesRegex(RuntimeError, "model failed"):
                asyncio.run(agent.run_turn("실패도 기록"))

            records = _records(agent.last_run_log_paths[1])
            self.assertTrue(any(record["event"] == "exception" for record in records))
            self.assertEqual(records[-1]["outcome"], "error")

    def test_logging_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(config, "RUN_LOG_DIR", tmp), \
             mock.patch.object(config, "PLANNER", "off"):
            agent = LoggedScriptedAgent([("완료", [])], enable_logging=False)
            asyncio.run(agent.run_turn("로그 끔"))
            self.assertIsNone(agent.last_run_log_paths)
            self.assertEqual(os.listdir(tmp), [])

    def test_agent_turn_logs_interrupted_outcome(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(config, "RUN_LOG_DIR", tmp), \
             mock.patch.object(config, "PLANNER", "off"):
            agent = LoggedScriptedAgent([asyncio.CancelledError()])
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(agent.run_turn("중단도 기록"))

            records = _records(agent.last_run_log_paths[1])
            self.assertEqual(records[-1]["outcome"], "interrupted")

    def test_log_io_failure_does_not_block_agent_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            invalid_root = os.path.join(tmp, "not-a-directory")
            with open(invalid_root, "w", encoding="utf-8") as handle:
                handle.write("occupied")
            with mock.patch.object(config, "RUN_LOG_DIR", invalid_root), \
                 mock.patch.object(config, "PLANNER", "off"):
                agent = LoggedScriptedAgent([("완료", [])])
                success = asyncio.run(agent.run_turn("로그 실패 격리"))

            self.assertTrue(success)
            self.assertIsNone(agent.last_run_log_paths)
            self.assertRegex(agent._run_log_error, r"File(?:Exists|NotFound)Error")


if __name__ == "__main__":
    unittest.main()
