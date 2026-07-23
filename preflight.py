"""Deterministic request preflight before any Unity mutation is allowed."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


ASSET_PATH = re.compile(
    r"Assets/[^\r\n]*?\.(?:cs|unity|prefab|mat|json)(?![A-Za-z0-9_.-])", re.I
)
ACCEPTANCE_SECTION = re.compile(
    r"(?:\[Play Mode 합격 조건\]|\[합격 조건\]|합격 조건)(.*?)(?=\n\s*\[[^\]]+\]|\Z)",
    re.I | re.S,
)


def normalise_asset_path(value: str) -> str:
    return str(value or "").replace("\\", "/").lstrip("/")


def extract_asset_paths(request: str) -> list[str]:
    return sorted({normalise_asset_path(match.group(0)) for match in ASSET_PATH.finditer(request)})


@dataclass(frozen=True)
class PreflightIssue:
    code: str
    message: str
    blocking: bool = True


@dataclass
class PreflightResult:
    normalized_request: str = ""
    asset_paths: list[str] = field(default_factory=list)
    scene_paths: list[str] = field(default_factory=list)
    canonical_scene_path: str | None = None
    issues: list[PreflightIssue] = field(default_factory=list)

    @property
    def blocking_issues(self) -> list[PreflightIssue]:
        return [issue for issue in self.issues if issue.blocking]

    @property
    def allowed(self) -> bool:
        return not self.blocking_issues


def inspect_request(request: str, scene_path_policy: str = "strict") -> PreflightResult:
    """Resolve request assets and reject contradictory final scene paths.

    ``strict`` refuses to guess. ``acceptance`` deterministically treats the one
    scene path in the acceptance section as canonical and records a warning.
    """
    policy = str(scene_path_policy or "strict").strip().lower()
    if policy not in {"strict", "acceptance"}:
        raise ValueError("scene_path_policy must be 'strict' or 'acceptance'")

    assets = extract_asset_paths(request)
    scenes = [path for path in assets if path.lower().endswith(".unity")]
    result = PreflightResult(
        normalized_request=request, asset_paths=assets, scene_paths=scenes
    )
    if not scenes:
        return result
    if len(scenes) == 1:
        result.canonical_scene_path = scenes[0]
        return result

    acceptance_scenes: list[str] = []
    for section in ACCEPTANCE_SECTION.findall(request):
        acceptance_scenes.extend(
            path for path in extract_asset_paths(section) if path.lower().endswith(".unity")
        )
    acceptance_scenes = sorted(set(acceptance_scenes))
    joined = ", ".join(scenes)
    if policy == "acceptance" and len(acceptance_scenes) == 1:
        result.canonical_scene_path = acceptance_scenes[0]
        for scene_path in scenes:
            if scene_path != result.canonical_scene_path:
                result.normalized_request = result.normalized_request.replace(
                    scene_path, result.canonical_scene_path
                )
        result.asset_paths = [
            path for path in assets
            if not path.lower().endswith(".unity") or path == result.canonical_scene_path
        ]
        result.issues.append(PreflightIssue(
            code="scene_path_conflict_resolved",
            message=(
                f"서로 다른 씬 경로({joined}) 중 합격 조건의 "
                f"{result.canonical_scene_path}을 최종 경로로 선택했습니다."
            ),
            blocking=False,
        ))
        return result

    result.issues.append(PreflightIssue(
        code="conflicting_scene_paths",
        message=(
            f"요청에 서로 다른 최종 씬 경로가 있습니다: {joined}. "
            "Unity를 변경하기 전에 하나의 최종 경로로 정리해야 합니다."
        ),
    ))
    return result
