"""优先级、通知与调度策略；用同步事件验证顺序，不做耗时性能推断。"""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from threading import Barrier, Event, Thread, get_ident
from unittest.mock import Mock, patch

from agent import run_agent
from events import EventSink
from jobs import JobManager
from runtime_input import InputInbox
from scheduler import ToolScheduler
from test_agent import assistant, request
from test_reviewer import recommendation
from tool_registry import Parameter, Tool, ToolRegistry


def notification(job_id="job-1"):
    return {"job_id": job_id, "status": "succeeded", "exit_code": 0, "output_excerpt": "结果已完成", "output_truncated": False}


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_unified_queue_priorities_fifo_and_deduplication(self):
        inbox = InputInbox()
        inbox.notify_job(notification())
        inbox.submit("旧要求")
        inbox.send_now("新要求")
        inbox.background()
        self.assertIsNone(inbox.notify_job(notification()))
        items = inbox.drain_events()
        self.assertEqual([i["priority"] for i in items], ["now", "now", "next", "later"])
        self.assertEqual([i["kind"] for i in items], ["user", "control", "user", "notification"])
        self.assertTrue(inbox.seal_if_empty())
        self.assertIsNone(inbox.notify_job(notification("job-2")))

    def test_urgent_dispatch_does_not_reverse_user_semantic_order(self):
        inbox = InputInbox()
        inbox.submit("先要求 A")
        inbox.send_now("改为 B")

        def model(messages, tools, **kwargs):
            self.assertEqual([m["content"] for m in messages[1:]], ["原任务", "先要求 A", "改为 B"])
            return assistant(content="按最新 B 回答")

        with patch("agent.chat", side_effect=model):
            report = run_agent("原任务", inbox=inbox, review=False, on_event=lambda e: None)
        self.assertEqual(report["task_version"], 2)

    def test_notification_inflight_changes_evidence_not_task_and_old_text_not_delivered(self):
        inbox, count = InputInbox(), 0

        def model(messages, tools, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                inbox.notify_job(notification())
                return assistant(content="检查仍在运行")
            self.assertEqual(messages[-1]["name"], "runtime")
            self.assertIn('"exit_code": 0', messages[-1]["content"])
            return assistant(content="已取得检查成功通知")

        with patch("agent.chat", side_effect=model):
            report = run_agent("查看状态", inbox=inbox, review=False, on_event=lambda e: None)
        self.assertEqual(report["answer"], "已取得检查成功通知")
        self.assertEqual(report["task_version"], 0)
        self.assertEqual(report["answer_evidence_version"], 1)
        self.assertEqual(len(report["notifications"]), 1)

    def test_notification_during_review_invalidates_old_advice_and_reviewer_sees_new_fact(self):
        inbox, payloads = InputInbox(), []

        def reviewer(messages, tools, **kwargs):
            payloads.append(json.loads(messages[1]["content"]))
            if len(payloads) == 1:
                inbox.notify_job(notification())
            return recommendation()

        with patch("agent.chat", side_effect=[assistant(content="旧候选"), assistant(content="有新结果的候选"), assistant(content="新交付")]):
            with patch("reviewer.chat", side_effect=reviewer):
                report = run_agent("核对结果", inbox=inbox, on_event=lambda e: None)
        self.assertEqual(payloads[1]["task_notifications"], [notification()])
        self.assertEqual([r["evidence_version"] for r in report["reviews"]], [0, 1])
        self.assertEqual([e["evidence_version"] for e in report["events"] if e["type"] == "review.feedback"], [1])
        self.assertEqual(report["answer_evidence_version"], 1)

    def test_notification_not_starved_by_user_messages_and_cannot_sneak_past_delivery(self):
        class Racing(InputInbox):
            def seal_if_empty(self):
                if not hasattr(self, "once"):
                    self.once = True
                    self.notify_job(notification())
                return super().seal_if_empty()

        inbox = Racing()
        for i in range(4):
            inbox.submit(str(i))
        with patch("agent.chat", side_effect=[assistant(content="旧交付"), assistant(content="包含新事实的交付")]):
            report = run_agent("原任务", inbox=inbox, review=False, on_event=lambda e: None)
        self.assertEqual(report["answer"], "包含新事实的交付")
        self.assertEqual(report["task_version"], 4)
        self.assertEqual(report["answer_evidence_version"], 1)

    def test_terminal_result_covers_notification_without_duplicate_context(self):
        jobs = JobManager(checks_command=[sys.executable, "-c", "print('ok')"])
        self.addCleanup(jobs.close, "test_cleanup")
        inbox = InputInbox()
        replies = [assistant(request("run", "run_project_checks", {"mode": "foreground"})), assistant(content="已完成")]
        with patch("agent.chat", side_effect=replies):
            report = run_agent("检查", inbox=inbox, jobs=jobs, review=False, on_event=lambda e: None)
        self.assertEqual([i["status"] for i in report["notifications"]], ["covered_by_tool_result"])
        self.assertEqual(report["evidence_version"], 0)
        self.assertFalse(any(m.get("name") == "runtime" for m in report["messages"]))

    def test_shared_handlers_overlap_but_messages_remain_in_original_order(self):
        rendezvous = Barrier(2)
        main_thread, events_on = get_ident(), []

        def read(name):
            self.assertNotEqual(get_ident(), main_thread)
            rendezvous.wait(timeout=2)
            return name

        registry = ToolRegistry([Tool("read", "读取", {"name": Parameter("名称")}, read, concurrency="shared")])
        replies = [assistant(request("a", "read", {"name": "A"}), request("b", "read", {"name": "B"})), assistant(content="完成")]
        with patch("agent.REGISTRY", registry), patch("agent.chat", side_effect=replies):
            report = run_agent("读两项", review=False, on_event=lambda e: events_on.append(get_ident()))
        self.assertEqual(report["status"], "answered")
        self.assertEqual([m["tool_call_id"] for m in report["messages"] if m["role"] == "tool"], ["a", "b"])
        self.assertEqual(set(events_on), {main_thread})

    def test_shared_group_finishes_before_exclusive_and_later_read_sees_write(self):
        done, state = set(), []

        def read_before(name):
            done.add(name)
            return name

        def write():
            self.assertEqual(done, {"A", "B"})
            state.append("written")
            return "写完"

        def read_after():
            self.assertEqual(state, ["written"])
            return "看到修改"

        registry = ToolRegistry([Tool("read", "读取", {"name": Parameter("名称")}, read_before, concurrency="shared"),
                                 Tool("write", "独占动作", {}, write),
                                 Tool("after", "读取新状态", {}, read_after, concurrency="shared")])
        calls = [request("a", "read", {"name": "A"}), request("b", "read", {"name": "B"}),
                 request("w", "write", {}), request("c", "after", {})]
        result = ToolScheduler(registry, EventSink(lambda e: None)).run(calls)
        self.assertEqual(result, ["A", "B", "写完", "看到修改"])

    def test_cooperative_cancel_stops_running_shared_work_and_skips_unstarted(self):
        inbox, started = InputInbox(), Event()
        skipped = Mock(return_value="不应执行")

        def blocking(*, cancellation):
            started.set()
            self.assertTrue(cancellation.wait(2))
            return "错误：已响应取消"

        def producer():
            if started.wait(2):
                inbox.send_now("改方向")

        worker = Thread(target=producer)
        worker.start()
        self.addCleanup(worker.join, 2)
        registry = ToolRegistry([Tool("blocking", "可取消", {}, blocking, concurrency="shared", interrupt="cancel"),
                                 Tool("later", "后续", {}, skipped)])
        events = EventSink(lambda e: None)
        results = ToolScheduler(registry, events, urgent=inbox.urgent).run([request("a", "blocking", {}), request("b", "later", {})])
        skipped.assert_not_called()
        self.assertIn("响应取消", results[0])
        self.assertIn("尚未启动", results[1])
        self.assertTrue(any(e["type"] == "tool.cancel_requested" for e in events.events))

    def test_control_lane_cancels_job_even_when_wait_is_earlier_in_same_batch(self):
        jobs = JobManager(checks_command=[sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(jobs.close, "test_cleanup")
        registry = ToolRegistry(jobs.tools())
        job_id = json.loads(jobs.run_checks("background"))["job_id"]
        events = EventSink(lambda e: None)
        result = ToolScheduler(registry, events, jobs=jobs).run([request("wait", "wait_job", {"job_id": job_id}),
                                                               request("cancel", "cancel_job", {"job_id": job_id})])
        self.assertEqual([json.loads(r)["status"] for r in result], ["cancelled", "cancelled"])
        completed = [e["call_id"] for e in events.events if e["type"] == "tool.completed"]
        self.assertEqual(completed, ["cancel", "wait"])

    def test_wait_policy_defers_interruption_until_handler_finishes(self):
        inbox, started, release = InputInbox(), Event(), Event()
        deferred = []

        def blocking():
            started.set()
            self.assertTrue(release.wait(2))
            return "有界工作完成"

        def producer():
            if started.wait(2):
                inbox.send_now("新要求")

        def listener(event):
            if event["type"] == "tool.interrupt_deferred":
                deferred.append(event["call_id"])
                release.set()

        worker = Thread(target=producer)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(release.set)
        registry = ToolRegistry([Tool("wait", "有界等待", {}, blocking, concurrency="shared", interrupt="wait")])
        result = ToolScheduler(registry, EventSink(listener), urgent=inbox.urgent).run([request("a", "wait", {})])
        self.assertEqual(result, ["有界工作完成"])
        self.assertEqual(deferred, ["a"])

    def test_new_fact_at_budget_limit_is_not_delivered_as_old_answer(self):
        inbox = InputInbox()

        def model(messages, tools, **kwargs):
            inbox.notify_job(notification())
            return assistant(content="旧状态")

        with patch("agent.chat", side_effect=model):
            report = run_agent("检查", inbox=inbox, review=False, max_calls=1, on_event=lambda e: None)
        self.assertEqual(report["status"], "limit_reached")
        self.assertEqual(report["model_requests"], 1)
        self.assertNotIn("answer", report)

    def test_worker_failure_does_not_execute_later_exclusive_action(self):
        later = Mock(return_value="不执行")
        def broken():
            raise RuntimeError("故障")
        registry = ToolRegistry([Tool("bad", "读取", {}, broken, concurrency="shared"), Tool("later", "独占", {}, later)])
        with self.assertRaises(RuntimeError):
            ToolScheduler(registry, EventSink(lambda e: None)).run([request("a", "bad", {}), request("b", "later", {})])
        later.assert_not_called()

    def test_bad_policy_and_parameter_reject_before_handler(self):
        for kwargs in ({"concurrency": "unknown"}, {"interrupt": "unknown"}, {"interrupt": "cancel"},
                       {"concurrency": "shared", "interrupt": "background"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Tool("x", "描述", {}, Mock(), **kwargs)
        handler = Mock(return_value="不应执行")
        registry = ToolRegistry([Tool("x", "描述", {"a": Parameter("参数")}, handler, concurrency="shared")])
        result = ToolScheduler(registry, EventSink(lambda e: None)).run([request("x", "x", {})])
        self.assertTrue(result[0].startswith("错误："))
        handler.assert_not_called()

    def test_notification_failure_retains_unapplied_fact_in_report(self):
        inbox = InputInbox()
        inbox.notify_job(notification())
        def listener(event):
            if event["type"] == "queue.consumed":
                raise RuntimeError("日志失败")
        with patch("agent.chat") as actor:
            report = run_agent("任务", inbox=inbox, review=False, on_event=listener)
        actor.assert_not_called()
        self.assertEqual(report["status"], "runtime_error")
        self.assertEqual(report["notifications"][0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
