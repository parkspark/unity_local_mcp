import json
import unittest
from unittest import mock

import config
from task_contract import TaskContract
from agent import _screenshot_path


OK = json.dumps({"status": "ok", "result": {}})
LEVEL_LOADED = json.dumps({
    "status": "ok",
    "result": {"logs": [{"message": "[LevelLoader] Loaded Level 1: 1 platforms"}]},
})


def player_at(x, y=1, z=0):
    return json.dumps({
        "status": "ok",
        "result": {"transform": {"position": [x, y, z]}},
    })


class TaskContractTests(unittest.TestCase):
    def test_script_path_before_korean_postposition_is_scoped(self):
        contract = TaskContract.from_request(
            "Assets/Scripts/PlayerMovement25D.cs를 읽고 점프키를 알려줘"
        )
        args, violation = contract.prepare_call(
            "unity_read_script", {"path": "Assets/Scripts/PlayerMovement25D.cs"}
        )
        self.assertEqual(args["path"], "Assets/Scripts/PlayerMovement25D.cs")
        self.assertIsNone(violation)

    def test_scene_mutations_cannot_escape_canonical_request_path(self):
        contract = TaskContract.from_request(
            "Assets/Scenes/Platformer25D_MVP.unity 씬을 만들어줘"
        )
        _, violation = contract.prepare_call(
            "unity_save_scene",
            {"path": "Assets/Scenes/Platformer25D_MVP_22.unity"},
        )
        self.assertIn("canonical scene target", violation)
        args, violation = contract.prepare_call(
            "unity_save_scene",
            {"path": "Assets/Scenes/Platformer25D_MVP.unity"},
        )
        self.assertIsNone(violation)
        self.assertEqual(args["path"], "Assets/Scenes/Platformer25D_MVP.unity")

    def test_extracts_screenshot_path_without_guessing(self):
        self.assertEqual(
            _screenshot_path(json.dumps({"status": "ok", "result": {"path": "C:/temp/game.png"}})),
            "C:/temp/game.png",
        )
        self.assertIsNone(_screenshot_path("not json"))

    def test_blocks_unscoped_existing_script_reads(self):
        contract = TaskContract.from_request("새 게임을 만들어 줘")
        with unittest.mock.patch.object(config, "ALLOW_UNSCOPED_SCRIPT_READ", False):
            _, error = contract.prepare_call("unity_read_script", {"path": "Assets/Scripts/OldSample.cs"})
        self.assertIn("did not explicitly scope", error)

    def test_optionally_allows_unscoped_existing_script_reads_but_not_delete(self):
        contract = TaskContract.from_request("현재 점프 입력을 확인해줘")
        with unittest.mock.patch.object(config, "ALLOW_UNSCOPED_SCRIPT_READ", True):
            _, error = contract.prepare_call("unity_read_script", {"path": "Assets/Scripts/OldSample.cs"})
        self.assertIsNone(error)
        _, error = contract.prepare_call("unity_delete_script", {"path": "Assets/Scripts/OldSample.cs"})
        self.assertIn("did not explicitly scope", error)

    def test_pathless_save_uses_observed_active_scene_path(self):
        contract = TaskContract.from_request("현재 씬을 수정해줘")
        contract.observe("unity_get_state", {}, json.dumps({
            "status": "ok",
            "result": {"activeScene": {"path": "Assets/Scenes/Game.unity"}},
        }))
        args, error = contract.prepare_call("unity_save_scene", {})
        self.assertIsNone(error)
        self.assertEqual(args["path"], "Assets/Scenes/Game.unity")

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

    def test_compile_refresh_accepts_host_note_after_json(self):
        contract = TaskContract.from_request("새 게임을 만들어 줘")
        args, _ = contract.prepare_call("unity_write_script", {"path": "Assets/Scripts/Game.cs"})
        contract.observe("unity_write_script", args, OK)
        contract.observe("unity_refresh_assets", {}, OK + "\n[host waited for compilation]")
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
        self.assertIn("unity_get_gameobject", missing[0])
        self.assertIn("before entering play mode", missing[1])
        contract.observe("unity_get_gameobject", {"target": "Cube"}, OK)
        self.assertEqual(
            contract.missing_verification(),
            ["persist scene changes with unity_save_scene before entering play mode"],
        )
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


class LevelAndInputContractTests(unittest.TestCase):
    def test_level_path_policy(self):
        contract = TaskContract.from_request("레벨 만들어 줘")
        _, error = contract.prepare_call("unity_write_level", {"path": "Assets/Scripts/level1.json"})
        self.assertIn("Policy blocked level access", error)
        _, error = contract.prepare_call(
            "unity_write_level", {"path": "Assets/StreamingAssets/Levels/level1.json"}
        )
        self.assertIsNone(error)

    def test_level_write_requires_runtime_verification(self):
        contract = TaskContract.from_request("레벨 만들어 줘")
        contract.observe(
            "unity_write_level", {"path": "Assets/StreamingAssets/Levels/level1.json"}, OK
        )
        self.assertTrue(any("[LevelLoader] Loaded" in m for m in contract.missing_verification()))
        contract.observe("unity_play_mode", {"action": "play"}, OK)
        contract.observe("unity_wait", {"seconds": 1}, OK)
        contract.observe("unity_read_console", {}, LEVEL_LOADED)
        contract.observe("unity_play_mode", {"action": "stop"}, OK)
        self.assertEqual(contract.missing_verification(), [])

    def test_level_runtime_verification_requires_loaded_marker(self):
        contract = TaskContract.from_request("레벨 만들어 줘")
        contract.observe(
            "unity_write_level", {"path": "Assets/StreamingAssets/Levels/level1.json"}, OK
        )
        contract.observe("unity_play_mode", {"action": "play"}, OK)
        contract.observe("unity_wait", {"seconds": 1}, OK)
        contract.observe("unity_read_console", {"types": "error,exception"}, OK)
        self.assertTrue(any("[LevelLoader] Loaded" in m for m in contract.missing_verification()))

    def test_loader_install_requires_compile_cycle(self):
        contract = TaskContract.from_request("레벨 만들어 줘")
        contract.observe("unity_install_level_loader", {}, OK)
        self.assertIn("unity_refresh_assets", contract.missing_verification()[0])
        # 설치된 로더는 세션 스크립트로 취급되어 읽기가 허용된다
        _, error = contract.prepare_call("unity_read_script", {"path": "Assets/Scripts/LevelLoader.cs"})
        self.assertIsNone(error)

    def test_send_key_blocked_before_play(self):
        contract = TaskContract.from_request("게임 조작 테스트")
        _, error = contract.prepare_call("unity_send_key", {"key": "leftArrow"})
        self.assertIn("enter play mode first", error)
        contract.observe("unity_play_mode", {"action": "play"}, OK)
        _, error = contract.prepare_call("unity_send_key", {"key": "leftArrow"})
        self.assertIsNone(error)

    def test_input_keyword_requires_input_sim(self):
        contract = TaskContract.from_request("플레이 검증까지 해 줘")
        self.assertTrue(contract.require_input_sim)
        self.assertTrue(contract.require_play)
        contract.observe("unity_play_mode", {"action": "play"}, OK)
        contract.observe("unity_wait", {"seconds": 1}, OK)
        contract.observe("unity_read_console", {"types": "error,exception"}, OK)
        self.assertTrue(any("Player position" in m for m in contract.missing_verification()))
        contract.observe("unity_get_gameobject", {"target": "Player"}, player_at(0))
        contract.observe("unity_send_key", {"key": "rightArrow"}, OK)
        self.assertTrue(any("Player again" in m for m in contract.missing_verification()))
        contract.observe("unity_get_gameobject", {"target": "Player"}, player_at(2))
        contract.observe("unity_play_mode", {"action": "stop"}, OK)
        self.assertEqual(contract.missing_verification(), [])

    def test_unchanged_player_position_fails_input_verification(self):
        contract = TaskContract.from_request("키 입력 테스트해 줘")
        contract.observe("unity_play_mode", {"action": "play"}, OK)
        contract.observe("unity_wait", {"seconds": 1}, OK)
        contract.observe("unity_read_console", {"types": "error,exception"}, OK)
        contract.observe("unity_get_gameobject", {"target": "Player"}, player_at(0))
        contract.observe("unity_send_key", {"key": "rightArrow"}, OK)
        contract.observe("unity_get_gameobject", {"target": "Player"}, player_at(0))
        self.assertTrue(any("did not change" in m for m in contract.missing_verification()))

    def test_input_request_cannot_finish_without_play_mode(self):
        contract = TaskContract.from_request("키 입력 테스트해 줘")
        self.assertTrue(any("play mode" in m for m in contract.missing_verification()))

    def test_failed_level_write_does_not_count(self):
        contract = TaskContract.from_request("레벨 만들어 줘")
        err = json.dumps({"status": "error", "error": "level validation failed"})
        contract.observe(
            "unity_write_level", {"path": "Assets/StreamingAssets/Levels/level1.json"}, err
        )
        self.assertEqual(contract.levels_written, set())


if __name__ == "__main__":
    unittest.main()
