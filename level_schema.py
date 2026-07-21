"""데이터 주도 레벨의 결정적 스키마 검증.

모델이 제안한 레벨 JSON을 호스트가 jsonschema로 검증한다. 오류는 사람이 읽을
문장 리스트로 반환되어 그대로 모델 피드백이 된다. 필드명은 templates/LevelLoader.cs
의 [Serializable] 클래스 필드와 문자 그대로 일치해야 한다(JsonUtility 매칭) —
tests/test_level_schema.py가 이 정합성을 정적으로 검사한다.
"""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

_VEC3 = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
_COLOR = {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
          "minItems": 3, "maxItems": 4}

LEVEL_SCHEMA = {
    "type": "object",
    "required": ["version", "name", "player_spawn", "goal", "objects"],
    "additionalProperties": False,
    "properties": {
        "version": {"const": 1},
        "name": {"type": "string", "minLength": 1, "maxLength": 80},
        # 같은 Levels/ 폴더 안의 다음 레벨 파일명. null 또는 생략이면 마지막 레벨.
        "next_level": {"type": ["string", "null"], "pattern": r"^[\w.-]+\.json$"},
        "player_spawn": _VEC3,
        "goal": {
            "type": "object",
            "required": ["position"],
            "additionalProperties": False,
            "properties": {"position": _VEC3, "size": _VEC3},
        },
        "objects": {
            "type": "array",
            "minItems": 1,
            "maxItems": 200,
            "items": {
                "type": "object",
                "required": ["type", "position"],
                "additionalProperties": False,
                "properties": {
                    "type": {"enum": ["platform", "hazard", "decoration"]},
                    "name": {"type": "string", "maxLength": 60},
                    "position": _VEC3,
                    "size": _VEC3,
                    "color": _COLOR,
                },
            },
        },
    },
}

_VALIDATOR = Draft202012Validator(LEVEL_SCHEMA)
_MAX_ERRORS = 5


def _lenient_json(text: str):
    """mcp_client._lenient_json과 동일한 관용 파싱 (C# float 접미사 등)."""
    from mcp_client import _lenient_json as parse
    return parse(text)


def validate_level(data) -> tuple[dict | None, list[str]]:
    """레벨 데이터(str 또는 dict)를 검증한다.

    반환: (정규화된 dict 또는 None, 오류/경고 문장 리스트).
    dict가 None이 아니면 통과이며, 리스트에는 경고만 남는다.
    """
    if isinstance(data, str):
        try:
            data = _lenient_json(data)
        except Exception as e:
            return None, [f"level JSON does not parse: {e}"]
    if not isinstance(data, dict):
        return None, ["level data must be a JSON object"]

    errors = []
    for err in _VALIDATOR.iter_errors(data):
        where = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{where}: {err.message}")
        if len(errors) >= _MAX_ERRORS:
            break
    if errors:
        return None, errors

    # 스키마로 못 잡는 의미 검사
    warnings: list[str] = []
    platforms = [o for o in data["objects"] if o["type"] == "platform"]
    if not platforms:
        return None, ["objects must contain at least one platform (the player needs ground)"]
    if data["player_spawn"] == data["goal"]["position"]:
        return None, ["goal.position must differ from player_spawn"]
    return data, warnings
