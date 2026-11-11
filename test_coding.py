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

    def test_bwrap_sigint_captures_python_stack_on_cancel_and_timeout(self):
        jobs = JobManager(stop_with_sigint=True)
        self.addCleanup(jobs.close, "test_sigint")
        coding = CodingTools(self.workspace, jobs, allow_edit_during_readonly=True)
        spin = "print('READY', flush=True)\ndef spin_forever():\n    while True:\n        pass\nspin_forever()\n"

        # 1) Background launch + explicit cancel_job (even wrapped in GNU timeout)
        started = json.loads(coding.run_command(["timeout", "60", "python3", "-u", "-c", spin], ".", {}, "background", "read_only"))
        job_id = started["job_id"]
        import time
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "READY" not in json.loads(jobs.read(job_id, 0))["output"]:
            time.sleep(0.02)
        cancelled = json.loads(jobs.cancel(job_id))
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIn("spin_forever", cancelled["output"])
        self.assertIn("KeyboardInterrupt", cancelled["output"])

        # 2) Hard command timeout also captures stack trace inside bwrap
        timed_out = json.loads(coding.run_command(["python3", "-u", "-c", spin], ".", {}, "foreground", "read_only", timeout_s=1))
        self.assertEqual(timed_out["status"], "failed")
        self.assertEqual(timed_out["reason"], "timeout")
        self.assertIn("spin_forever", timed_out["output"])
        self.assertIn("KeyboardInterrupt", timed_out["output"])

    def test_readonly_background_allows_edit_and_marks_revision_changed(self):
        jobs = JobManager(stop_with_sigint=True)
        self.addCleanup(jobs.close, "test_lock")
        coding = CodingTools(self.workspace, jobs, allow_edit_during_readonly=True)
        ro_job = json.loads(coding.run_command(
            ["python3", "-c", "import time; time.sleep(0.35); print('ro_ok')"],
            ".", {}, "background", "read_only",
        ))
        digest = hashlib.sha256((self.root / "a.py").read_bytes()).hexdigest()
        edit_res = coding.edit_file("a.py", digest, "value = 1", "value = 99")
        self.assertIn("已修改", edit_res)
        finished = json.loads(jobs.wait(ro_job["job_id"]))
        self.assertEqual(finished["status"], "succeeded")
        self.assertTrue(finished["workspace_changed"])
        self.assertFalse(finished["stable_workspace"])

        # Write-mode background job still blocks edits and names the holding job_id
        wr_job = json.loads(coding.run_command(
            ["python3", "-c", "import time; time.sleep(5)"],
            ".", {}, "background", "write",
        ))
        digest2 = hashlib.sha256((self.root / "a.py").read_bytes()).hexdigest()
        blocked = coding.edit_file("a.py", digest2, "value = 99", "value = 100")
        self.assertIn(wr_job["job_id"], blocked)
        jobs.cancel(wr_job["job_id"])

    def test_live_diag_stack_peek_zero_pollution_and_non_python_safety(self):
        jobs = JobManager(foreground_yield_s=0.25, wait_timeout_s=0.35, stop_with_sigint=True, live_diag=True)
        self.addCleanup(jobs.close, "test_live_diag")
        coding = CodingTools(self.workspace, jobs, allow_edit_during_readonly=True)

        # 1) Fast command (< yield_s): zero live_diag overhead, zero sys.path/env/workspace pollution
        fast = json.loads(coding.run_command(
            ["python3", "-c", "import os, sys; print('HOOK:', '/run/nano_hook' in sys.path, 'ENV:', 'NANO_DIAG_STACK' in os.environ)"],
            ".", {}, "foreground", "read_only",
        ))
        self.assertEqual(fast["status"], "succeeded")
        self.assertNotIn("live_diag", fast)
        self.assertIn("HOOK: False ENV: False", fast["output"])
        self.assertIn("无改动", self.workspace.get_diff())

        # 2) Advancing multi-phase Python task: live stack moves from phase_one -> phase_two -> completes rc=0
        advancing = (
            "import time\n"
            "def phase_one():\n"
            "    end = time.monotonic() + 0.45\n"
            "    while time.monotonic() < end: pass\n"
            "def phase_two():\n"
            "    end = time.monotonic() + 0.55\n"
            "    while time.monotonic() < end: pass\n"
            "phase_one()\nphase_two()\nprint('ALL_DONE')\n"
        )
        y1 = json.loads(coding.run_command(["python3", "-u", "-c", advancing], ".", {}, "foreground", "read_only"))
        self.assertEqual(y1["status"], "running")
        self.assertIn("phase_one", y1["live_diag"]["current_stack"])
        self.assertNotIn("stack_moved", y1["live_diag"])

        y2 = json.loads(jobs.wait(y1["job_id"], wait_s=1))
        if y2["status"] == "running":
            self.assertIn("phase_two", y2["live_diag"]["current_stack"])
            self.assertTrue(y2["live_diag"]["stack_moved"])
            done = json.loads(jobs.wait(y1["job_id"], wait_s=2))
        else:
            done = y2
        self.assertEqual(done["status"], "succeeded")
        self.assertIn("ALL_DONE", done["output"])

        # 3) Pinned loop reports stack_moved == False; non-Python sleep safely skips SIGUSR2
        pinned = "def stuck_here():\n    while True: pass\nstuck_here()\n"
        p1 = json.loads(coding.run_command(["python3", "-u", "-c", pinned], ".", {}, "foreground", "read_only"))
        self.assertIn("stuck_here", p1["live_diag"]["current_stack"])
        p2 = json.loads(jobs.describe(p1["job_id"]))
        self.assertIn("stuck_here", p2["live_diag"]["current_stack"])
        self.assertFalse(p2["live_diag"]["stack_moved"])
        jobs.cancel(p1["job_id"])

        native_sleep = json.loads(coding.run_command(["sleep", "5"], ".", {}, "foreground", "read_only"))
        self.assertEqual(native_sleep["status"], "running")
        self.assertEqual(native_sleep["live_diag"]["state"], "idle_blocked")
        self.assertIn("sleep", native_sleep["live_diag"].get("active_proc", ""))
        jobs.cancel(native_sleep["job_id"])

    def test_multiprocess_runner_inside_bwrap_unwinds_outer_python_stack(self):
        jobs = JobManager(stop_with_sigint=True, live_diag=True)
        self.addCleanup(jobs.close, "test_mp_sigint")
        coding = CodingTools(self.workspace, jobs, allow_edit_during_readonly=True)
        runner = (
            "import subprocess\n"
            "print('RUNNER_READY', flush=True)\n"
            "def run_suite_step():\n"
            "    for _ in range(5):\n"
            "        subprocess.run(['sleep', '5'])\n"
            "run_suite_step()\n"
        )
        started = json.loads(coding.run_command(["python3", "-u", "-c", runner], ".", {}, "background", "read_only"))
        import time
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "RUNNER_READY" not in json.loads(jobs.read(started["job_id"], 0))["output"]:
            time.sleep(0.02)
        cancelled = json.loads(jobs.cancel(started["job_id"]))
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIn("run_suite_step", cancelled["output"])
        self.assertIn("KeyboardInterrupt", cancelled["output"])

    def test_delivery_guard_blocks_running_write_job_but_reaps_readonly_job(self):
        from unittest.mock import patch
        from agent import run_agent

        def assistant(content=None, *tool_calls):
            msg = {"role": "assistant", "content": content}
            if tool_calls:
                msg["tool_calls"] = list(tool_calls)
            return msg

        def call(cid, name, args):
            return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

        # Turn 1: start slow write command -> auto-yields
        # Turn 2: try premature final answer -> blocked by write-job guard!
        # Turn 3: cancel job-1 + launch background read_only probe
        # Turn 4: deliver final answer -> allowed while read_only job is reaped!
        step = {"n": 0}
        def scripted_model(messages, tools):
            step["n"] += 1
            if step["n"] == 1:
                return assistant(None, call("c1", "run_command", {
                    "argv": ["python3", "-c", "import time; time.sleep(10)"],
                    "cwd": ".", "env": {}, "mode": "foreground", "access": "write",
                }))
            if step["n"] == 2:
                return assistant("我打算不等写命令跑完直接交卷。")
            if step["n"] == 3:
                self.assertIn("独占写任务 [job-1(write)] 正在后台运行", messages[-1]["content"])
                return assistant(
                    None,
                    call("c2", "cancel_job", {"job_id": "job-1"}),
                    call("c3", "run_command", {
                        "argv": ["python3", "-c", "import time; time.sleep(10)"],
                        "cwd": ".", "env": {}, "mode": "background", "access": "read_only",
                    }),
                )
            return assistant("写任务已取消，只读辅助探测可直接回收交付。")

        with patch("agent.chat", side_effect=scripted_model):
            report = run_agent(
                "测试写任务交付守卫", max_calls=6, review=False, profile="coding",
                workspace=self.root, foreground_yield_s=0.2, wait_timeout_s=0.5,
                allow_edit_during_readonly=True, stop_with_sigint=True, live_diag=True,
                on_event=lambda _: None,
            )
        self.assertEqual(report["status"], "answered")
        self.assertEqual(step["n"], 4)
        self.assertEqual(report.get("reaped_readonly_jobs"), ["job-2"])


if __name__ == "__main__":
    unittest.main()
