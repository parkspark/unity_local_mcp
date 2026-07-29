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

    def test_allows_deleting_only_script_named_by_current_compiler_error(self):
        contract = TaskContract.from_request("fix the current Unity compilation failure")
        console = json.dumps({
            "status": "ok",
            "result": {
                "entries": [{
                    "message": (
                        r"Assets\Scripts\PlayerInputHandler.cs(38,28): "
                        "error CS1061: PlayerMovement has no SetMoveInput"
                    ),
                }],
            },
        })
        contract.observe("unity_read_console", {"types": "error,exception"}, console)

        _, error = contract.prepare_call(
            "unity_delete_script",
            {"path": "Assets/Scripts/PlayerInputHandler.cs"},
        )
        self.assertIsNone(error)
        _, error = contract.prepare_call(
            "unity_delete_script",
            {"path": "Assets/Scripts/Unrelated.cs"},
        )
        self.assertIn("did not explicitly scope", error)

    def test_non_compiler_console_text_does_not_authorize_script_delete(self):
        contract = TaskContract.from_request("inspect the current Unity errors")
        console = json.dumps({
            "status": "ok",
            "result": {
                "entries": [{
                    "message": "Assets/Scripts/OldSample.cs has a warning but no compiler diagnostic",
                }],
            },
        })
        contract.observe("unity_read_console", {"types": "error,exception"}, console)
        _, error = contract.prepare_call(
            "unity_delete_script",
            {"path": "Assets/Scripts/OldSample.cs"},
        )
        self.assertIn("did not explicitly scope", error)

    def test_explicit_simple_keys_require_direct_keyboard_script(self):
        contract = TaskContract.from_request(
            "New Input System으로 A/D 이동과 Space 점프를 구현해줘"
        )
        event_script = {
            "path": "Assets/Scripts/PlayerMovement.cs",
            "content": (
                "using UnityEngine.InputSystem; "
                "class PlayerMovement { void OnMove(InputValue value) {} void OnJump() {} }"
            ),
        }
        _, error = contract.prepare_call("unity_write_script", event_script)
        self.assertIn("Policy blocked event-only input script", error)
        self.assertIn("aKey, dKey, spaceKey", error)

        direct_script = {
            "path": "Assets/Scripts/PlayerMovement.cs",
            "content": (
                "using UnityEngine.InputSystem; class PlayerMovement { "
                "bool jumpRequested; Collider collider; Rigidbody rb; void Update() { "
                "var keyboard = Keyboard.current; var a = keyboard.aKey; "
                "var d = keyboard.dKey; if (keyboard.spaceKey.isPressed) "
                "jumpRequested = true; } void FixedUpdate() { "
                "rb.linearVelocity = new Vector3(0, rb.linearVelocity.y, 0); "
                "var bottom = collider.bounds.min; jumpRequested = false; } }"
            ),
        }
        _, error = contract.prepare_call("unity_write_script", direct_script)
        self.assertIsNone(error)

    def test_direct_keyboard_jump_rejects_fixedupdate_only_edge_read(self):
        contract = TaskContract.from_request(
            "A/D 이동과 Space 점프가 실제로 되는지 검증해줘"
        )
        _, error = contract.prepare_call(
            "unity_write_script",
            {
                "path": "Assets/Scripts/PlayerMovement.cs",
                "content": (
                    "using UnityEngine.InputSystem; class PlayerMovement { "
                    "Rigidbody rb; Collider collider; void Update() {} "
                    "void FixedUpdate() { if (Keyboard.current.spaceKey.wasPressedThisFrame) {} "
                    "if (Keyboard.current.aKey.isPressed || Keyboard.current.dKey.isPressed) {} "
                    "rb.linearVelocity = Vector3.zero; var bottom = collider.bounds.min; } }"
                ),
            },
        )
        self.assertIn("Update jumpRequested latch", error)

    def test_direct_keyboard_jump_accepts_collision_contact_grounding(self):
        contract = TaskContract.from_request(
            "A/D 이동과 Space 점프가 실제로 되는지 검증해줘"
        )
        _, error = contract.prepare_call(
            "unity_write_script",
            {
                "path": "Assets/Scripts/PlayerMovement.cs",
                "content": (
                    "using UnityEngine.InputSystem;\n"
                    "class PlayerMovement {\n"
                    "bool jumpRequested; Rigidbody rb;\n"
                    "void Update() { var k = Keyboard.current; var a = k.aKey; "
                    "var d = k.dKey; if (k.spaceKey.isPressed) jumpRequested = true; }\n"
                    "void FixedUpdate() { rb.linearVelocity = Vector3.zero; "
                    "jumpRequested = false; }\n"
                    "void OnCollisionStay(Collision collision) { foreach "
                    "(ContactPoint contact in collision.contacts) { "
                    "if (contact.normal.y > 0.5f) {} } }\n"
                    "}"
                ),
            },
        )
        self.assertIsNone(error)

    def test_direct_keyboard_jump_normalizes_short_edge_read(self):
        contract = TaskContract.from_request(
            "A/D 이동과 Space 점프가 실제로 되는지 검증해줘"
        )
        prepared, error = contract.prepare_call(
            "unity_write_script",
            {
                "path": "Assets/Scripts/PlayerMovement.cs",
                "content": (
                    "using UnityEngine.InputSystem;\n"
                    "class PlayerMovement {\n"
                    "bool jumpRequested; Rigidbody rb;\n"
                    "void Update() { var k = Keyboard.current; var a = k.aKey; "
                    "var d = k.dKey; if (k.spaceKey.wasPressedThisFrame) "
                    "jumpRequested = true; }\n"
                    "void FixedUpdate() { rb.linearVelocity = Vector3.zero; "
                    "jumpRequested = false; }\n"
                    "void OnCollisionEnter(Collision collision) { "
                    "if (collision.gameObject.CompareTag(\"Ground\")) "
                    "{ isGrounded = true; } }\n"
                    "}"
                ),
            },
        )
        self.assertIsNone(error)
        self.assertIn("spaceKey.isPressed", prepared["content"])
        self.assertNotIn("spaceKey.wasPressedThisFrame", prepared["content"])
        self.assertIn("collision.gameObject != gameObject", prepared["content"])
        self.assertNotIn('CompareTag("Ground")', prepared["content"])

    def test_direct_keyboard_script_rejects_legacy_input_api_mixing(self):
        contract = TaskContract.from_request(
            "A/D 이동과 Space 점프가 실제로 되는지 검증해줘"
        )
        _, error = contract.prepare_call(
            "unity_write_script",
            {
                "path": "Assets/Scripts/PlayerMovement.cs",
                "content": (
                    "using UnityEngine.InputSystem;\n"
                    "class PlayerMovement {\n"
                    "bool jumpRequested; Rigidbody rb; Collider collider;\n"
                    "void Update() { var k = Keyboard.current; var a = k.aKey; "
                    "var d = k.dKey; var old = Input.GetAxis(\"Horizontal\"); "
                    "if (k.spaceKey.isPressed) jumpRequested = true; }\n"
                    "void FixedUpdate() { rb.linearVelocity = Vector3.zero; "
                    "var bottom = collider.bounds.min; jumpRequested = false; }\n"
                    "}"
                ),
            },
        )
        self.assertIn("no legacy UnityEngine.Input API", error)

    def test_fresh_keyboard_scene_must_write_custom_movement_before_attach(self):
        contract = TaskContract.from_request(
            "Assets/Scenes/Fresh.unity에 새 빈 씬을 만들고 A/D 이동과 Space 점프를 구현해줘"
        )
        _, error = contract.prepare_call(
            "unity_add_component",
            {"target": "Player", "component_type": "PlayerMovement"},
        )
        self.assertIn("Policy blocked reuse of PlayerMovement", error)

        write_args = {
            "path": "Assets/Scripts/PlayerMovement.cs",
            "content": (
                "using UnityEngine.InputSystem; class PlayerMovement { "
                "bool jumpRequested; Rigidbody rb; Collider collider; void Update() { "
                "var k = Keyboard.current; var a = k.aKey; var d = k.dKey; "
                "if (k.spaceKey.isPressed) jumpRequested = true; } "
                "void FixedUpdate() { rb.linearVelocity = Vector3.zero; "
                "var bottom = collider.bounds.min; jumpRequested = false; } }"
            ),
        }
        write_args, error = contract.prepare_call("unity_write_script", write_args)
        self.assertIsNone(error)
        contract.observe("unity_write_script", write_args, OK)
        _, error = contract.prepare_call(
            "unity_add_component",
            {"target": "Player", "component_type": "PlayerMovement"},
        )
        self.assertIsNone(error)

    def test_behaviour_verification_request_requires_builder_input_measurement(self):
        contract = TaskContract.from_request(
            "이동과 점프가 실제로 되는지 검증까지 끝내줘"
        )
        self.assertTrue(contract.require_play)
        self.assertTrue(contract.require_input_sim)

    def test_explicit_simple_keys_block_unrequested_playerinput_component(self):
        contract = TaskContract.from_request("A/D 이동과 Space 점프를 구현해줘")
        _, error = contract.prepare_call(
            "unity_add_component",
            {"target": "Player", "component_type": "UnityEngine.InputSystem.PlayerInput"},
        )
        self.assertIn("Policy blocked PlayerInput", error)

    def test_explicit_playerinput_architecture_is_not_forced_to_direct_reads(self):
        contract = TaskContract.from_request(
            "PlayerInput과 InputAction 콜백으로 A/D 이동과 Space 점프를 구현해줘"
        )
        _, error = contract.prepare_call(
            "unity_write_script",
            {
                "path": "Assets/Scripts/PlayerMovement.cs",
                "content": "class PlayerMovement { void OnMove() {} void OnJump() {} }",
            },
        )
        self.assertIsNone(error)
        _, error = contract.prepare_call(
            "unity_add_component",
            {"target": "Player", "component_type": "PlayerInput"},
        )
        self.assertIsNone(error)

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
    def test_unrequested_level_workflow_is_blocked(self):
        contract = TaskContract.from_request(
            "새 씬에 Player와 바닥 플랫폼을 만들어 줘"
        )
        for name, args in (
            ("unity_install_level_loader", {}),
            (
                "unity_write_level",
                {"path": "Assets/StreamingAssets/Levels/level1.json"},
            ),
            (
                "unity_read_level",
                {"path": "Assets/StreamingAssets/Levels/level1.json"},
            ),
        ):
            with self.subTest(name=name):
                _, error = contract.prepare_call(name, args)
                self.assertIn("did not request a level/stage", error)

    def test_explicit_level_workflow_allows_loader(self):
        contract = TaskContract.from_request("레벨 1과 LevelLoader를 만들어 줘")
        _, error = contract.prepare_call("unity_install_level_loader", {})
        self.assertIsNone(error)

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
