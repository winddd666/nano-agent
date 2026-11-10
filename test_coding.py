import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from coding_tools import CodingTools
from jobs import JobManager
from workspace import Workspace


@unittest.skipUnless(shutil.which("bwrap"), "bwrap unavailable")
class CodingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "a.py").write_text("value = 1\n", encoding="utf-8")
        self.workspace = Workspace(self.root)
        self.jobs = JobManager()
        self.addCleanup(self.jobs.close, "test_finished")
        self.coding = CodingTools(self.workspace, self.jobs)

    def test_read_edit_conflict_and_diff(self):
        source = (self.root / "a.py").read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        self.assertIn(digest, self.workspace.read_file("a.py", 1, 2))
        self.assertIn("已修改", self.coding.edit_file("a.py", digest, "value = 1", "value = 2"))
        self.assertIn("摘要已变化", self.coding.edit_file("a.py", digest, "value = 2", "value = 3"))
        self.assertIn("+value = 2", self.workspace.get_diff())
        self.assertIn("已创建", self.coding.create_file("empty.txt", ""))
        self.assertIn("empty.txt", self.workspace.get_diff())

    def test_links_and_parent_escape_rejected(self):
        (self.root / "link").symlink_to("/etc/passwd")
        self.assertIn("错误", self.workspace.read_file("link", 1, 1))
        self.assertIn("错误", self.workspace.create_file("../escape", "x"))
        self.assertIn("错误", self.workspace.list_files("../escape"))

    def test_large_file_change_updates_revision(self):
        path = self.root / "large.bin"
        path.write_bytes(b"a" * (1024 * 1024 + 1))
        self.workspace.refresh()
        revision = self.workspace.revision
        path.write_bytes(b"b" * (1024 * 1024 + 1))
        self.assertGreater(self.workspace.refresh(), revision)
        self.assertIn("large.bin: 二进制或超过", self.workspace.get_diff())

    def test_existing_hard_link_is_rejected(self):
        outside = self.root.parent / (self.root.name + "-outside.txt")
        outside.write_text("outside", encoding="utf-8")
        self.addCleanup(outside.unlink)
        os.link(outside, self.root / "linked.txt")
        with self.assertRaisesRegex(ValueError, "硬链接"):
            Workspace(self.root)

    def test_command_is_limited_to_workspace_and_tracks_revision(self):
        result = json.loads(self.coding.run_command(
            ["python3", "-c", "from pathlib import Path; Path('new.txt').write_text('ok'); print(Path('/work/a.py').read_text().strip())"],
            ".", {}, "foreground"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("value = 1", result["output"])
        self.assertTrue(result["workspace_changed"])
        self.assertEqual(self.workspace.revision, result["workspace_revision_end"])
        self.assertTrue((self.root / "new.txt").exists())
        hidden = json.loads(self.coding.run_command(
            ["python3", "-c", "from pathlib import Path; print(Path('/home').exists())"], ".", {}, "foreground"))
        self.assertIn("False", hidden["output"])


if __name__ == "__main__":
    unittest.main()
