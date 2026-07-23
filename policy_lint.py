"""Request-scoped C# policy checks that run before Play Mode."""

from __future__ import annotations

import os
import re
import tempfile


CLASS_DECLARATION = re.compile(r"\b(?:public\s+)?class\s+([A-Za-z_]\w*)")
COMPARE_TAG = re.compile(r'\bCompareTag\s*\(\s*"([^"]+)"\s*\)')
LEGACY_INPUT = re.compile(
    r"\b(?:UnityEngine\.)?Input\.(?:Get|GetAxis|GetButton|GetKey|mouse|touch|anyKey)"
)


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\r\n]*", "", text)


def _defined_tags(project_dir: str) -> set[str]:
    tags = {"Untagged", "Respawn", "Finish", "EditorOnly", "MainCamera", "Player", "GameController"}
    path = os.path.join(project_dir, "ProjectSettings", "TagManager.asset")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return tags
    in_tags = False
    for line in text.splitlines():
        if line.strip() == "tags:":
            in_tags = True
            continue
        if in_tags and re.match(r"^\S", line):
            break
        match = re.match(r"^\s*-\s+(.+?)\s*$", line)
        if in_tags and match:
            tags.add(match.group(1).strip('"'))
    return tags


def lint_scripts(request: str, asset_paths: list[str], project_dir: str) -> list[str]:
    lower = request.lower()
    violations: list[str] = []
    defined_tags = _defined_tags(project_dir)
    for relative in asset_paths:
        if not relative.lower().endswith(".cs"):
            continue
        absolute = os.path.join(project_dir, relative)
        try:
            with open(absolute, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        code = _strip_comments(text)
        filename = os.path.splitext(os.path.basename(relative))[0]
        classes = CLASS_DECLARATION.findall(code)
        if filename not in classes:
            violations.append(f"class_filename_mismatch:{relative}:{filename}")
        if "legacy unityengine.input api 사용 금지" in lower and LEGACY_INPUT.search(code):
            violations.append(f"legacy_input_api:{relative}")
        if "keyboard.current" in lower and "PlayerMovement" in filename:
            if "Keyboard.current" not in code:
                violations.append(f"keyboard_current_missing:{relative}")
            elif not re.search(r"Keyboard\.current\s*(?:==\s*null|is\s+null)", code):
                violations.append(f"keyboard_null_check_missing:{relative}")
        if "rigidbody.linearvelocity" in lower and "PlayerMovement" in filename:
            if ".linearVelocity" not in code:
                violations.append(f"linear_velocity_missing:{relative}")
        if 'comparetag("ground")' in lower and "PlayerMovement" in filename:
            if re.search(r'CompareTag\s*\(\s*"Ground"\s*\)', code):
                violations.append(f"ground_compare_tag_forbidden:{relative}")
        for tag in COMPARE_TAG.findall(code):
            if tag not in defined_tags:
                violations.append(f"undefined_compare_tag:{relative}:{tag}")
        if "낙사 시 시작 위치로 복귀" in lower and "PlayerMovement" in filename:
            if "spawnPosition" not in code or not re.search(
                r"(?:transform\.position\.y|transform\.position\s*\.\s*y)", code
            ):
                violations.append(f"fall_respawn_check_missing:{relative}")
        if "무입력 0.5초" in lower and "PlayerMovement" in filename:
            writes_zero_horizontal_velocity = re.search(
                r"\.linearVelocity\s*=\s*new\s+Vector3\s*\(\s*0(?:f)?\s*,",
                code,
            )
            if (
                re.search(r"if\s*\(\s*moveX\s*!=\s*0", code)
                and not writes_zero_horizontal_velocity
            ):
                violations.append(f"idle_velocity_not_zeroed:{relative}")
        if "SideScrollerCamera" in filename:
            # Adding a negative offset to the camera's current Z every frame
            # makes the camera drift away indefinitely instead of preserving a
            # fixed side-scroller depth.
            if re.search(
                r"new\s+Vector3\s*\([^;]*(?:transform\.position\.z|fixedZ)"
                r"\s*\)\s*\+\s*offset",
                code,
                flags=re.S | re.I,
            ):
                violations.append(f"camera_z_accumulates_offset:{relative}")
    return sorted(set(violations))


def apply_safe_repairs(failures: list[str], project_dir: str) -> list[str]:
    """Apply exact, request-scoped repairs for deterministic lint findings."""
    changed: list[str] = []
    prefix = "policy_lint:camera_z_accumulates_offset:"
    project_root = os.path.abspath(project_dir)
    for failure in failures:
        if not failure.startswith(prefix):
            continue
        relative = failure[len(prefix):].replace("\\", "/")
        absolute = os.path.abspath(os.path.join(project_root, relative))
        if (
            os.path.commonpath([absolute, project_root]) != project_root
            or not relative.startswith("Assets/")
            or not relative.lower().endswith(".cs")
        ):
            continue
        try:
            with open(absolute, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        pattern = re.compile(
            r"new\s+Vector3\s*\(\s*([^,\r\n]+),\s*([^,\r\n]+),"
            r"\s*fixedZ\s*\)\s*\+\s*offset"
        )
        repaired, count = pattern.subn(
            lambda match: (
                f"new Vector3({match.group(1).strip()} + offset.x, "
                f"{match.group(2).strip()} + offset.y, fixedZ)"
            ),
            text,
        )
        if not count or repaired == text:
            continue
        directory = os.path.dirname(absolute)
        fd, temporary = tempfile.mkstemp(
            prefix=".mcp-repair-", suffix=".cs", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(repaired)
            os.replace(temporary, absolute)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        changed.append(relative)
    return sorted(set(changed))
