import tempfile
import unittest
from pathlib import Path

from bridge_install import ensure_bridge_dependencies, ensure_bridge_installed, ensure_new_input_is_enabled


class BridgeInstallTests(unittest.TestCase):
    def make_project(self, root: Path) -> Path:
        project = root / "project"
        (project / "Assets").mkdir(parents=True)
        (project / "ProjectSettings").mkdir()
        (project / "Packages").mkdir()
        (project / "Packages/manifest.json").write_text('{"dependencies": {}}', encoding="utf-8")
        (project / "ProjectSettings/ProjectSettings.asset").write_text(
            "activeInputHandler: 0\n", encoding="utf-8"
        )
        return project

    def test_installs_bundled_bridge_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.make_project(root)
            source = root / "UnityMcpBridge.cs"
            source.write_text("bridge", encoding="utf-8")

            result = ensure_bridge_installed(project, source)

            self.assertEqual(result.status, "installed")
            self.assertEqual((project / "Assets/Editor/UnityMcpBridge.cs").read_text(), "bridge")

    def test_preserves_existing_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.make_project(root)
            existing = project / "Assets/Editor/UnityMcpBridge.cs"
            existing.parent.mkdir()
            existing.write_text("custom", encoding="utf-8")
            source = root / "UnityMcpBridge.cs"
            source.write_text("bundled", encoding="utf-8")

            result = ensure_bridge_installed(project, source)

            self.assertEqual(result.status, "already_installed")
            self.assertEqual(existing.read_text(), "custom")

    def test_reports_missing_bundled_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = ensure_bridge_installed(self.make_project(Path(tmp)), Path(tmp) / "missing.cs")
            self.assertEqual(result.status, "source_missing")

    def test_adds_missing_packages_and_enables_new_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self.make_project(Path(tmp))

            result = ensure_bridge_dependencies(project)

            self.assertEqual(result.status, "packages_added")
            manifest = (project / "Packages/manifest.json").read_text(encoding="utf-8")
            self.assertIn("com.unity.inputsystem", manifest)
            self.assertIn("com.unity.nuget.newtonsoft-json", manifest)
            self.assertEqual(ensure_new_input_is_enabled(project), "enabled")
            self.assertIn("activeInputHandler: 2", (project / "ProjectSettings/ProjectSettings.asset").read_text())

    def test_preserves_existing_package_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self.make_project(Path(tmp))
            manifest = project / "Packages/manifest.json"
            manifest.write_text(
                '{"dependencies": {"com.unity.inputsystem": "9.9.9", "com.unity.nuget.newtonsoft-json": "8.8.8"}}',
                encoding="utf-8",
            )

            result = ensure_bridge_dependencies(project)

            self.assertEqual(result.status, "already_ready")
            self.assertIn("9.9.9", manifest.read_text(encoding="utf-8"))
