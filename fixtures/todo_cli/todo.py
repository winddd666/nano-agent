"""Tiny task store used as an isolated coding-agent exercise."""

import json
from pathlib import Path


def load(path):
    file = Path(path)
    return json.loads(file.read_text(encoding="utf-8")) if file.exists() else []


def save(path, tasks):
    Path(path).write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")


def add(tasks, title):
    if not title.strip():
        raise ValueError("title must not be empty")
    # Deliberate defect: deleting a middle item can make this ID collide.
    task = {"id": len(tasks) + 1, "title": title, "done": False}
    tasks.append(task)
    return task


def remove(tasks, task_id):
    for index, task in enumerate(tasks):
        if task["id"] == task_id:
            return tasks.pop(index)
    raise KeyError(task_id)


def complete(tasks, task_id):
    for task in tasks:
        if task["id"] == task_id:
            task["done"] = True
            return task
    raise KeyError(task_id)
