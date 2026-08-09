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


_KEYBOARD_BINDING = re.compile(
    r"(?:var|Keyboard)\s+(\w+)\s*=\s*Keyboard\s*\.\s*current", re.I
)


def _keyboard_null_checked(code: str) -> bool:
    """`Keyboard.current`의 null 검사가 있는가.

    직접 비교(`Keyboard.current == null`)만 인정하면 **호스트의 다른 규칙과
    충돌한다.** `task_contract`의 교정 스니펫이 가르치는 형태가 정확히 지역
    변수를 거치는 쪽이기 때문이다.

        var keyboard = Keyboard.current;
        if (keyboard == null) return;

    게이트는 A를 가르치고 린트는 B를 요구하니 repair가 수렴할 수 없었다 —
    실측 `20260809_124847`에서 이 위반이 repair 3사이클을 그대로 통과해
    Play Mode 측정이 전부 blocked됐다.
    """
    if re.search(r"Keyboard\s*\.\s*current\s*(?:==\s*null|is\s+null)", code):
        return True
    for name in set(_KEYBOARD_BINDING.findall(code)):
        if re.search(
            rf"\b{re.escape(name)}\b\s*(?:==|!=)\s*null|\b{re.escape(name)}\b\s+is\s+"
            rf"(?:not\s+)?null",
            code,
        ):
            return True
    return False


_VELOCITY_WRITEBACK = re.compile(r"\.\s*linearVelocity\s*=\s*(\w+)\s*;")


def _zeroes_horizontal_when_idle(code: str) -> bool:
    """입력이 없을 때 수평 속도를 0으로 만드는가.

    **철자가 아니라 동작을 본다.** 예전 규칙은 `.linearVelocity = new Vector3(0, ...)`
    한 형태만 인정했는데, 모델은 지역 벡터를 거쳐 쓰는 쪽을 택한다.

        Vector3 velocity = rb.linearVelocity;
        if (moveX == 0) { velocity.x = 0f; }
        rb.linearVelocity = velocity;

    실측 `20260809_132831`에서 모델이 **"to satisfy policy check"라는 주석까지 달아
    가며** 이 코드를 썼는데도 규칙이 거부했고, repair 4사이클이 같은 위반을 반복하며
    Play Mode 측정 11개를 전부 blocked시켰다.
    """
    if re.search(r"\.\s*linearVelocity\s*=\s*new\s+Vector3\s*\(\s*0(?:f)?\s*,", code):
        return True
    if re.search(r"\.\s*linearVelocity\s*=\s*Vector3\s*\.\s*zero", code):
        return True
    # 지역 벡터의 x를 0으로 만든 뒤 되써 넣는 형태
    for name in set(_VELOCITY_WRITEBACK.findall(code)):
        if re.search(rf"\b{re.escape(name)}\s*\.\s*x\s*=\s*0(?:\.0)?f?\s*;", code):
            return True
    return False


_REMEMBERS_START = re.compile(r"(\w+)\s*=\s*transform\s*\.\s*position\b")


def _respawns_on_fall(code: str) -> bool:
    """떨어지면 시작 위치로 되돌리는가.

    **변수 이름이 아니라 동작을 본다.** 예전 규칙은 식별자 `spawnPosition`이 코드에
    문자 그대로 있을 것을 요구했다. 모델이 `startPosition`·`initialPos` 같은 이름을
    쓰면 아무리 옳게 구현해도 통과할 수 없다 — 영수증 재생에서 이 규칙은 **5회 등장
    중 3회가 repair 2사이클 이상을 그대로 통과**한 최대 비수렴 규칙이었다.

    필요한 것은 셋이다: 시작 위치를 어딘가에 기억하고, 높이를 견주고, 그 기억한
    값으로 되돌린다.
    """
    remembered = set(_REMEMBERS_START.findall(code))
    if not remembered:
        return False
    restored = any(
        re.search(rf"transform\s*\.\s*position\s*=\s*{re.escape(name)}\b", code)
        for name in remembered
    )
    if not restored:
        return False
    return bool(re.search(r"\.\s*y\s*<", code) or re.search(r"\.\s*y\s*<=", code))


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
            elif not _keyboard_null_checked(code):
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
            if not _respawns_on_fall(code):
                violations.append(f"fall_respawn_check_missing:{relative}")
        if "무입력 0.5초" in lower and "PlayerMovement" in filename:
            if (
                re.search(r"if\s*\(\s*moveX\s*!=\s*0", code)
                and not _zeroes_horizontal_when_idle(code)
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
