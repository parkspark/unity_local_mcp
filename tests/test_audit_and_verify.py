import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

import config
from agent import Agent
from audit_logging import ToolAuditLogger
from mcp_client import (
    UnityTools, _editor_log_compile_errors, _supplement_console_errors,
)


def _records(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class AuditLoggerTests(unittest.TestCase):
    def test_editor_log_recovers_latest_failed_compile_errors(self):
        with tempfile.TemporaryDirectory() as project:
            logs = os.path.join(project, "Logs")
            os.makedirs(logs)
            editor_log = os.path.join(logs, "Editor.log")
            with open(editor_log, "w", encoding="utf-8") as handle:
                handle.write(
                    "##### Output\n"
                    "Assets/Scripts/Old.cs(1,1): error CS0001: old\n"
                    "*** Tundra build failed\n"
                    "*** Tundra build success\n"
                    "##### Output\n"
                    "Assets/Scripts/PlayerInputHandler.cs(38,28): error CS1061: "
                    "PlayerMovement has no SetMoveInput\n"
                    "Assets/Scripts/PlayerInputHandler.cs(38,28): error CS1061: "
                    "PlayerMovement has no SetMoveInput\n"
                    "*** Tundra build failed\n"
                )

            errors = _editor_log_compile_errors(project)
            self.assertEqual(len(errors), 1)
            self.assertIn("PlayerInputHandler.cs", errors[0]["message"])
            supplemented = json.loads(_supplement_console_errors(
                '{"status":"ok","result":{"entries":[],"totalBuffered":0}}',
                {"types": "error,exception"},
                project,
            ))
            self.assertEqual(len(supplemented["result"]["entries"]), 1)
            self.assertEqual(supplemented["result"]["editorLogRecovered"], 1)

    def test_later_success_clears_durable_compile_errors(self):
        with tempfile.TemporaryDirectory() as project:
            logs = os.path.join(project, "Logs")
            os.makedirs(logs)
            with open(os.path.join(logs, "Editor.log"), "w", encoding="utf-8") as handle:
                handle.write(
                    "##### Output\n"
                    "Assets/Scripts/X.cs(1,1): error CS0001: broken\n"
                    "*** Tundra build failed\n"
                    "*** Tundra build success\n"
                )
            self.assertEqual(_editor_log_compile_errors(project), [])

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


class HostFileToolIdentityTests(unittest.TestCase):
    """Host file tools must obey the same project guard as bridge mutations.

    The Editor was open on `My project (58)` while the run targeted
    `My project (55)`: every scene and component call was rejected, but three
    scripts were still written into (55) — a project nobody was working in.
    """

    def _tools(self, project_dir, bridge_project):
        tools = UnityTools.__new__(UnityTools)
        tools._audit_log = None
        tools.audit_log_error = None
        tools.tool_mode = "full"
        tools.last_raw_result = ""
        tools._project_dir = project_dir
        tools._project_identity_verified = False
        tools._schemas = {}

        async def ping(_name, _args):
            return json.dumps({
                "status": "ok",
                "result": {"projectPath": bridge_project},
            }, ensure_ascii=False)

        tools._call_once = ping
        return tools

    def test_an_argument_the_tool_does_not_accept_is_refused_not_dropped(self):
        """모르는 인자를 버리고 성공을 돌려주면 모델이 하지도 않은 일을 믿는다.

        2026-08-05 실측: `unity_modify_gameobject {target: "Player",
        tag: "Player"}`가 `{"status":"ok"}`를 받았는데 태그는 붙지 않았다. 그
        도구에 `tag` 파라미터가 없다. 모델은 태그를 설정했다고 믿고
        `CompareTag("Player")`로 분기하는 코인 획득 코드를 썼고, 그 분기는
        영원히 거짓이었다.
        """
        with tempfile.TemporaryDirectory() as project:
            tools = self._tools(project, project)
            tools._schemas = {"unity_modify_gameobject": {"properties": {
                "target": {}, "name": {}, "position": {},
            }}}
            called = []

            async def spy(name, args):
                called.append((name, args))
                return json.dumps({"status": "ok", "result": {}}, ensure_ascii=False)

            tools._call_once = spy
            with mock.patch.object(config, "UNITY_PROJECT_DIR", project):
                text = asyncio.run(tools.call(
                    "unity_modify_gameobject", {"target": "Player", "tag": "Player"}
                ))

            payload = json.loads(text)
            self.assertEqual(payload["status"], "error")
            self.assertIn("tag", payload["error"])
            self.assertIn("marker component", payload["error"])
            # 브리지까지 가지 않아야 한다 — 조용한 성공이 문제였으므로
            self.assertEqual(called, [])

    def test_accepted_arguments_still_go_through(self):
        with tempfile.TemporaryDirectory() as project:
            tools = self._tools(project, project)
            tools._schemas = {"unity_modify_gameobject": {"properties": {
                "target": {}, "position": {},
            }}}
            called = []

            async def spy(name, args):
                called.append((name, args))
                return json.dumps({"status": "ok", "result": {}}, ensure_ascii=False)

            tools._call_once = spy
            with mock.patch.object(config, "UNITY_PROJECT_DIR", project):
                asyncio.run(tools.call(
                    "unity_modify_gameobject",
                    {"target": "Wall", "position": [1, 2, 3]},
                ))
            self.assertEqual(len(called), 1)

    def test_an_unknown_schema_does_not_block_anything(self):
        """스키마가 아직 없을 때(세션 연결 전) 모르는 것을 근거로 막지 않는다."""
        with tempfile.TemporaryDirectory() as project:
            tools = self._tools(project, project)
            self.assertEqual(
                tools.unknown_arguments("unity_modify_gameobject", {"tag": "Player"}),
                [],
            )

    def test_write_script_is_refused_when_the_editor_is_on_another_project(self):
        with tempfile.TemporaryDirectory() as expected, tempfile.TemporaryDirectory() as actual:
            tools = self._tools(expected, actual)
            target = os.path.join(expected, "Assets", "Scripts", "Ghost.cs")
            with mock.patch.object(config, "UNITY_PROJECT_DIR", expected):
                text = asyncio.run(tools.call("unity_write_script", {
                    "path": "Assets/Scripts/Ghost.cs",
                    "content": "public class Ghost {}",
                }))

            payload = json.loads(text)
            self.assertEqual(payload["status"], "error")
            self.assertIn("project mismatch", payload["error"])
            self.assertFalse(
                os.path.exists(target),
                "no file may be written into a project the Editor is not showing",
            )

    def test_write_script_proceeds_when_the_projects_agree(self):
        with tempfile.TemporaryDirectory() as project:
            tools = self._tools(project, project)
            with mock.patch.object(config, "UNITY_PROJECT_DIR", project):
                text = asyncio.run(tools.call("unity_write_script", {
                    "path": "Assets/Scripts/Ghost.cs",
                    "content": "public class Ghost {}",
                }))

            self.assertEqual(json.loads(text)["status"], "ok")
            self.assertTrue(
                os.path.exists(os.path.join(project, "Assets", "Scripts", "Ghost.cs"))
            )


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
