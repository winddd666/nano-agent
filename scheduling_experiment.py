"""真实模型接收后台完成通知，并与材料读取共同完成任务。"""

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from agent import run_agent
from cancel_experiment import PROCESS
from events import JsonlEvents
from jobs import JobManager
from runtime_input import InputInbox


TASK = (
    "先用 run_project_checks 的 background 模式启动恰好一项后台检查。"
    "本实验宿主配置为真实读取材料后等待门文件的子进程，第二次主模型响应开始输出时会放行。"
    "然后实际读取 schedule.txt 与 contact.txt，总结 Agent 实验课准备事项、现场任务和答疑位置，标注依据。"
    "运行时会将任务完成通知及退出码送入上下文。不要使用 job_status、wait_job 或 read_job_output 轮询；"
    "不要重复启动检查。取得程序完成通知后，一并报告检查最终状态与退出码。"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-calls", type=int, default=12)
    args = parser.parse_args()
    if args.max_calls < 1:
        parser.error("--max-calls 必须大于 0")
    root = Path(__file__).resolve().parent
    stem = "scheduling-experiment-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path, event_path = root / "runs" / (stem + ".json"), root / "runs" / (stem + ".events.jsonl")
    release_seq = None
    with tempfile.TemporaryDirectory(prefix="nano-scheduling-") as directory:
        gate = Path(directory) / "finish"
        manager = JobManager(checks_command=[sys.executable, "-u", "-c", PROCESS, str(root / "sandbox"), str(gate)], timeout=60)
        with JsonlEvents(event_path) as listener:
            def observe(event):
                nonlocal release_seq
                listener(event)
                if (release_seq is None and manager.records and event["scope"] == "actor"
                        and event.get("request") == 2 and event["type"] in ("model.text.delta", "model.tool.delta")):
                    release_seq = event["seq"]
                    gate.touch()
            report = run_agent(TASK, jobs=manager, inbox=InputInbox(), stream=True, on_event=observe,
                               max_calls=args.max_calls,
                               criteria=["材料总结有实际读取与文件依据，检查状态和退出码来自实际程序完成通知。"])
    report["event_log"] = str(event_path)
    names = ["agent.py", "jobs.py", "scheduler.py", "runtime_input.py", "tool_registry.py", "model.py",
             "events.py", "context.py", "reviewer.py", "cancel_experiment.py", "scheduling_experiment.py"]
    report["experiment"] = {"mode": "live", "fixture": PROCESS, "release_seq": release_seq,
                            "description": "模型与工具结果不模拟；门文件在第二次模型流中释放，仅控制完成时序。",
                            "source_sha256": {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in names}}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[轨迹] {path}")
    assert report["status"] == "answered", report.get("error", report["status"])
    assert report["task_version"] == 0 and report["answer_evidence_version"] == 1
    assert list(report["jobs"]) == ["job-1"] and report["jobs"]["job-1"]["status"] == "succeeded"
    applied = [item for item in report["notifications"] if item["status"] == "applied"]
    assert len(applied) == 1 and applied[0]["payload"]["exit_code"] == 0
    calls = [call for m in report["messages"] for call in m.get("tool_calls", [])]
    assert not any(c["function"]["name"] in ("wait_job", "job_status", "read_job_output") for c in calls)
    reads = {c["id"]: json.loads(c["function"]["arguments"])["name"] for c in calls if c["function"]["name"] == "read_file"}
    assert set(reads.values()) >= {"schedule.txt", "contact.txt"}
    for message in report["messages"]:
        if message["role"] == "tool" and message["tool_call_id"] in reads:
            assert message["content"] == (root / "sandbox" / reads[message["tool_call_id"]]).read_text()
    for history in [report["messages"]] + [r["messages"] for r in report["reviews"]]:
        pending = []
        for message in history:
            if message["role"] == "tool":
                assert message["tool_call_id"] in pending
                pending.remove(message["tool_call_id"])
            else:
                assert not pending
                pending = [c["id"] for c in message.get("tool_calls", [])]
        assert not pending
    assert any(r["task_notifications"] and r["evidence_version"] == 1 for r in report["reviews"])
    events = report["events"]
    assert [json.loads(line) for line in event_path.read_text().splitlines()] == events
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert release_seq < next(e["seq"] for e in events if e["type"] == "notification.applied")
    # 参数就绪事件和执行策略可追踪；不把观察时间当作并发速度测量。
    assert all(e["boundary"] == "response_complete" for e in events if e["type"] == "tool.ready")
    summary = {"report": str(path), "model_requests": report["model_requests"], "actor_requests": report["actor_requests"],
               "reviewer_requests": report["reviewer_requests"], "notifications": applied,
               "checks": ["terminal notification applied exactly once", "no model polling", "task and evidence versions separate",
                          "reviewer saw runtime evidence", "real file results match source", "tool ids paired", "JSONL matches report",
                          "tool start gated on response completion"], "answer": report["answer"]}
    audit_path = root / "runs" / (stem + ".verification.json")
    audit_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[调度核查通过] {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
