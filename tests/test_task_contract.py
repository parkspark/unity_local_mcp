import json
import unittest

from task_contract import TaskContract
from agent import _screenshot_path


OK = json.dumps({"status": "ok", "result": {}})


class TaskContractTests(unittest.TestCase):
    def test_extracts_screenshot_path_without_guessing(self):
        self.assertEqual(
            _screenshot_path(json.dumps({"status": "ok", "result": {"path": "C:/temp/game.png"}})),
            "C:/temp/game.png",
        )
        self.assertIsNone(_screenshot_path("not json"))

    def test_blocks_unscoped_existing_script_reads(self):
        contract = TaskContract.from_request("새 게임을 만들어 줘")
        _, error = contract.prepare_call("unity_read_script", {"path": "Assets/Scripts/OldSample.cs"})
        self.assertIn("did not explicitly scope", error)

    def test_allows_scripts_written_in_this_session(self):
        contract = TaskContract.from_request("새 게임을 만들어 줘")
        args, error = contract.prepare_call("unity_write_script", {"path": "Assets/Scripts/Game.cs"})
        self.assertIsNone(error)
        contract.observe("unity_write_script", args, OK)
        _, error = contract.prepare_call("unity_read_script", {"path": "Assets/Scripts/Game.cs"})
        self.assertIsNone(error)

    def test_extracts_multiple_explicit_asset_paths(self):
        contract = TaskContract.from_request(
            "edit Assets/Scripts/Game.cs and open Assets/Scenes/Game.unity"
        )
        self.assertEqual(contract.user_paths, {"Assets/Scripts/Game.cs", "Assets/Scenes/Game.unity"})

    def test_scopes_script_search_and_blocks_menu_dialogs(self):
        contract = TaskContract.from_request("상태 확인")
        args, error = contract.prepare_call("unity_list_assets", {"filter": "t:Script"})
        self.assertIsNone(error)
        self.assertEqual(args["folder"], "Assets/Scripts")
        self.assertEqual(args["limit"], 30)
        _, error = contract.prepare_call("unity_execute_menu_item", {"menu_path": "File/New Scene"})
        self.assertIn("Policy blocked", error)

    def test_requires_compile_and_runtime_verification(self):
        contract = TaskContract.from_request("새 게임을 만들어 줘")
        args, _ = contract.prepare_call("unity_write_script", {"path": "Assets/Scripts/Game.cs"})
        contract.observe("unity_write_script", args, OK)
        self.assertIn("unity_refresh_assets", contract.missing_verification()[0])
        contract.observe("unity_refresh_assets", {}, OK)
        self.assertIn("unity_read_console", contract.missing_verification()[0])
        contract.observe("unity_read_console", {"types": "error,exception"}, OK)
        self.assertEqual(contract.missing_verification(), [])
        contract.observe("unity_play_mode", {"action": "play"}, OK)
        missing = contract.missing_verification()
        self.assertEqual(len(missing), 3)
        contract.observe("unity_wait", {"seconds": 1}, OK)
        contract.observe("unity_read_console", {"types": "error,exception"}, OK)
        contract.observe("unity_play_mode", {"action": "stop"}, OK)
        self.assertEqual(contract.missing_verification(), [])

    def test_requires_scene_query_and_save_after_mutation(self):
        contract = TaskContract.from_request("큐브를 만들어 줘")
        contract.observe("unity_create_gameobject", {"name": "Cube"}, OK)
        missing = contract.missing_verification()
        self.assertEqual(len(missing), 2)
        contract.observe("unity_get_gameobject", {"target": "Cube"}, OK)
        self.assertEqual(contract.missing_verification(), ["persist scene changes with unity_save_scene"])
        contract.observe("unity_save_scene", {}, OK)
        self.assertEqual(contract.missing_verification(), [])

    def test_requires_requested_screenshot_after_play(self):
        contract = TaskContract.from_request("play and capture a screenshot")
        contract.observe("unity_play_mode", {"action": "play"}, OK)
        contract.observe("unity_wait", {"seconds": 1}, OK)
        contract.observe("unity_read_console", {"types": "error,exception"}, OK)
        contract.observe("unity_play_mode", {"action": "stop"}, OK)
        self.assertIn("unity_screenshot", " ".join(contract.missing_verification()))
        contract.observe("unity_screenshot", {}, OK)
        self.assertEqual(contract.missing_verification(), [])


if __name__ == "__main__":
    unittest.main()
