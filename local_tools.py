"""호스트에서 직접 실행되는 로컬 도구 (Unity를 거치지 않는 파일 I/O).

Unity 프로젝트의 Assets/ 아래 C# 스크립트를 읽고 쓴다.
쓰기 후 컴파일은 기존 unity_refresh_assets 도구가 담당한다.
"""

import json
import os
import re

NAMES = {"unity_write_script", "unity_read_script", "unity_delete_script"}

# 브리지 자기 자신은 삭제 금지 — 제어 채널이 끊긴다.
_PROTECTED_PREFIX = "Assets/Editor/McpBridge/"

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
