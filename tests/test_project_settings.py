import json
import tempfile
import unittest
from pathlib import Path

from project_settings import select_project


class ProjectSettingsTests(unittest.TestCase):
    def make_project(self, root: Path, name: str) -> Path:
        project = root / name
        (project / "Assets").mkdir(parents=True)
        (project / "ProjectSettings").mkdir()
        return project

    def test_cli_project_wins_and_is_remembered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli_project = self.make_project(root, "cli")
            env_project = self.make_project(root, "env")
            settings = root / "settings.json"

            selected = select_project(
                str(cli_project), str(env_project),
                environ={"UNITY_PROJECT_DIR": str(env_project)}, settings_path=settings,
            )

            self.assertEqual(selected.path, str(cli_project.resolve()))
            self.assertEqual(selected.source, "--project")
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["last_project"], selected.path)

    def test_environment_wins_over_recent_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_project = self.make_project(root, "env")
            recent_project = self.make_project(root, "recent")
            settings = root / "settings.json"
            settings.write_text(json.dumps({"last_project": str(recent_project)}), encoding="utf-8")

            selected = select_project(
                None, str(recent_project),
                environ={"UNITY_PROJECT_DIR": str(env_project)}, settings_path=settings,
            )

            self.assertEqual(selected.path, str(env_project.resolve()))
            self.assertEqual(selected.source, "UNITY_PROJECT_DIR")

    def test_recent_project_is_reused_without_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recent_project = self.make_project(root, "recent")
            settings = root / "settings.json"
            settings.write_text(json.dumps({"last_project": str(recent_project)}), encoding="utf-8")

            selected = select_project(
                None, str(root / "old-default"), environ={}, settings_path=settings,
            )

            self.assertEqual(selected.path, str(recent_project.resolve()))
            self.assertEqual(selected.source, "최근 프로젝트")

    def test_invalid_explicit_project_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "Unity 프로젝트가 아닙니다"):
                select_project(
                    tmp, tmp, environ={}, settings_path=Path(tmp) / "settings.json",
                )


if __name__ == "__main__":
    unittest.main()
