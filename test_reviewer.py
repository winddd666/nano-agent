"""受控模型验证独立评审、建议回填和主智能体的最终决定权。"""

import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agent import run_agent
from reviewer import parse_recommendation, review_answer
from test_agent import assistant, request
from tool_registry import REGISTRY


def recommendation(value="accept", reason="材料支持当前回答"):
    return assistant(content=json.dumps({"recommendation": value, "reason": reason}, ensure_ascii=False))


def consultation(candidate="候选回答", call_id="review-1"):
    return request(call_id, "review_answer", {"candidate": candidate})


class ReviewTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_reviewer_has_own_history_and_can_read_real_files(self):
        evidence = [
            {"role": "system", "content": "执行者的私有提示"},
            assistant(request("list-1", "list_files", {})),
            {"role": "tool", "tool_call_id": "list-1", "content": "contact.txt"},
            assistant(consultation()),
            {"role": "tool", "tool_call_id": "review-1", "content": "上一位评审的建议"},
        ]
        before = copy.deepcopy(evidence)

        def model(messages, tools):
            self.assertEqual([t["function"]["name"] for t in tools], ["list_files", "read_file", "read_tool_result"])
            payload = json.loads(messages[1]["content"])
            self.assertEqual(payload["task"], "答疑去哪里？")
            self.assertEqual(payload["criteria"], ["给出答疑话题"])
            self.assertEqual(len(payload["tool_evidence"]), 2)
            self.assertNotIn("执行者的私有提示", messages[1]["content"])
            self.assertNotIn("上一位评审的建议", messages[1]["content"])
            if len(messages) == 2:
                return assistant(request("check-1", "read_file", {"name": "contact.txt"}))
            self.assertEqual(messages[-1]["tool_call_id"], "check-1")
            self.assertIn("Agent 实验答疑", messages[-1]["content"])
            return recommendation()

        with patch("reviewer.chat", side_effect=model):
            result = review_answer("答疑去哪里？", "去 Agent 实验答疑话题", evidence, ["给出答疑话题"])
        self.assertEqual(result["status"], "reviewed")
        self.assertEqual(result["model_requests"], 2)
        self.assertEqual(evidence, before)
        self.assertIsNot(result["messages"], evidence)

    def test_malformed_recommendations_are_errors_not_acceptance(self):
        values = [
            "不是 JSON", "[]", "null", "{}",
            '{"recommendation":"yes","reason":"好"}',
            '{"recommendation":"accept","reason":" "}',
            '{"recommendation":"accept","reason":true}',
            '{"recommendation":"accept","reason":"好","extra":1}',
        ]
        for content in values:
            with self.subTest(content=content), self.assertRaises(RuntimeError):
                parse_recommendation(assistant(content=content))

    def test_reviewer_can_see_cancel_evidence_without_cancel_capability(self):
        evidence = [assistant(request("cancel-one", "cancel_job", {"job_id": "job-1"})),
                    {"role": "tool", "tool_call_id": "cancel-one", "content": json.dumps({
                        "job_id": "job-1", "status": "cancelled", "exit_code": -15, "reason": "requested"})}]
        expected = [{"role": "assistant", "tool_calls": evidence[0]["tool_calls"]}, evidence[1]]

        def model(messages, tools):
            self.assertNotIn("cancel_job", [t["function"]["name"] for t in tools])
            payload = json.loads(messages[1]["content"])
            self.assertEqual(payload["tool_evidence"], expected)
            return recommendation()

        with patch("reviewer.chat", side_effect=model):
            result = review_answer("核对定向取消", "第一项已由 cancel_job 取消", evidence)
        self.assertEqual(result["status"], "reviewed")
        self.assertEqual(result["tool_evidence"], expected)

    def test_reviewer_cannot_delegate_recursively(self):
        replies = [assistant(consultation()), recommendation("cannot_verify", "没有递归评审能力")]
        with patch("reviewer.chat", side_effect=replies):
            result = review_answer("核查", "候选", [])
        self.assertTrue(result["messages"][-2]["content"].startswith("错误："))
        self.assertEqual(result["recommendation"], "cannot_verify")

    def test_reviewer_loop_is_bounded(self):
        with patch("reviewer.chat", return_value=assistant(request("list", "list_files", {}))) as model:
            result = review_answer("核查", "候选", [], max_calls=2)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(result["status"], "limit_reached")
        self.assertIsNone(result["recommendation"])
        self.assertEqual(result["messages"][-1]["role"], "tool")

    def test_bad_call_ids_stop_before_any_tool_executes(self):
        for calls in (
            [request("same", "list_files", {}), request("same", "list_files", {})],
            [{"function": {"name": "list_files", "arguments": "{}"}}],
        ):
            with self.subTest(calls=calls), patch("reviewer.chat", return_value=assistant(*calls)):
                with patch("reviewer.REVIEW_TOOLS.execute") as execute:
                    result = review_answer("核查", "候选", [])
            self.assertEqual(result["status"], "runtime_error")
            execute.assert_not_called()

    def test_api_failure_is_recorded_without_a_recommendation(self):
        with patch("reviewer.chat", side_effect=RuntimeError("评审网络故障")):
            result = review_answer("核查", "候选", [])
        self.assertEqual(result["status"], "runtime_error")
        self.assertEqual(result["model_requests"], 1)
        self.assertIsNone(result["recommendation"])

    def test_main_agent_can_decline_revision_advice_and_deliver(self):
        def model(messages, tools):
            self.assertNotIn("review_answer", [t["function"]["name"] for t in tools])
            if len(messages) == 2:
                return assistant(content="候选回答")
            self.assertEqual(messages[-1]["role"], "user")
            advice = json.loads(messages[-1]["content"].split("\n", 1)[1])
            self.assertTrue(advice["advisory"])
            self.assertEqual(advice["recommendation"], "revise")
            return assistant(content="评审要求增加无关材料，我保留原回答并说明依据。")

        with patch("agent.chat", side_effect=model) as actor:
            with patch("reviewer.chat", return_value=recommendation("revise", "增加另一门课程介绍")):
                result = run_agent("回答本课程问题")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(actor.call_count, 2)
        self.assertEqual(result["reviews"][0]["recommendation"], "revise")
        self.assertEqual(result["model_requests"], 3)
        self.assertNotIn("review_answer", [t["function"]["name"] for t in REGISTRY.schemas()])

    def test_accept_advice_ends_main_agent_immediately(self):
        replies = [assistant(content="候选回答"), assistant(content="不应再调用")]
        with patch("agent.chat", side_effect=replies) as actor:
            with patch("reviewer.chat", return_value=recommendation()):
                result = run_agent("核查答疑约定")
        self.assertEqual(actor.call_count, 1)
        self.assertEqual(result["answer"], "候选回答")
        self.assertEqual(len(result["reviews"]), 1)

    def test_tool_rounds_do_not_trigger_review_even_when_they_contain_text(self):
        events = []
        replies = iter([
            assistant(request("list", "list_files", {}), content="我准备结束了"),
            assistant(request("read", "read_file", {"name": "contact.txt"})),
            assistant(content="候选回答"),
        ])

        def actor(messages, tools):
            events.append("actor")
            return next(replies)

        def reviewer(messages, tools):
            events.append("reviewer")
            payload = json.loads(messages[1]["content"])
            self.assertEqual(payload["candidate"], "候选回答")
            self.assertEqual(len(payload["tool_evidence"]), 4)
            return recommendation()

        with patch("agent.chat", side_effect=actor), patch("reviewer.chat", side_effect=reviewer):
            result = run_agent("查答疑位置")
        self.assertEqual(events, ["actor", "actor", "actor", "reviewer"])
        self.assertEqual(result["answer"], "候选回答")

    def test_tool_work_after_advice_opens_a_new_candidate_review(self):
        replies = [
            assistant(content="第一份候选"),
            assistant(request("read", "read_file", {"name": "contact.txt"})),
            assistant(content="补查后的新候选"),
        ]
        review_replies = [recommendation("revise", "请先读取 contact.txt"), recommendation()]
        with patch("agent.chat", side_effect=replies):
            with patch("reviewer.chat", side_effect=review_replies) as reviewer:
                result = run_agent("查答疑位置")
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(result["model_requests"], 5)
        payload = json.loads(result["reviews"][1]["messages"][1]["content"])
        self.assertEqual(payload["candidate"], "补查后的新候选")
        self.assertIn("Agent 实验答疑", payload["tool_evidence"][1]["content"])

    def test_main_agent_can_use_advice_to_repair_answer(self):
        actor_replies = [
            assistant(request("schedule", "read_file", {"name": "schedule.txt"})),
            assistant(content="只需要带电脑"),
            assistant(content="准备电脑、验证模型 API、阅读工具接口说明。依据 schedule.txt。"),
        ]
        with patch("agent.chat", side_effect=actor_replies):
            with patch("reviewer.chat", return_value=recommendation("revise", "遗漏 API 验证和接口说明")):
                result = run_agent("准备什么？", criteria=["覆盖 Agent 实验课所有准备事项"])
        payload = json.loads(result["reviews"][0]["messages"][1]["content"])
        self.assertEqual(payload["criteria"], result["criteria"])
        self.assertEqual(len(payload["tool_evidence"]), 2)
        self.assertIn("提前验证一次模型 API 请求", payload["tool_evidence"][1]["content"])
        self.assertEqual(result["actor_requests"], 3)
        self.assertEqual(result["reviewer_requests"], 1)
        self.assertEqual(result["model_requests"], 4)

    def test_reviewer_budget_is_independent_of_actor_budget(self):
        actor_replies = [assistant(content="候选回答"), assistant(content="评审未能完成，我据已有证据交付并注明限制。")]
        with patch("agent.chat", side_effect=actor_replies):
            with patch("reviewer.chat", return_value=assistant(request("list", "list_files", {}))) as reviewer:
                result = run_agent("整理材料", max_calls=2, max_review_calls=3)
        self.assertEqual(reviewer.call_count, 3)
        self.assertEqual(result["actor_requests"], 2)
        self.assertEqual(result["reviewer_requests"], 3)
        self.assertEqual(result["model_requests"], 5)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["reviews"][0]["status"], "limit_reached")
        self.assertIsNone(json.loads(result["messages"][-2]["content"].split("\n", 1)[1])["recommendation"])

    def test_candidate_at_last_actor_slot_can_still_be_reviewed_and_accepted(self):
        with patch("agent.chat", return_value=assistant(content="最后一步的候选")):
            with patch("reviewer.chat", return_value=recommendation()) as reviewer:
                result = run_agent("整理材料", max_calls=1)
        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "最后一步的候选")

    def test_revise_at_last_actor_slot_hits_limit_reached(self):
        with patch("agent.chat", return_value=assistant(content="尚未完善的候选")):
            with patch("reviewer.chat", return_value=recommendation("revise", "缺少依据")) as reviewer:
                result = run_agent("整理材料", max_calls=1)
        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(result["status"], "limit_reached")
        self.assertEqual(result["candidate_answer"], "尚未完善的候选")
        self.assertNotIn("answer", result)

    def test_review_count_limit_returns_advice_error_without_stopping_main(self):
        replies = [assistant(content="第一份候选"),
                   assistant(request("read", "read_file", {"name": "contact.txt"})),
                   assistant(content="新候选"), assistant(content="主智能体最终回答")]
        with patch("agent.chat", side_effect=replies):
            with patch("reviewer.chat", return_value=recommendation("revise", "请先读取文件")) as reviewer:
                result = run_agent("整理材料", max_reviews=1)
        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(len(result["reviews"]), 1)
        self.assertEqual(json.loads(result["messages"][-2]["content"].split("\n", 1)[1])["status"], "unavailable")
        self.assertEqual(result["status"], "answered")

    def test_invalid_review_response_is_returned_to_main_as_error(self):
        with patch("agent.chat", side_effect=[assistant(content="候选回答"), assistant(content="说明评审故障后交付")]):
            with patch("reviewer.chat", return_value=assistant(content="随意批准")):
                result = run_agent("整理材料")
        advice = json.loads(result["messages"][-2]["content"].split("\n", 1)[1])
        self.assertEqual(advice["status"], "runtime_error")
        self.assertIsNone(advice["recommendation"])
        self.assertEqual(result["status"], "answered")

    def test_user_interrupt_during_review_still_stops_the_run(self):
        with patch("agent.chat", return_value=assistant(content="候选回答")) as actor:
            with patch("reviewer.chat", side_effect=KeyboardInterrupt):
                result = run_agent("整理材料")
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["model_requests"], 2)
        self.assertEqual(actor.call_count, 1)

    def test_empty_response_does_not_trigger_review(self):
        with patch("agent.chat", return_value=assistant()):
            with patch("reviewer.chat") as reviewer:
                result = run_agent("整理材料")
        reviewer.assert_not_called()
        self.assertEqual(result["status"], "empty_response")

    def test_disabling_review_keeps_original_delivery_behavior(self):
        with patch("agent.chat", return_value=assistant(content="直接交付")) as actor:
            with patch("reviewer.chat") as reviewer:
                result = run_agent("整理材料", review=False, max_calls=1)
        reviewer.assert_not_called()
        self.assertEqual(actor.call_count, 1)
        self.assertEqual(result["status"], "answered")


if __name__ == "__main__":
    unittest.main()
