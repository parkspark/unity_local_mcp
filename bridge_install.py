"""Install the Unity Editor bridge into a selected Unity project safely."""

from __future__ import annotations

import shutil
import json
import re
from dataclasses import dataclass
from pathlib import Path


BRIDGE_NAME = "UnityMcpBridge.cs"
REQUIRED_PACKAGES = {
    "com.unity.inputsystem": "1.19.0",
    "com.unity.nuget.newtonsoft-json": "3.2.2",
}


@dataclass(frozen=True)
class BridgeInstallResult:
    status: str
    path: Path | None = None
    message: str | None = None


@dataclass(frozen=True)
class PackageInstallResult:
    status: str
    added: tuple[str, ...] = ()
    message: str | None = None


def _installed_bridge(project_root: Path) -> Path | None:
    """Return an existing bridge without modifying a user's installation."""
    assets = project_root / "Assets"
    if not assets.is_dir():
        return None
    matches = sorted(path for path in assets.rglob(BRIDGE_NAME) if path.is_file())
    return matches[0] if matches else None


def ensure_bridge_dependencies(project_root: str | Path) -> PackageInstallResult:
    """Add only the packages required by the bridge to a Unity manifest.

    Unity's Package Manager observes ``manifest.json`` and resolves the packages
    on the next editor refresh. Existing versions are deliberately preserved.
    """
    manifest = Path(project_root) / "Packages" / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PackageInstallResult("manifest_missing", message=f"Missing Unity package manifest: {manifest}")
    except (OSError, json.JSONDecodeError) as exc:
        return PackageInstallResult("manifest_invalid", message=str(exc))
    dependencies = data.get("dependencies")
    if not isinstance(dependencies, dict):
        return PackageInstallResult("manifest_invalid", message="manifest dependencies must be an object")

    added = tuple(name for name in REQUIRED_PACKAGES if name not in dependencies)
    if added:
        dependencies.update({name: REQUIRED_PACKAGES[name] for name in added})
        try:
            manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            return PackageInstallResult("package_install_failed", message=str(exc))
    return PackageInstallResult("packages_added" if added else "already_ready", added)


def ensure_new_input_is_enabled(project_root: str | Path) -> str | None:
    """Use Both input backends so Keyboard.current works without breaking legacy projects."""
    settings = Path(project_root) / "ProjectSettings" / "ProjectSettings.asset"
    try:
        text = settings.read_text(encoding="utf-8")
    except OSError:
        return None
    updated, count = re.subn(r"(activeInputHandler:\s*)\d+", r"\g<1>2", text, count=1)
    if count and updated != text:
        try:
            settings.write_text(updated, encoding="utf-8")
        except OSError:
            return None
        return "enabled"
    return None


def ensure_bridge_installed(project_root: str | Path, bridge_source: str | Path) -> BridgeInstallResult:
    """Install the bundled bridge once; never overwrite an existing bridge."""
    project = Path(project_root)
    existing = _installed_bridge(project)
    if existing is not None:
        return BridgeInstallResult("already_installed", existing)

    source = Path(bridge_source)
    if not source.is_file():
        return BridgeInstallResult(
            "source_missing",
            message=f"Bundled Unity bridge was not found: {source}",
        )

    destination = project / "Assets" / "Editor" / BRIDGE_NAME
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    except OSError as exc:
        return BridgeInstallResult("install_failed", destination, str(exc))
    return BridgeInstallResult("installed", destination)
