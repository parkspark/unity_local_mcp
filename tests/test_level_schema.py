import json
import os
import unittest

from level_schema import LEVEL_SCHEMA, validate_level

_TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates", "LevelLoader.cs")


def _level(**overrides):
    base = {
        "version": 1,
        "name": "Level 1",
        "next_level": "level2.json",
        "player_spawn": [0, 1, 0],
        "goal": {"position": [10, 1, 0], "size": [1, 2, 1]},
        "objects": [
            {"type": "platform", "position": [0, 0, 0], "size": [4, 1, 4]},
            {"type": "hazard", "position": [5, 0, 0], "color": [1, 0, 0]},
        ],
    }
    base.update(overrides)
    return base


class LevelSchemaTests(unittest.TestCase):
    def test_valid_level_passes(self):
        data, problems = validate_level(_level())
        self.assertIsNotNone(data)
        self.assertEqual(problems, [])

    def test_valid_level_as_json_string(self):
        data, problems = validate_level(json.dumps(_level()))
        self.assertIsNotNone(data)

    def test_csharp_float_suffix_is_tolerated(self):
        text = json.dumps(_level()).replace("[0, 1, 0]", "[0.0f, 1.0f, 0.0f]")
        data, _ = validate_level(text)
        self.assertIsNotNone(data)
        self.assertEqual(data["player_spawn"], [0.0, 1.0, 0.0])

    def test_missing_player_spawn_fails_with_readable_error(self):
        level = _level()
        del level["player_spawn"]
        data, problems = validate_level(level)
        self.assertIsNone(data)
        self.assertTrue(any("player_spawn" in p for p in problems))

    def test_two_element_vector_fails(self):
        data, problems = validate_level(_level(player_spawn=[1, 2]))
        self.assertIsNone(data)
        self.assertTrue(any("player_spawn" in p for p in problems))

    def test_empty_objects_fails(self):
        data, problems = validate_level(_level(objects=[]))
        self.assertIsNone(data)

    def test_requires_at_least_one_platform(self):
        data, problems = validate_level(
            _level(objects=[{"type": "hazard", "position": [1, 0, 0]}])
        )
        self.assertIsNone(data)
        self.assertTrue(any("platform" in p for p in problems))

    def test_goal_must_differ_from_spawn(self):
        data, problems = validate_level(
            _level(goal={"position": [0, 1, 0]})
        )
        self.assertIsNone(data)

    def test_unknown_object_type_fails(self):
        data, problems = validate_level(
            _level(objects=[{"type": "boss", "position": [0, 0, 0]}])
        )
        self.assertIsNone(data)

    def test_unparseable_text_fails_gracefully(self):
        data, problems = validate_level("not json at all {")
        self.assertIsNone(data)
        self.assertTrue(problems)

    def test_template_loader_matches_schema_field_names(self):
        """Python 스키마 ↔ C# JsonUtility 필드 정합성 잠금."""
        with open(_TEMPLATE, encoding="utf-8") as f:
            template = f.read()
        top_level = set(LEVEL_SCHEMA["properties"])
        object_fields = set(
            LEVEL_SCHEMA["properties"]["objects"]["items"]["properties"]
        )
        goal_fields = set(LEVEL_SCHEMA["properties"]["goal"]["properties"])
        for field in top_level | object_fields | goal_fields:
            self.assertIn(field, template, f"LevelLoader.cs is missing schema field '{field}'")
        # 결정적 콘솔 마커도 잠근다 (호스트/프롬프트가 이 문자열에 의존)
        for marker in ("[LevelLoader] Loaded", "[LevelLoader] GOAL reached",
                       "[LevelLoader] ALL LEVELS CLEAR"):
            self.assertIn(marker, template)


if __name__ == "__main__":
    unittest.main()
