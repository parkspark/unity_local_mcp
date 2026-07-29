"""Deterministic file snapshots so an automatic repair cycle can be undone.

Automatic repair can trade one bug for another: a measured E2E run fixed the
jump at cycle 2 (1 failure left) and then broke it again at cycle 3 while
chasing movement (2 failures). Without a snapshot the project keeps the worse
final state even though a strictly better one existed.

Only what repair is allowed to touch is captured: project scripts, level data,
and any file the request named. Scene object state reaches disk through the
scene file, which the repair loop already saves before every reverification.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable, Iterator

# (folder, suffix) pairs scanned recursively. These are exactly the trees the
# host's own write tools are allowed to modify.
SNAPSHOT_TREES = (
    ("Assets/Scripts", ".cs"),
    ("Assets/StreamingAssets/Levels", ".json"),
)


@dataclass
class ProjectSnapshot:
    label: str
    files: dict[str, bytes] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.files)


def _normalise(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("/")


def _tree_files(project_dir: str) -> Iterator[str]:
    """Every file inside the repair-writable trees, project-relative."""
    for folder, suffix in SNAPSHOT_TREES:
        root = os.path.join(project_dir, *folder.split("/"))
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                if name.lower().endswith(suffix):
                    yield _normalise(
                        os.path.relpath(os.path.join(dirpath, name), project_dir)
                    )


def capture(project_dir: str, label: str, extra_paths: Iterable[str] = ()) -> ProjectSnapshot:
    """Read the repair-writable files plus any explicitly named ones."""
    snapshot = ProjectSnapshot(label=label)
    if not project_dir or not os.path.isdir(project_dir):
        return snapshot
    candidates = list(_tree_files(project_dir))
    candidates.extend(_normalise(path) for path in extra_paths)
    for rel in candidates:
        if not rel or rel in snapshot.files or ".." in rel.split("/"):
            continue
        absolute = os.path.join(project_dir, *rel.split("/"))
        if not os.path.isfile(absolute):
            continue
        try:
            with open(absolute, "rb") as handle:
                snapshot.files[rel] = handle.read()
        except OSError:
            continue
    return snapshot


def diff(snapshot: ProjectSnapshot, project_dir: str) -> tuple[list[str], list[str]]:
    """(files whose content differs, files created since the snapshot)."""
    current = capture(project_dir, "current", snapshot.files.keys())
    modified = [
        rel for rel, data in snapshot.files.items()
        if current.files.get(rel) != data
    ]
    # Only files inside the scanned trees may be considered "created"; a path
    # supplied through extra_paths is never deleted by a restore.
    tree_now = set(_tree_files(project_dir))
    created = sorted(tree_now - set(snapshot.files))
    return sorted(modified), created


def restore(snapshot: ProjectSnapshot, project_dir: str) -> list[str]:
    """Put the captured files back and remove ones created after capture.

    Returns the project-relative paths that actually changed on disk.
    """
    if not project_dir or not os.path.isdir(project_dir):
        return []
    modified, created = diff(snapshot, project_dir)
    changed: list[str] = []

    for rel in modified:
        absolute = os.path.join(project_dir, *rel.split("/"))
        try:
            os.makedirs(os.path.dirname(absolute), exist_ok=True)
            with open(absolute, "wb") as handle:
                handle.write(snapshot.files[rel])
            changed.append(rel)
        except OSError:
            continue

    for rel in created:
        absolute = os.path.join(project_dir, *rel.split("/"))
        try:
            os.remove(absolute)
            changed.append(rel)
        except OSError:
            continue
        # Unity leaves an orphaned .meta behind otherwise.
        meta = absolute + ".meta"
        if os.path.exists(meta):
            try:
                os.remove(meta)
            except OSError:
                pass
    return changed
