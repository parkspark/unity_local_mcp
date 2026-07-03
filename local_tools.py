"""호스트에서 직접 실행되는 로컬 도구 (Unity를 거치지 않는 파일 I/O).

Unity 프로젝트의 Assets/ 아래 C# 스크립트를 읽고 쓴다.
쓰기 후 컴파일은 기존 unity_refresh_assets 도구가 담당한다.
"""

import json
import os

NAMES = {"unity_write_script", "unity_read_script"}

SCHEMAS = [
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
]


def _error(msg: str) -> str:
    return json.dumps({"status": "error", "error": msg}, ensure_ascii=False)


def _resolve(project_dir: str, path: str) -> str:
    """경로 가드: Assets/ 아래의 .cs 파일만 허용. 위반 시 ValueError."""
    norm = path.replace("\\", "/").strip().lstrip("/")
    parts = norm.split("/")
    if parts[0] != "Assets":
        raise ValueError(f'path must start with "Assets/", got: {path}')
    if ".." in parts or "." in parts[:-1]:
        raise ValueError(f"path must not contain '..': {path}")
    if not norm.lower().endswith(".cs"):
        raise ValueError(f"only .cs files are allowed, got: {path}")
    abs_path = os.path.normpath(os.path.join(project_dir, norm))
    assets_root = os.path.normpath(os.path.join(project_dir, "Assets"))
    if not abs_path.startswith(assets_root + os.sep):
        raise ValueError(f"path escapes the Assets folder: {path}")
    return abs_path


def call(name: str, args: dict, project_dir: str) -> str:
    try:
        abs_path = _resolve(project_dir, str(args.get("path", "")))
    except ValueError as e:
        return _error(str(e))

    if name == "unity_write_script":
        content = args.get("content")
        if not isinstance(content, str) or not content.strip():
            return _error("content must be a non-empty string of C# source code")
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return json.dumps({
            "status": "ok",
            "result": {
                "written": abs_path,
                "bytes": len(content.encode("utf-8")),
                "next_step": "call unity_refresh_assets to compile, then unity_read_console types=\"error\"",
            },
        }, ensure_ascii=False)

    if name == "unity_read_script":
        if not os.path.exists(abs_path):
            return _error(f"file not found: {abs_path}")
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()

    return _error(f"unknown local tool: {name}")
