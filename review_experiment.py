"""真实模型试跑：完整材料任务，或单独评审已知错误/正确的候选答案。"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from agent import DEFAULT_TASK, run_agent
from events import EventSink, JsonlEvents
from reviewer import review_answer


BAD_ANSWER = (
    "Agent 实验课是线上讨论，提前完成函数与字典练习即可。"
    "现场不用保存工具调用记录；有问题到“Python 基础”话题提问。"
)
GOOD_ANSWER = (
    "提前准备：携带能运行 Python 的电脑，提前验证一次模型 API 请求，"
    "阅读群文件中的工具接口说明。现场任务：实现能查询本地材料的简单 Agent，"
    "并保存一次工具调用记录。以上依据 schedule.txt，Agent 实验课采用现场实践。"
    "遇到问题去学习群的“Agent 实验答疑”话题，附上最小复现步骤、完整错误信息"
    "和已尝试的处理方法，不发送模型 API 密钥。答疑约定依据 contact.txt。"
)
EXPERIMENT_TASK = (
    DEFAULT_TASK + "\n有人给了下面这份说明，请依据实际材料逐项核对，指出错误，"
    "再给出更正后的说明。不能直接采信这份现成说明：\n" + BAD_ANSWER
)


def main():
    parser = argparse.ArgumentParser(description="用真实模型观察交付前评审及正确/错误答案的建议。")
    parser.add_argument("--case", choices=("task", "bad", "good"), default="task",
                        help="task：完整主循环；bad/good：单独评审预设错误/正确答案")
    parser.add_argument("--max-calls", type=int, default=12, help="本次模型请求总上限")
    parser.add_argument("--no-stream", action="store_true", help="使用非流式模型请求")
    args = parser.parse_args()
    if args.max_calls < 1:
        parser.error("--max-calls 必须大于 0")

    runs = Path(__file__).resolve().parent / "runs"
    runs.mkdir(exist_ok=True)
    stem = f"experiment-{args.case}-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = runs / (stem + ".json")
    event_path = runs / (stem + ".events.jsonl")
    print(f"[真实模型实验] case={args.case}", flush=True)
    with JsonlEvents(event_path) as listener:
        if args.case == "task":
            print(f"[任务]\n{EXPERIMENT_TASK}", flush=True)
            report = run_agent(EXPERIMENT_TASK, max_calls=args.max_calls, max_review_calls=4,
                               stream=not args.no_stream, on_event=listener)
        else:
            candidate = BAD_ANSWER if args.case == "bad" else GOOD_ANSWER
            print(f"[待评审候选]\n{candidate}", flush=True)
            # 不预填工具结果，由评审者自己决定怎样查证真实材料。
            report = review_answer(DEFAULT_TASK, candidate, [], max_calls=min(4, args.max_calls),
                                   stream=not args.no_stream, events=EventSink(listener, scope="reviewer"))

    report["experiment"] = {
        "case": args.case, "model_mode": "live",
        "description": "bad/good 的候选答案是人为预设，模型响应和工具执行未做模拟。",
    }
    report["event_log"] = str(event_path)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[轨迹] {path}")
    print(f"[增量事件] {event_path}")
    print(f"[模型请求总数] {report['model_requests']}")
    return 0 if report["status"] in ("answered", "reviewed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
