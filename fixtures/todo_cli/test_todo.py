import tempfile
import unittest
from pathlib import Path

from todo import add, complete, load, remove, save


class TodoTests(unittest.TestCase):
    def test_add_and_complete(self):
        tasks = []
        self.assertEqual(add(tasks, "one")["id"], 1)
        self.assertTrue(complete(tasks, 1)["done"])

    def test_remove(self):
        tasks = []
        add(tasks, "one")
        add(tasks, "two")
        self.assertEqual(remove(tasks, 1)["title"], "one")
        self.assertEqual(len(tasks), 1)

    def test_empty_title(self):
        with self.assertRaises(ValueError):
            add([], "  ")

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            save(path, [{"id": 3, "title": "x", "done": False}])
            self.assertEqual(load(path)[0]["id"], 3)
