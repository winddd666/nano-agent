"""真实子进程与控制协议检查；模型响应受控，真实 API 验证另记。"""

import io
import json
import os
import pty
import sys
import termios
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from threading import Event
from unittest.mock import Mock, patch

from agent import run_agent
from cancel_experiment import PROCESS, verify
from control import RequestCancelled
from jobs import JobManager
from model import _process_request
from runtime_input import InputInbox, StdinInput
from test_agent import assistant, request
from test_reviewer import recommendation
from tool_registry import Tool, ToolRegistry


def blocked_worker(connection):
    connection.send(("event", {"type": "text.delta", "text": "未完成", "pid": os.getpid()}))
    time.sleep(30)
    connection.send(("result", assistant(content="不应收到")))


class JobTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def manager(self, code="import time; print('开始', flush=True); time.sleep(30)", **kwargs):
        manager = JobManager(checks_command=[sys.executable, "-u", "-c", code], **kwargs)
        self.addCleanup(manager.close, "test_cleanup")
        return manager

    def assert_reaped(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_success_failure_unicode_output_and_idempotent_cancel(self):
        for code, expected in [(0, "succeeded"), (3, "failed")]:
            with self.subTest(code=code):
                manager = self.manager(f"print('中文结果'); raise SystemExit({code})")
                job_id = json.loads(manager.run_checks("background"))["job_id"]
                result = json.loads(manager.wait(job_id))
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["exit_code"], code)
                self.assertEqual(result["output"], "中文结果\n")
                self.assertEqual(json.loads(manager.cancel(job_id))["status"], expected)
                manager.close("test_done")
                self.assertEqual(json.loads(manager.read(job_id, 0))["output"], "中文结果\n")

    def test_background_prompt_is_answered_while_real_job_is_running_then_run_cleans_up(self):
        manager, inbox, observed = self.manager(), InputInbox(), []

        def listener(event):
            if event["type"] == "job.waiting":
                inbox.background()
            if event["type"] == "answer":
                observed.append(manager.records["job-1"]["status"])

        def model(messages, tools, **kwargs):
            if len(messages) == 2:
                return assistant(request("checks", "run_project_checks", {"mode": "foreground"}))
            receipt = json.loads(messages[-1]["content"])
            self.assertEqual(receipt["status"], "running")
            self.assertEqual(receipt["job_id"], "job-1")
            return assistant(content="检查仍在后台运行，这是当前方案的解释。")

        with patch("agent.chat", side_effect=model):
            report = run_agent("检查项目", inbox=inbox, jobs=manager, review=False, on_event=listener)
        self.assertEqual(observed, ["running"])
        self.assertEqual(report["status"], "answered")
        self.assertEqual(report["jobs"]["job-1"]["status"], "cancelled")
        self.assertEqual(report["jobs"]["job-1"]["reason"], "run_answered")
        self.assertEqual(report["jobs"]["job-1"]["call_id"], "checks")
        self.assert_reaped(report["jobs"]["job-1"]["pid"])
        self.assertEqual([e["outcome"] for e in report["events"] if e["type"] == "control.handled"], ["backgrounded"])
        json.dumps(report)  # 报告中不能残留进程句柄、Path 或临时实现字段。

    def test_send_now_yields_wait_skips_unstarted_calls_and_preserves_pairs(self):
        manager, inbox, handler = self.manager(), InputInbox(), Mock(return_value="不应执行")
        registry = ToolRegistry([Tool("later", "下一项", {}, handler)])

        def listener(event):
            if event["type"] == "job.waiting":
                inbox.send_now("先解释方案")

        def model(messages, tools, **kwargs):
            if len(messages) == 2:
                return assistant(request("checks", "run_project_checks", {"mode": "foreground"}),
                                 request("later", "later", {}))
            self.assertEqual([m["role"] for m in messages[-4:]], ["assistant", "tool", "tool", "user"])
            self.assertEqual(json.loads(messages[-3]["content"])["status"], "running")
            self.assertIn("尚未启动", messages[-2]["content"])
            self.assertEqual(messages[-1]["content"], "先解释方案")
            return assistant(content="方案说明；检查尚未完成。")

        with patch("agent.REGISTRY", registry), patch("agent.chat", side_effect=model):
            report = run_agent("检查项目", inbox=inbox, jobs=manager, review=False, on_event=listener)
        handler.assert_not_called()
        self.assertEqual(report["answer_version"], 1)
        self.assertEqual(report["model_requests"], 2)
        self.assertEqual([e["call_id"] for e in report["events"] if e["type"] == "tool.skipped"], ["later"])

    def test_send_now_before_tool_start_does_not_launch_any_process(self):
        manager, inbox = self.manager(), InputInbox()

        def listener(event):
            if event["type"] == "tool.started":
                inbox.send_now("先回答我")

        replies = [assistant(request("checks", "run_project_checks", {"mode": "background"})),
                   assistant(content="先回答；检查没有启动。")]
        with patch("agent.chat", side_effect=replies):
            report = run_agent("检查", inbox=inbox, jobs=manager, review=False, on_event=listener)
        self.assertEqual(report["status"], "answered")
        self.assertEqual(report["jobs"], {})

    def test_model_cancel_tool_targets_one_job_and_sibling_completes(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            gate = Path(directory) / "finish"
            manager = JobManager(checks_command=[sys.executable, "-u", "-c", PROCESS,
                                                str(Path(__file__).resolve().parent / "sandbox"), str(gate)])
            self.addCleanup(manager.close, "test_cleanup")

            def listener(event):
                if event["type"] == "job.completed" and event["job_id"] == "job-1" and event["status"] == "cancelled":
                    self.assertEqual(manager.records["job-2"]["status"], "running")
                    gate.touch()

            replies = [assistant(request("one", "run_project_checks", {"mode": "background"}),
                                 request("two", "run_project_checks", {"mode": "background"})),
                       assistant(request("cancel", "cancel_job", {"job_id": "job-1"}),
                                 request("wait", "wait_job", {"job_id": "job-2"})),
                       assistant(content="第一项取消，第二项正常完成。")]
            with patch("agent.chat", side_effect=replies):
                report = run_agent("定向取消", jobs=manager, review=False, on_event=listener)
            result = verify(report)
            self.assertEqual(result["cancel_call_id"], "cancel")
            for job in report["jobs"].values():
                self.assert_reaped(job["pid"])

    def test_send_now_discards_partial_model_tools_without_execution(self):
        inbox, count = InputInbox(), 0

        def model(messages, tools, *, stream, on_event, control):
            nonlocal count
            count += 1
            if count == 1:
                on_event({"type": "tool.delta", "index": 0, "arguments": '{"mode":'})
                inbox.send_now("先回答")
                return assistant(request("not-run", "run_project_checks", {"mode": "background"}))
            self.assertEqual([m["role"] for m in messages], ["system", "user", "user"])
            return assistant(content="新要求的回答")

        with patch("agent.chat", side_effect=model):
            report = run_agent("原任务", inbox=inbox, stream=True, review=False, on_event=lambda e: None)
        self.assertEqual(report["answer"], "新要求的回答")
        self.assertEqual(report["jobs"], {})
        self.assertEqual(len([e for e in report["events"] if e["type"] == "model.completed"]), 1)
        self.assertTrue(next(e for e in report["events"] if e["type"] == "model.failed")["cancelled"])

    def test_network_worker_can_be_cancelled_even_without_another_chunk(self):
        inbox, pids = InputInbox(), []

        def receive(event):
            pids.append(event["pid"])
            inbox.send_now("立即处理")

        start = time.monotonic()
        with self.assertRaises(RequestCancelled):
            _process_request(blocked_worker, (), on_event=receive, control=inbox.urgent)
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual(len(pids), 1)
        self.assert_reaped(pids[0])

    def test_observer_error_also_reaps_network_worker(self):
        pids = []

        def receive(event):
            pids.append(event["pid"])
            raise RuntimeError("事件保存失败")

        with self.assertRaisesRegex(RuntimeError, "事件保存失败"):
            _process_request(blocked_worker, (), on_event=receive, control=lambda: False)
        self.assert_reaped(pids[0])

    def test_send_now_during_review_records_superseded_not_runtime_failure(self):
        inbox, count = InputInbox(), 0

        def reviewer(messages, tools, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                inbox.send_now("最新要求")
            return recommendation()

        with patch("agent.chat", side_effect=[assistant(content="旧候选"), assistant(content="新候选"), assistant(content="新交付")]):
            with patch("reviewer.chat", side_effect=reviewer):
                report = run_agent("原任务", inbox=inbox, on_event=lambda e: None)
        self.assertEqual(report["reviews"][0]["status"], "superseded")
        self.assertEqual(report["answer_version"], 1)
        self.assertEqual([e["task_version"] for e in report["events"] if e["type"] == "review.feedback"], [1])

    def test_budget_exhaustion_after_cancel_does_not_reset_budget(self):
        inbox = InputInbox()

        def model(messages, tools, **kwargs):
            inbox.send_now("新要求")
            return assistant(content="旧回答")

        with patch("agent.chat", side_effect=model):
            report = run_agent("原任务", inbox=inbox, review=False, max_calls=1, on_event=lambda e: None)
        self.assertEqual(report["status"], "limit_reached")
        self.assertEqual(report["model_requests"], 1)
        self.assertNotIn("answer", report)

    def test_timeout_and_output_limit_stop_processes(self):
        for manager in [self.manager(timeout=0.01), self.manager("print('x' * 20000, flush=True); import time; time.sleep(30)", output_limit=128)]:
            result = json.loads(manager.run_checks("foreground"))
            self.assertEqual(result["status"], "failed")
            self.assertIn(manager.records["job-1"]["reason"], ["timeout", "output_limit"])
            self.assertLessEqual(len(result["output"]), manager.output_limit)
            self.assert_reaped(result["pid"])

    def test_unknown_ids_and_invalid_modes_do_not_launch_commands(self):
        manager = self.manager()
        for value in [manager.run_checks("shell"), manager.describe("absent"), manager.cancel("absent"),
                      manager.read("absent", 0), manager.read("absent", -1)]:
            self.assertTrue(value.startswith("错误："))
        self.assertEqual(manager.records, {})

    def test_cancel_stops_child_process_group_and_reaps_direct_child(self):
        code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(p.pid,flush=True); time.sleep(30)"
        manager = self.manager(code)
        job_id = json.loads(manager.run_checks("background"))["job_id"]
        deadline = time.monotonic() + 3
        child_pid = None
        while time.monotonic() < deadline:
            text = json.loads(manager.read(job_id, 0))["output"].strip()
            if text:
                child_pid = int(text)
                break
            time.sleep(0.01)
        self.assertIsNotNone(child_pid)
        result = json.loads(manager.cancel(job_id))
        self.assertEqual(result["status"], "cancelled")
        self.assert_reaped(result["pid"])
        try:
            with open(f"/proc/{child_pid}/stat") as child_stat:
                self.assertEqual(child_stat.read().split()[2], "Z", "子进程不能继续运行")
        except FileNotFoundError:
            pass

    def test_job_log_failure_stops_run_and_reaps_process(self):
        manager = self.manager()

        def listener(event):
            if event["type"] == "job.started":
                raise RuntimeError("任务日志失败")

        with patch("agent.chat", return_value=assistant(request("checks", "run_project_checks", {"mode": "background"}))) as actor:
            report = run_agent("检查", jobs=manager, review=False, on_event=listener)
        self.assertEqual(report["status"], "runtime_error")
        self.assertEqual(actor.call_count, 1)
        self.assert_reaped(report["jobs"]["job-1"]["pid"])

    def test_idle_background_control_does_not_generate_an_extra_model_request(self):
        inbox = InputInbox()

        def listener(event):
            if event["type"] == "model.completed":
                inbox.background()

        with patch("agent.chat", return_value=assistant(content="最终回答")) as actor:
            report = run_agent("问题", inbox=inbox, review=False, on_event=listener)
        self.assertEqual(actor.call_count, 1)
        self.assertEqual(report["status"], "answered")

    def test_ctrl_b_arrives_without_enter_and_terminal_settings_restore(self):
        ready = Event()

        class Observed(InputInbox):
            def background(self):
                super().background()
                ready.set()

        inbox = Observed()
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        try:
            with os.fdopen(slave, encoding="utf-8", closefd=False) as stream:
                with StdinInput(inbox, stream) as reader:
                    os.write(master, b'\x02')
                    self.assertTrue(ready.wait(2))
                self.assertFalse(reader.thread.is_alive())
                self.assertEqual(termios.tcgetattr(slave), original)
            self.assertEqual(len(inbox.take_background()), 1)
        finally:
            os.close(master)
            os.close(slave)

    def test_pipe_parses_control_commands_and_preserves_normal_text(self):
        done = Event()

        class Observed(InputInbox):
            def submit(self, text, **kwargs):
                result = super().submit(text, **kwargs)
                if result == 2:
                    done.set()
                return result

        inbox = Observed()
        reader_fd, writer_fd = os.pipe()
        with os.fdopen(reader_fd, encoding="utf-8") as stream:
            with StdinInput(inbox, stream):
                os.write(writer_fd, '/bg\n普通消息\n/now 马上解释\n'.encode())
                os.close(writer_fd)
                self.assertTrue(done.wait(2))
        self.assertTrue(inbox.urgent())
        self.assertEqual([(i["text"], i["mode"]) for i in inbox.drain()], [("普通消息", "normal"), ("马上解释", "immediate")])
        self.assertFalse(inbox.urgent())
        self.assertEqual(len(inbox.take_background()), 1)

    def test_auto_yield_bounded_wait_and_silent_timeout_clamp(self):
        script = (
            "import time\n"
            "print('phase-1', flush=True)\n"
            "time.sleep(0.28)\n"
            "print('phase-2', flush=True)\n"
            "time.sleep(0.28)\n"
            "print('all-done', flush=True)\n"
        )
        manager = self.manager(
            script, timeout=5, max_timeout=10,
            foreground_yield_s=0.12, wait_timeout_s=0.15, max_wait_timeout_s=0.45,
        )
        job_id = manager.start([sys.executable, "-u", "-c", script], label="command", timeout=999)
        self.assertEqual(manager.records[job_id]["timeout_s"], 10)
        self.assertTrue(manager.records[job_id]["timeout_clamped"])

        first = json.loads(manager.wait_foreground(job_id))
        self.assertEqual(first["status"], "running")
        self.assertTrue(first["yielded"])
        self.assertEqual(first["yield_reason"], "auto_yield")
        self.assertIn("phase-1", first["output"])
        self.assertGreater(first["delta_chars"], 0)

        second = json.loads(manager.wait(job_id))
        self.assertEqual(second["status"], "running")
        self.assertTrue(second["yielded"])
        self.assertEqual(second["yield_reason"], "wait_timeout")

        final = json.loads(manager.wait(job_id, wait_s=99))
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(final["exit_code"], 0)
        self.assertTrue(final["wait_clamped"])
        self.assertIn("all-done", final["output"])

    def test_smart_notification_excerpt_preserves_head_error_and_tail(self):
        script = (
            "print('HEAD_START_MARKER')\n"
            "for i in range(80): print('noise line', i)\n"
            "print('ValueError: critical_mid_error_123')\n"
            "for i in range(80): print('more noise line', i)\n"
            "print('TAIL_END_MARKER')\n"
        )
        inbox = InputInbox()
        manager = self.manager(script, smart_excerpt=True)
        manager.inbox = inbox
        job_id = json.loads(manager.run_checks("background"))["job_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and manager.records[job_id]["status"] == "running":
            manager.poll()
            time.sleep(0.02)
        items = inbox.drain_events()
        self.assertEqual(len(items), 1)
        payload = items[0]["payload"]
        self.assertTrue(payload["output_truncated"])
        self.assertLessEqual(len(payload["output_excerpt"]), 2000)
        self.assertIn("HEAD_START_MARKER", payload["output_excerpt"])
        self.assertIn("critical_mid_error_123", payload["output_excerpt"])
        self.assertIn("TAIL_END_MARKER", payload["output_excerpt"])

    def test_live_diag_clean_sitecustomize_and_custom_sigusr2_ownership(self):
        from pathlib import Path

        manager = JobManager(foreground_yield_s=0.15, wait_timeout_s=0.15, stop_with_sigint=True, live_diag=True)
        self.addCleanup(manager.close, "test_diag_ownership")

        # 1) Plain Python command without user sitecustomize emits zero stderr noise
        jid_plain = manager.start([sys.executable, "-c", "print('OK')"], label="command")
        res_plain = json.loads(manager.wait(jid_plain, wait_s=5))
        self.assertEqual(res_plain["status"], "succeeded")
        self.assertEqual(res_plain["output"], "OK\n")

        # Downstream user sitecustomize on PYTHONPATH still chains cleanly
        with tempfile.TemporaryDirectory() as user_site:
            (Path(user_site) / "sitecustomize.py").write_text("import builtins; builtins._USER_SITE_OK = 7\n", encoding="utf-8")
            env = dict(os.environ, PYTHONPATH=user_site)
            jid_chain = manager.start([sys.executable, "-c", "import builtins; print(builtins._USER_SITE_OK)"], label="command", env=env)
            res_chain = json.loads(manager.wait(jid_chain, wait_s=5))
            self.assertEqual(res_chain["status"], "succeeded")
            self.assertEqual(res_chain["output"], "7\n")

        # 2) Python command installing its own SIGUSR2 handler (exit 42) is never signaled by live_diag
        with tempfile.TemporaryDirectory() as gate_dir:
            gate = Path(gate_dir) / "release"
            app_usr2 = (
                "import os, pathlib, signal, sys, time\n"
                "assert signal.signal(signalnum=signal.SIGTERM, handler=signal.SIG_IGN) == signal.SIG_DFL\n"
                "orig = signal.signal(signalnum=signal.SIGUSR2, handler=lambda *_: sys.exit(42))\n"
                "assert orig == signal.SIG_DFL, orig\n"
                "print('HANDLER_INSTALLED', flush=True)\n"
                f"g = pathlib.Path({str(gate)!r})\n"
                "deadline = time.monotonic() + 5\n"
                "while not g.exists() and time.monotonic() < deadline:\n"
                "    time.sleep(0.02)\n"
                "print('CLEAN_EXIT', flush=True)\n"
            )
            jid_usr2 = manager.start([sys.executable, "-u", "-c", app_usr2], label="command")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and "HANDLER_INSTALLED" not in json.loads(manager.read(jid_usr2, 0))["output"]:
                time.sleep(0.02)
            desc = json.loads(manager.describe(jid_usr2))
            self.assertEqual(desc["status"], "running")
            self.assertNotIn("current_stack", desc.get("live_diag", {}))
            yielded = json.loads(manager.wait(jid_usr2))
            self.assertEqual(yielded["status"], "running")
            gate.touch()
            done = json.loads(manager.wait(jid_usr2, wait_s=5))
            self.assertEqual(done["status"], "succeeded")
            self.assertEqual(done["exit_code"], 0)
            self.assertIn("CLEAN_EXIT", done["output"])

    def test_incremental_utf8_split_across_yield_preserves_multibyte_char(self):
        from pathlib import Path

        with tempfile.TemporaryDirectory() as gate_dir:
            gate = Path(gate_dir) / "rest"
            writer = (
                "import pathlib, sys, time\n"
                "sys.stdout.buffer.write(b'a' * 5000 + b'\\xe4')\n"
                "sys.stdout.buffer.flush()\n"
                f"g = pathlib.Path({str(gate)!r})\n"
                "deadline = time.monotonic() + 5\n"
                "while not g.exists() and time.monotonic() < deadline:\n"
                "    time.sleep(0.02)\n"
                "sys.stdout.buffer.write(b'\\xb8\\xad\\n')\n"
                "sys.stdout.buffer.flush()\n"
            )
            manager = JobManager(foreground_yield_s=0.18, wait_timeout_s=2, live_diag=False)
            self.addCleanup(manager.close, "test_utf8_split")
            jid = manager.start([sys.executable, "-u", "-c", writer], label="command")
            first = json.loads(manager.wait_foreground(jid))
            self.assertEqual(first["status"], "running")
            self.assertTrue(first["yielded"])
            self.assertEqual(first["output"], "a" * 5000)
            self.assertNotIn("\ufffd", first["output"])
            self.assertEqual(first["next_offset"], 5000)

            gate.touch()
            second = json.loads(manager.wait(jid, wait_s=5))
            self.assertEqual(second["status"], "succeeded")
            self.assertEqual(second["output"], "中\n")
            self.assertEqual(first["output"] + second["output"], "a" * 5000 + "中\n")


if __name__ == "__main__":
    unittest.main()
