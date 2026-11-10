"""真实模型发起定向取消：两个真实进程，取消一个，另一个继续完成。"""

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from agent import run_agent
from events import JsonlEvents
from jobs import JobManager


# 控制夹具：实际读取材料并计算摘要，然后等待门文件。
# 第一项被模型取消后，宿主只释放门文件，允许另一项完成；不伪造任何模型或进程结果。
PROCESS = """
import hashlib, json, sys, time
from pathlib import Path
root, gate = map(Path, sys.argv[1:])
facts = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob('*.txt'))}
print(json.dumps({'material_sha256': facts}, ensure_ascii=False), flush=True)
while not gate.exists():
    time.sleep(0.02)
print('材料检查进程正常结束。', flush=True)
"""
TASK = (
    "验证定向取消：用 run_project_checks 的 background 模式启动恰好两项任务。"
    "本实验的工具由宿主配置为真实读取材料后等待门文件的子进程。"
    "第一项现在不再需要，请由你调用取消工具取消第一项；第二项仍然需要，不能取消。"
    "宿主会在第一项确认取消后允许第二项继续结束。"
    "等待并读取第二项的真实结果，最后报告两项的任务编号、状态及退出码。"
    "不要重新启动任务，不要把运行结束时的自动清理当成你主动取消的证据。"
)


def verify(report):
    assert report["status"] == "answered", report.get("error", report["status"])
    assert list(report["jobs"]) == ["job-1", "job-2"]
    cancelled, completed = report["jobs"]["job-1"], report["jobs"]["job-2"]
    assert cancelled["status"] == "cancelled" and cancelled["reason"] == "requested"
    assert completed["status"] == "succeeded" and completed["exit_code"] == 0
    assert "材料检查进程正常结束。" in completed["output"]
    calls = [c for m in report["messages"] for c in m.get("tool_calls", [])
             if c["function"]["name"] == "cancel_job"]
    assert len(calls) == 1 and json.loads(calls[0]["function"]["arguments"]) == {"job_id": "job-1"}
    events = report["events"]
    start = next(e for e in events if e["type"] == "tool.started" and e["call_id"] == calls[0]["id"])
    end = next(e for e in events if e["type"] == "tool.completed" and e["call_id"] == calls[0]["id"])
    first = next(e for e in events if e["type"] == "job.completed" and e["job_id"] == "job-1")
    second = next(e for e in events if e["type"] == "job.completed" and e["job_id"] == "job-2")
    assert start["seq"] < first["seq"] < end["seq"]
    assert first["seq"] < second["seq"]
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
    return {"cancel_call_id": calls[0]["id"], "cancelled_job": cancelled,
            "completed_job": completed, "checks": ["model emitted cancel_job", "cancellation occurred inside tool call",
                                                      "sibling completed normally", "tool ids paired"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-calls", type=int, default=12)
    parser.add_argument("--no-stream", action="store_true")
    args = parser.parse_args()
    if args.max_calls < 1:
        parser.error("--max-calls 必须大于 0")
    root = Path(__file__).resolve().parent
    stem = "cancel-experiment-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path, event_path = root / "runs" / (stem + ".json"), root / "runs" / (stem + ".events.jsonl")
    with tempfile.TemporaryDirectory(prefix="nano-cancel-experiment-") as directory:
        gate = Path(directory) / "finish"
        manager = JobManager(checks_command=[sys.executable, "-u", "-c", PROCESS, str(root / "sandbox"), str(gate)], timeout=60)
        with JsonlEvents(event_path) as listener:
            def observe(event):
                listener(event)
                if event["type"] == "job.completed" and event["job_id"] == "job-1" and event["status"] == "cancelled":
                    gate.touch()
            report = run_agent(TASK, jobs=manager, stream=not args.no_stream, on_event=observe,
                               max_calls=args.max_calls,
                               criteria=["两项任务的最终状态和退出码有实际工具结果支持，第一项由取消工具停止，第二项正常完成。"])
    report["event_log"] = str(event_path)
    names = ["agent.py", "jobs.py", "model.py", "events.py", "control.py", "runtime_input.py",
             "tool_registry.py", "reviewer.py", "context.py", "scheduler.py", "cancel_experiment.py"]
    report["experiment"] = {"mode": "live", "fixture": PROCESS, "task": TASK,
                            "description": "取消意图是预设任务；模型响应和进程结果真实。门文件只控制两项进程的完成顺序。",
                            "source_sha256": {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in names}}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[轨迹] {path}")
    checks = verify(report)
    assert [json.loads(line) for line in event_path.read_text().splitlines()] == report["events"]
    summary = {"report": str(path), "model_requests": report["model_requests"], **checks}
    audit_path = root / "runs" / (stem + ".verification.json")
    audit_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[定向取消核查通过] {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
