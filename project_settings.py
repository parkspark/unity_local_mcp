"""Unity 프로젝트 선택과 최근 프로젝트 로컬 설정 관리."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


SETTINGS_PATH = Path(__file__).with_name(".unity-local-agent.json")


@dataclass(frozen=True)
class ProjectSelection:
    path: str
    source: str
    warning: str | None = None


def _normalise(path: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
    return Path(expanded).resolve()


def _validation_error(path: Path) -> str | None:
    if not path.is_dir():
        return f"프로젝트 폴더가 없습니다: {path}"
    missing = [name for name in ("Assets", "ProjectSettings") if not (path / name).is_dir()]
    if missing:
        return f"Unity 프로젝트가 아닙니다({', '.join(missing)} 폴더 없음): {path}"
    return None


def _read_recent(settings_path: Path) -> tuple[str | None, str | None]:
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"최근 프로젝트 설정을 읽지 못했습니다: {exc}"
    path = data.get("last_project") if isinstance(data, dict) else None
    return (path if isinstance(path, str) and path.strip() else None), None


def remember_project(path: str, settings_path: Path = SETTINGS_PATH) -> str | None:
    """최근 프로젝트를 저장한다. 저장 실패는 연결을 막지 않고 경고로 반환한다."""
    try:
        settings_path.write_text(
            json.dumps({"last_project": path}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return f"최근 프로젝트를 저장하지 못했습니다: {exc}"
    return None


def select_project(
    cli_project: str | None,
    default_project: str,
    *,
    environ: Mapping[str, str] | None = None,
    settings_path: Path = SETTINGS_PATH,
) -> ProjectSelection:
    """CLI > 환경변수 > 최근 설정 > 기존 기본값 순서로 프로젝트를 선택한다."""
    env = os.environ if environ is None else environ
    env_project = env.get("UNITY_PROJECT_DIR", "").strip()

    if cli_project:
        raw, source, must_validate = cli_project, "--project", True
    elif env_project:
        raw, source, must_validate = env_project, "UNITY_PROJECT_DIR", True
    else:
        recent, read_warning = _read_recent(settings_path)
        if recent:
            recent_path = _normalise(recent)
            recent_error = _validation_error(recent_path)
            if recent_error is None:
                return ProjectSelection(str(recent_path), "최근 프로젝트", read_warning)
            read_warning = f"저장된 최근 프로젝트를 사용할 수 없습니다: {recent_error}"
        raw, source, must_validate = default_project, "기본값", False

    path = _normalise(raw)
    error = _validation_error(path)
    if error and must_validate:
        raise ValueError(error)

    warning = error if error else remember_project(str(path), settings_path)
    if not (cli_project or env_project) and read_warning:
        warning = read_warning if warning is None else f"{read_warning}; {warning}"
    return ProjectSelection(str(path), source, warning)
