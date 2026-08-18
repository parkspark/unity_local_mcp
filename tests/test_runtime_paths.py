import sys
from pathlib import Path
from unittest import mock

import runtime_paths


def test_source_paths_use_module_directory():
    with mock.patch.object(sys, "frozen", False, create=True), \
         mock.patch.object(sys, "_MEIPASS", None, create=True):
        expected = Path(runtime_paths.__file__).resolve().parent
        assert runtime_paths.app_dir() == expected
        assert runtime_paths.resource_dir() == expected


def test_frozen_paths_separate_writable_and_bundled_directories(tmp_path):
    executable = tmp_path / "app" / "UnityLocalAgent.exe"
    bundle = tmp_path / "bundle"
    with mock.patch.object(sys, "frozen", True, create=True), \
         mock.patch.object(sys, "executable", str(executable)), \
         mock.patch.object(sys, "_MEIPASS", str(bundle), create=True):
        assert runtime_paths.app_dir() == executable.parent.resolve()
        assert runtime_paths.resource_dir() == bundle.resolve()
