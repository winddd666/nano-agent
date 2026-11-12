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

    def test_v5_workspace_path_normalization_overwrite_and_edit_hints(self):
        reg = self.coding.tools()

        def invoke(name, args):
            return reg.execute({"id": "t1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}})

        # 1) /work/... and ./... normalize cleanly; optional read_file / list_files / search_text defaults work
        read_default = invoke("read_file", {"name": "/work/a.py"})
        self.assertIn("文件: a.py", read_default)
        self.assertIn("value = 1", read_default)
        self.assertIn("a.py", invoke("list_files", {"directory": "/work"}))
        self.assertIn("a.py", invoke("list_files", {}))
        self.assertIn("a.py:1:", invoke("search_text", {"query": "value"}))

        # /tmp/... and ../escape are still strictly blocked with clear error messages
        tmp_err = invoke("create_file", {"name": "/tmp/repro.py", "content": "x = 1\n"})
        self.assertIn("错误", tmp_err)
        self.assertIn("/tmp", tmp_err)
        self.assertIn("错误", invoke("read_file", {"name": "/work/../escape.py"}))

        # 2) create_file auto-mkdirs parents, returns sha256, and handles existing files cleanly
        created = invoke("create_file", {"name": "/work/sub/nested/repro.py", "content": "ans = 1\n"})
        self.assertIn("已创建 sub/nested/repro.py", created)
        self.assertIn("sha256:", created)

        exists_err = invoke("create_file", {"name": "sub/nested/repro.py", "content": "ans = 2\n"})
        self.assertIn("已存在", exists_err)
        self.assertIn("sha256:", exists_err)
        self.assertIn("overwrite=true", exists_err)
        self.assertNotIn(str(self.root), exists_err)

        overwritten = invoke("create_file", {"name": "sub/nested/repro.py", "content": "ans = 2\n", "overwrite": True})
        self.assertIn("已覆盖 sub/nested/repro.py", overwritten)
        new_sha = overwritten.split("sha256: ")[1].split("；")[0]
        self.assertEqual((self.root / "sub/nested/repro.py").read_text(encoding="utf-8"), "ans = 2\n")

        # 3) edit_file stale hash returns latest sha256; 0-match and multi-match give line hints
        (self.root / "multi.py").write_text("alpha = 1\nbeta = 2\nalpha = 1\n", encoding="utf-8")
        multi_sha = hashlib.sha256((self.root / "multi.py").read_bytes()).hexdigest()

        stale_err = invoke("edit_file", {
            "name": "sub/nested/repro.py",
            "expected_hash": "0" * 64,
            "old_text": "ans = 2",
            "new_text": "ans = 3",
        })
        self.assertIn(f"当前最新 sha256: {new_sha}", stale_err)

        dup_err = invoke("edit_file", {
            "name": "multi.py",
            "expected_hash": multi_sha,
            "old_text": "alpha = 1",
            "new_text": "alpha = 9",
        })
        self.assertIn("匹配到 2 处", dup_err)
        self.assertIn("1, 3", dup_err)

        zero_err = invoke("edit_file", {
            "name": "multi.py",
            "expected_hash": multi_sha,
            "old_text": "beta = 22",
            "new_text": "beta = 3",
        })
        self.assertIn("匹配 0 处", zero_err)
        self.assertIn("第 2 行", zero_err)

        # 4) Non-regular existing path (e.g. FIFO left by a command) is rejected immediately without blocking
        fifo_path = self.root / "leftover.fifo"
        os.mkfifo(fifo_path)
        for ov in (False, True):
            fifo_err = invoke("create_file", {"name": "leftover.fifo", "content": "x\n", "overwrite": ov})
            self.assertIn("错误", fifo_err)
            self.assertIn("不是普通文件", fifo_err)

    def test_nested_subprocess_live_diag_captures_inner_leaf_python_stack_and_command_hints(self):
        jobs = JobManager(foreground_yield_s=0.3, wait_timeout_s=0.4, stop_with_sigint=True, live_diag=True)
        self.addCleanup(jobs.close, "test_nested_diag")
        coding = CodingTools(self.workspace, jobs, allow_edit_during_readonly=True)

        # 1) Outer python3 spawns inner child python3 via subprocess.run: live_diag must show inner leaf stack!
        inner_code = (
            "import os, sys, time\n"
            "assert '/run/nano_hook' not in ':'.join(sys.path)\n"
            "assert not any(k.startswith('NANO_') for k in os.environ)\n"
            "def inner_leaf_worker():\n"
            "    end = time.monotonic() + 0.9\n"
            "    while time.monotonic() < end:\n"
            "        pass\n"
            "inner_leaf_worker()\n"
            "print('INNER_OK', flush=True)\n"
        )
        outer_code = (
            "import subprocess, sys\n"
            f"rc = subprocess.run([sys.executable, '-u', '-c', {inner_code!r}])\n"
            "sys.exit(rc.returncode)\n"
        )
        y1 = json.loads(coding.run_command(["python3", "-u", "-c", outer_code], "/work", {}, "foreground", "read_only"))
        self.assertEqual(y1["status"], "running")
        self.assertEqual(y1["cwd"], ".")
        self.assertIn("inner_leaf_worker", y1["live_diag"].get("current_stack", ""))
        self.assertNotIn("_try_wait", y1["live_diag"].get("current_stack", ""))
        done = json.loads(jobs.wait(y1["job_id"], wait_s=2))
        self.assertEqual(done["status"], "succeeded")
        self.assertIn("INNER_OK", done["output"])

        # 2) Actionable command hints on pytest / git / read_only EROFS
        pytest_fail = json.loads(coding.run_command(
            ["python3", "-c", "import sys; sys.stderr.write('No module named pytest\\n'); sys.exit(1)"],
            ".", {}, "foreground", "read_only",
        ))
        self.assertEqual(pytest_fail["status"], "failed")
        self.assertIn("未安装 pytest", pytest_fail.get("hint", ""))

        git_fail = json.loads(coding.run_command(
            ["python3", "-c", "import sys; sys.stderr.write('fatal: not a git repository\\n'); sys.exit(128)"],
            ".", {}, "foreground", "read_only",
        ))
        self.assertIn("get_diff", git_fail.get("hint", ""))

        ro_fail = json.loads(coding.run_command(
            ["python3", "-c", "open('/work/ro_blocked.txt', 'w')"],
            ".", {}, "foreground", "read_only",
        ))
        self.assertEqual(ro_fail["status"], "failed")
        self.assertIn("access='read_only'", ro_fail.get("hint", ""))
        self.assertIn("access='read_only'", json.loads(jobs.describe(ro_fail["job_id"])).get("hint", ""))

        # 3) Plain Python output inside bwrap has zero sitecustomize noise, and custom SIGUSR2 inside bwrap is untouched
        clean_ok = json.loads(coding.run_command(["python3", "-c", "print('OK')"], ".", {}, "foreground", "read_only"))
        self.assertEqual(clean_ok["status"], "succeeded")
        self.assertEqual(clean_ok["output"], "OK\n")

        sandbox_usr2 = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGUSR2, lambda *_: sys.exit(42))\n"
            "time.sleep(0.55)\n"
            "print('SANDBOX_USR2_SAFE', flush=True)\n"
        )
        u1 = json.loads(coding.run_command(["python3", "-u", "-c", sandbox_usr2], ".", {}, "foreground", "read_only"))
        self.assertEqual(u1["status"], "running")
        self.assertEqual(json.loads(jobs.describe(u1["job_id"]))["status"], "running")
        u_done = json.loads(jobs.wait(u1["job_id"], wait_s=3))
        self.assertEqual(u_done["status"], "succeeded")
        self.assertEqual(u_done["exit_code"], 0)
        self.assertEqual(u_done["output"], "SANDBOX_USR2_SAFE\n")


if __name__ == "__main__":
    unittest.main()
