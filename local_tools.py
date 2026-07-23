"""호스트에서 직접 실행되는 로컬 도구 (Unity를 거치지 않는 파일 I/O).

Unity 프로젝트의 Assets/ 아래 C# 스크립트를 읽고 쓴다.
쓰기 후 컴파일은 기존 unity_refresh_assets 도구가 담당한다.
"""

import json
import os
import re

NAMES = {
    "unity_write_script", "unity_read_script", "unity_delete_script", "unity_wait",
    "unity_write_level", "unity_read_level", "unity_install_level_loader",
}

# 브리지 자기 자신은 삭제 금지 — 제어 채널이 끊긴다.
_PROTECTED_PREFIX = "Assets/Editor/McpBridge/"

# 데이터 주도 레벨 JSON의 고정 위치. StreamingAssets는 컴파일을 유발하지 않아
# 레벨 수정 후 refresh 없이 바로 플레이 검증이 가능하다.
LEVELS_PREFIX = "Assets/StreamingAssets/Levels/"
# 호스트 저장소의 canonical LevelLoader 템플릿과 설치 위치
_TEMPLATE_LOADER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "LevelLoader.cs")
LOADER_SCRIPT_PATH = "Assets/Scripts/LevelLoader.cs"

# Unity 6에서 개명된 구식 API. 그대로 두면 API Updater가 에디터를 막는
# "Script Updating Consent" 모달을 띄우므로 쓰기 전에 교정한다.
# 오탐을 피하기 위해 가장 흔하고 모호하지 않은 패턴만 다룬다.
_OBSOLETE_FIXES = [
    (re.compile(r"\.velocity\b"), ".linearVelocity", "velocity→linearVelocity"),
]


def _sanitize(content: str) -> tuple[str, list[str]]:
    fixed = []
    for pattern, replacement, label in _OBSOLETE_FIXES:
        content, n = pattern.subn(replacement, content)
        if n:
            fixed.append(label)
    return content, fixed

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "unity_wait",
            "description": (
                "Wait briefly without sending a Unity command. Use this after entering play mode "
                "so Start/Awake/runtime errors have time to occur before unity_read_console. "
                "Allowed range is 0.05 to 10 seconds."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "number", "description": "Seconds to wait (0.05..10)."},
                },
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unity_write_script",
            "description": (
                "Create or overwrite a C# script file inside the Unity project. "
                "The C# class name MUST match the file name (e.g. Assets/Scripts/PlayerMovement.cs "
                "must contain 'public class PlayerMovement'). "
                "After writing, call unity_refresh_assets to compile, then check "
                "unity_read_console with types=\"error\" for compile errors."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Project-relative path starting with Assets/, ending with .cs, e.g. \"Assets/Scripts/PlayerMovement.cs\"",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full C# source code of the file",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unity_read_script",
            "description": "Read an existing C# script file from the Unity project. Use this before modifying a script.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Project-relative path starting with Assets/, ending with .cs",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unity_install_level_loader",
            "description": (
                "Install the canonical LevelLoader.cs script (data-driven level builder) at "
                "Assets/Scripts/LevelLoader.cs. Always use this instead of writing your own loader. "
                "After installing, call unity_refresh_assets to compile, then attach the LevelLoader "
                "component to an empty GameObject and set its levelFile property. "
                "The loader reads Assets/StreamingAssets/Levels/*.json at runtime, builds platforms/"
                "hazards/goal, moves the GameObject named 'Player' to player_spawn, and logs "
                "'[LevelLoader] Loaded ...' to the console."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unity_write_level",
            "description": (
                "Create or overwrite a level data JSON file under Assets/StreamingAssets/Levels/. "
                "The host validates it against the level schema and rejects invalid data with "
                "specific errors. Levels do NOT need unity_refresh_assets — after writing, verify "
                "by entering play mode and checking the console for '[LevelLoader] Loaded ...'. "
                "Schema: {\"version\": 1, \"name\": str, \"next_level\": \"level2.json\"|null, "
                "\"player_spawn\": [x,y,z], \"goal\": {\"position\": [x,y,z], \"size\": [x,y,z]?}, "
                "\"objects\": [{\"type\": \"platform\"|\"hazard\"|\"decoration\", \"name\": str?, "
                "\"position\": [x,y,z], \"size\": [x,y,z]?, \"color\": [r,g,b]?}, ...]}. "
                "At least one platform is required."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Project-relative path like \"Assets/StreamingAssets/Levels/level1.json\"",
                    },
                    "content": {
                        "type": "string",
                        "description": "Level data as a JSON string matching the level schema",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unity_read_level",
            "description": "Read an existing level JSON file from Assets/StreamingAssets/Levels/. Use before modifying a level.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Project-relative path like \"Assets/StreamingAssets/Levels/level1.json\"",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unity_delete_script",
            "description": (
                "Delete a C# script file from the Unity project (removes the .cs and its .meta). "
                "Use this to remove a stale, duplicate, or conflicting script that breaks compilation "
                "(e.g. two files defining the same class). "
                "After deleting, call unity_refresh_assets to recompile."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Project-relative path starting with Assets/, ending with .cs, e.g. \"Assets/Scripts/TetrominoBlock.cs\"",
                    },
                },
                "required": ["path"],
            },
        },
    },
]


def _error(msg: str) -> str:
    return json.dumps({"status": "error", "error": msg}, ensure_ascii=False)


def wait_seconds(args: dict) -> float:
    """Validate a bounded host-side wait used for runtime checks."""
    try:
        seconds = float(args.get("seconds"))
    except (TypeError, ValueError):
        raise ValueError("seconds must be a number between 0.05 and 10")
    if not 0.05 <= seconds <= 10:
        raise ValueError("seconds must be between 0.05 and 10")
    return seconds


def _resolve(project_dir: str, path: str, suffix: str = ".cs", required_prefix: str | None = None) -> str:
    """경로 가드: Assets/ 아래의 지정 확장자 파일만 허용. 위반 시 ValueError."""
    norm = path.replace("\\", "/").strip().lstrip("/")
    parts = norm.split("/")
    if parts[0] != "Assets":
        raise ValueError(f'path must start with "Assets/", got: {path}')
    if ".." in parts or "." in parts[:-1]:
        raise ValueError(f"path must not contain '..': {path}")
    if not norm.lower().endswith(suffix):
        raise ValueError(f"only {suffix} files are allowed, got: {path}")
    if required_prefix and not norm.startswith(required_prefix):
        raise ValueError(f"path must be under {required_prefix}, got: {path}")
    abs_path = os.path.normpath(os.path.join(project_dir, norm))
    assets_root = os.path.normpath(os.path.join(project_dir, "Assets"))
    if not abs_path.startswith(assets_root + os.sep):
        raise ValueError(f"path escapes the Assets folder: {path}")
    return abs_path


def _install_level_loader(project_dir: str) -> str:
    """호스트의 canonical LevelLoader 템플릿을 Assets/Scripts/에 설치(항상 덮어씀)."""
    if not os.path.exists(_TEMPLATE_LOADER):
        return _error(f"host template missing: {_TEMPLATE_LOADER}")
    with open(_TEMPLATE_LOADER, "r", encoding="utf-8") as f:
        content = f.read()
    abs_path = _resolve(project_dir, LOADER_SCRIPT_PATH)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)
    return json.dumps({
        "status": "ok",
        "result": {
            "written": LOADER_SCRIPT_PATH,
            "next_step": (
                "call unity_refresh_assets to compile, check unity_read_console types=\"error\", "
                "then create an empty GameObject, unity_add_component LevelLoader, and set its "
                "levelFile property (unity_set_component_property) to your level file name."
            ),
        },
    }, ensure_ascii=False)


def _write_level(project_dir: str, args: dict) -> str:
    import level_schema
    try:
        abs_path = _resolve(project_dir, str(args.get("path", "")), suffix=".json",
                            required_prefix=LEVELS_PREFIX)
    except ValueError as e:
        return _error(str(e))
    content = args.get("content")
    if isinstance(content, (dict, list)):
        data, problems = level_schema.validate_level(content)
    elif isinstance(content, str) and content.strip():
        data, problems = level_schema.validate_level(content)
    else:
        return _error("content must be a JSON string (or object) matching the level schema")
    if data is None:
        return _error("level validation failed: " + "; ".join(problems))

    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    objects = data["objects"]
    result = {
        "written": abs_path,
        "level_name": data["name"],
        "platforms": sum(1 for o in objects if o["type"] == "platform"),
        "hazards": sum(1 for o in objects if o["type"] == "hazard"),
        "next_level": data.get("next_level"),
        "next_step": (
            "no recompile needed — verify at runtime: unity_play_mode action=\"play\", "
            "unity_wait, then unity_read_console and look for '[LevelLoader] Loaded ...'"
        ),
    }
    if problems:
        result["warnings"] = problems
    return json.dumps({"status": "ok", "result": result}, ensure_ascii=False)


def call(name: str, args: dict, project_dir: str) -> str:
    if name == "unity_install_level_loader":
        return _install_level_loader(project_dir)

    if name == "unity_write_level":
        return _write_level(project_dir, args)

    if name == "unity_read_level":
        try:
            abs_path = _resolve(project_dir, str(args.get("path", "")), suffix=".json",
                                required_prefix=LEVELS_PREFIX)
        except ValueError as e:
            return _error(str(e))
        if not os.path.exists(abs_path):
            return _error(f"file not found: {abs_path}")
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()

    try:
        abs_path = _resolve(project_dir, str(args.get("path", "")))
    except ValueError as e:
        return _error(str(e))

    if name == "unity_write_script":
        content = args.get("content")
        if not isinstance(content, str) or not content.strip():
            return _error("content must be a non-empty string of C# source code")
        content, auto_fixed = _sanitize(content)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        result = {
            "written": abs_path,
            "bytes": len(content.encode("utf-8")),
            "next_step": "call unity_refresh_assets to compile, then unity_read_console types=\"error\"",
        }
        if auto_fixed:
            result["auto_fixed"] = auto_fixed
        return json.dumps({"status": "ok", "result": result}, ensure_ascii=False)

    if name == "unity_read_script":
        if not os.path.exists(abs_path):
            return _error(f"file not found: {abs_path}")
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()

    if name == "unity_delete_script":
        norm = str(args.get("path", "")).replace("\\", "/").strip().lstrip("/")
        if norm.startswith(_PROTECTED_PREFIX):
            return _error(
                f"refusing to delete the MCP bridge itself ({norm}) — it is the control channel"
            )
        if not os.path.exists(abs_path):
            return _error(f"file not found: {abs_path}")
        os.remove(abs_path)
        meta_path = abs_path + ".meta"
        meta_deleted = False
        if os.path.exists(meta_path):
            os.remove(meta_path)
            meta_deleted = True
        return json.dumps({
            "status": "ok",
            "result": {
                "deleted": abs_path,
                "meta_deleted": meta_deleted,
                "next_step": "call unity_refresh_assets to recompile",
            },
        }, ensure_ascii=False)

    return _error(f"unknown local tool: {name}")
