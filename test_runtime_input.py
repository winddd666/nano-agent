"""运行中输入：真实队列/线程、受控模型，以及交付与版本边界。"""

import json
import os
import unittest
from copy import deepcopy
from threading import Event, Thread
from unittest.mock import patch

from agent import run_agent
from runtime_input import InputInbox, StdinInput
from test_agent import assistant, request
from test_reviewer import recommendation
from tool_registry import Tool, ToolRegistry


class RuntimeInputTests(unittest.TestCase):
    def run_actor(self, inbox, model, **kwargs):
        with patch("agent.chat", side_effect=model):
            return run_agent("原任务", inbox=inbox, on_event=lambda e: None, **kwargs)

    def test_receive_during_blocking_generation_and_apply_before_next_request(self):
        inbox, generating, submitted = InputInbox(), Event(), Event()
        histories = []

        def producer():
            if generating.wait(2):
                inbox.submit("只回答答疑位置")
                submitted.set()

        thread = Thread(target=producer)
        thread.start()
        self.addCleanup(thread.join, 2)

        def model(messages, tools, **kwargs):
            histories.append(deepcopy(messages))
            if len(histories) == 1:
                generating.set()
                self.assertTrue(submitted.wait(2), "输入应能在模型请求尚未返回时接收")
                return assistant(content="旧回答")
            self.assertEqual(messages[-1], {"role": "user", "content": "只回答答疑位置"})
            return assistant(content="新回答")

        report = self.run_actor(inbox, model, review=False)
        self.assertEqual(report["answer"], "新回答")
        self.assertEqual(report["answer_version"], 1)
        self.assertEqual(len(histories[0]), 2)
        self.assertIn("旧回答", [m.get("content") for m in report["messages"]])
        self.assertTrue(any(e["type"] == "output.superseded" for e in report["events"]))

    def test_input_in_stream_does_not_modify_inflight_history_or_publish_old_text(self):
        inbox = InputInbox()
        count = 0

        def model(messages, tools, *, stream, on_event, **kwargs):
            nonlocal count
            count += 1
            before = deepcopy(messages)
            on_event({"type": "text.delta", "text": "旧" if count == 1 else "新"})
            if count == 1:
                inbox.submit("更正任务")
            self.assertEqual(messages, before)
            return assistant(content="旧" if count == 1 else "新")

        report = self.run_actor(inbox, model, review=False, stream=True)
        self.assertEqual(report["answer"], "新")
        self.assertEqual([e["content"] for e in report["events"] if e["type"] == "answer"], ["新"])

    def test_tools_complete_as_a_batch_before_user_message_is_applied(self):
        inbox, executed = InputInbox(), []

        def first():
            inbox.submit("再给我来源")
            executed.append("first")
            return "第一项结果"

        def second():
            executed.append("second")
            return "第二项结果"

        registry = ToolRegistry([Tool("first", "第一项", {}, first), Tool("second", "第二项", {}, second)])

        def model(messages, tools, **kwargs):
            if len(messages) == 2:
                return assistant(request("a", "first", {}), request("b", "second", {}))
            self.assertEqual([m["role"] for m in messages[-4:]], ["assistant", "tool", "tool", "user"])
            self.assertEqual([m["tool_call_id"] for m in messages[-3:-1]], ["a", "b"])
            return assistant(content="带来源的回答")

        with patch("agent.REGISTRY", registry):
            report = self.run_actor(inbox, model, review=False)
        self.assertEqual(executed, ["first", "second"])
        self.assertEqual(report["status"], "answered")
        event = next(e for e in report["events"] if e["type"] == "input.applied")
        self.assertEqual(event["boundary"], "after_tools")

    def test_input_during_review_discards_old_feedback_and_reviews_new_version(self):
        inbox, tasks = InputInbox(), []
        actor = [assistant(content="旧候选"), assistant(content="新候选"), assistant(content="最终交付")]

        def reviewer(messages, tools, **kwargs):
            tasks.append(json.loads(messages[1]["content"])["task"])
            if len(tasks) == 1:
                inbox.submit("把来源也附上")
            return recommendation()

        with patch("reviewer.chat", side_effect=reviewer):
            report = self.run_actor(inbox, actor)
        self.assertEqual(report["answer_version"], 1)
        self.assertEqual([r["task_version"] for r in report["reviews"]], [0, 1])
        self.assertEqual(json.loads(tasks[1])["user_updates_in_order"], ["把来源也附上"])
        feedback = [e for e in report["events"] if e["type"] == "review.feedback"]
        self.assertEqual([e["task_version"] for e in feedback], [1])
        self.assertEqual(report["model_requests"], 4)

    def test_input_during_final_decision_resets_previous_review(self):
        inbox, count = InputInbox(), 0

        def model(messages, tools, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                inbox.submit("增加一个要求")
            return assistant(content=f"回答 {count}")

        with patch("reviewer.chat", side_effect=[recommendation("revise", "请补充"), recommendation()]) as reviewer:
            report = self.run_actor(inbox, model)
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(report["answer"], "回答 3")
        self.assertEqual(report["model_requests"], 5)

    def test_delivery_seal_detects_last_moment_submit_and_rejects_late_input(self):
        class RacingInbox(InputInbox):
            def seal_if_empty(self):
                if not hasattr(self, "injected"):
                    self.injected = True
                    self.submit("交付前最后补充")
                return super().seal_if_empty()

        inbox = RacingInbox()
        report = self.run_actor(inbox, [assistant(content="旧交付"), assistant(content="新交付")], review=False)
        self.assertEqual(report["answer"], "新交付")
        self.assertEqual(report["input_messages"][0]["status"], "applied")
        with self.assertRaises(RuntimeError):
            inbox.submit("已交付之后的消息")

    def test_multiple_queued_inputs_keep_order_and_get_distinct_versions(self):
        inbox = InputInbox()
        inbox.submit("先加来源")
        inbox.submit("再限制三句话")

        def model(messages, tools, **kwargs):
            self.assertEqual([m["content"] for m in messages[1:]], ["原任务", "先加来源", "再限制三句话"])
            return assistant(content="新交付")

        report = self.run_actor(inbox, model, review=False)
        self.assertEqual(report["task_version"], 2)
        self.assertEqual([m["id"] for m in report["input_messages"]], [1, 2])

    def test_budget_exhaustion_does_not_deliver_old_answer_or_reset_budget(self):
        inbox = InputInbox()

        def model(messages, tools, **kwargs):
            inbox.submit("更正要求")
            return assistant(content="旧回答")

        report = self.run_actor(inbox, model, review=False, max_calls=1)
        self.assertEqual(report["status"], "limit_reached")
        self.assertEqual(report["model_requests"], 1)
        self.assertNotIn("answer", report)

    def test_failure_records_unapplied_input_and_closes_inbox(self):
        inbox = InputInbox()

        def model(messages, tools, **kwargs):
            inbox.submit("请求过程中补充")
            raise RuntimeError("网络失败")

        report = self.run_actor(inbox, model, review=False)
        self.assertEqual(report["status"], "runtime_error")
        self.assertEqual(report["input_messages"][0]["status"], "pending")
        self.assertEqual(report["task_version"], 0)
        self.assertNotIn("请求过程中补充", [m.get("content") for m in report["messages"]])
        with self.assertRaises(RuntimeError):
            inbox.submit("晚到消息")

    def test_reader_failure_stops_instead_of_delivering(self):
        inbox = InputInbox()

        def model(messages, tools, **kwargs):
            inbox.fail("输入通道失效")
            return assistant(content="未交付")

        report = self.run_actor(inbox, model, review=False)
        self.assertEqual(report["status"], "runtime_error")
        self.assertNotIn("answer", report)

    def test_receipt_logging_failure_retains_all_accepted_messages_as_pending(self):
        inbox = InputInbox()

        def model(messages, tools, **kwargs):
            inbox.submit("第一条")
            inbox.submit("第二条")
            return assistant(content="旧回答")

        def listener(event):
            if event["type"] == "input.received":
                raise RuntimeError("输入事件保存失败")

        with patch("agent.chat", side_effect=model):
            report = run_agent("原任务", inbox=inbox, review=False, on_event=listener)
        self.assertEqual(report["status"], "runtime_error")
        self.assertEqual([item["text"] for item in report["input_messages"]], ["第一条", "第二条"])
        self.assertEqual([item["status"] for item in report["input_messages"]], ["pending", "pending"])
        self.assertEqual(report["task_version"], 0)
        self.assertNotIn("answer", report)

    def test_stdin_pipe_receives_lines_utf8_and_eof_without_blocking_shutdown(self):
        received = Event()

        class ObservedInbox(InputInbox):
            def submit(self, text):
                result = super().submit(text)
                if result == 2:
                    received.set()
                return result

        inbox = ObservedInbox()
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, encoding="utf-8") as stream:
            with StdinInput(inbox, stream) as reader:
                os.write(write_fd, "第一条\n\n第二条".encode())
                os.close(write_fd)
                self.assertTrue(received.wait(2))
            self.assertFalse(reader.thread.is_alive())
        self.assertEqual([item["text"] for item in inbox.drain()], ["第一条", "第二条"])
        self.assertTrue(inbox.seal_if_empty())

    def test_stdin_reader_can_stop_while_pipe_is_still_open(self):
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(read_fd, encoding="utf-8") as stream:
                with StdinInput(InputInbox(), stream) as reader:
                    pass
                self.assertFalse(reader.thread.is_alive())
        finally:
            os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
