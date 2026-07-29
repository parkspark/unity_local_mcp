import os
import tempfile
import unittest

import snapshot


def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _read(root, rel):
    with open(os.path.join(root, *rel.split("/")), encoding="utf-8") as handle:
        return handle.read()


class SnapshotTests(unittest.TestCase):
    def test_captures_only_repair_writable_trees(self):
        with tempfile.TemporaryDirectory() as project:
            _write(project, "Assets/Scripts/Player.cs", "class Player {}")
            _write(project, "Assets/StreamingAssets/Levels/level1.json", "{}")
            _write(project, "Assets/Scenes/Game.unity", "scene")
            _write(project, "Assets/Art/logo.png", "binary")

            captured = snapshot.capture(project, "t")
            self.assertIn("Assets/Scripts/Player.cs", captured.files)
            self.assertIn("Assets/StreamingAssets/Levels/level1.json", captured.files)
            # Untracked trees stay out unless named explicitly.
            self.assertNotIn("Assets/Scenes/Game.unity", captured.files)
            self.assertNotIn("Assets/Art/logo.png", captured.files)

    def test_extra_paths_are_captured(self):
        with tempfile.TemporaryDirectory() as project:
            _write(project, "Assets/Scenes/Game.unity", "scene")
            captured = snapshot.capture(project, "t", ["Assets/Scenes/Game.unity"])
            self.assertIn("Assets/Scenes/Game.unity", captured.files)

    def test_restore_reverts_modified_content(self):
        with tempfile.TemporaryDirectory() as project:
            _write(project, "Assets/Scripts/Player.cs", "good")
            captured = snapshot.capture(project, "good")
            _write(project, "Assets/Scripts/Player.cs", "broken")

            changed = snapshot.restore(captured, project)
            self.assertEqual(changed, ["Assets/Scripts/Player.cs"])
            self.assertEqual(_read(project, "Assets/Scripts/Player.cs"), "good")

    def test_restore_deletes_files_created_after_capture(self):
        with tempfile.TemporaryDirectory() as project:
            _write(project, "Assets/Scripts/Player.cs", "good")
            captured = snapshot.capture(project, "good")
            _write(project, "Assets/Scripts/Extra.cs", "added by repair")
            _write(project, "Assets/Scripts/Extra.cs.meta", "meta")

            changed = snapshot.restore(captured, project)
            self.assertIn("Assets/Scripts/Extra.cs", changed)
            self.assertFalse(
                os.path.exists(os.path.join(project, "Assets", "Scripts", "Extra.cs"))
            )
            # The orphaned .meta must go too or Unity warns on every import.
            self.assertFalse(
                os.path.exists(os.path.join(project, "Assets", "Scripts", "Extra.cs.meta"))
            )

    def test_restore_recreates_deleted_file(self):
        with tempfile.TemporaryDirectory() as project:
            _write(project, "Assets/Scripts/Player.cs", "good")
            captured = snapshot.capture(project, "good")
            os.remove(os.path.join(project, "Assets", "Scripts", "Player.cs"))

            changed = snapshot.restore(captured, project)
            self.assertEqual(changed, ["Assets/Scripts/Player.cs"])
            self.assertEqual(_read(project, "Assets/Scripts/Player.cs"), "good")

    def test_restore_never_deletes_an_extra_path(self):
        """씬 파일은 명시 경로로만 들어오므로 삭제 대상이 되면 안 된다."""
        with tempfile.TemporaryDirectory() as project:
            _write(project, "Assets/Scripts/Player.cs", "good")
            captured = snapshot.capture(project, "good")
            _write(project, "Assets/Scenes/New.unity", "new scene")

            snapshot.restore(captured, project)
            self.assertTrue(
                os.path.exists(os.path.join(project, "Assets", "Scenes", "New.unity"))
            )

    def test_unchanged_project_restores_nothing(self):
        with tempfile.TemporaryDirectory() as project:
            _write(project, "Assets/Scripts/Player.cs", "good")
            captured = snapshot.capture(project, "good")
            self.assertEqual(snapshot.restore(captured, project), [])

    def test_missing_project_dir_is_safe(self):
        empty = snapshot.capture(os.path.join(tempfile.gettempdir(), "no_such_dir"), "t")
        self.assertEqual(len(empty), 0)
        self.assertEqual(snapshot.restore(empty, "/no/such/dir"), [])


if __name__ == "__main__":
    unittest.main()
