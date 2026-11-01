"""Fast, offline regression tests for GENESIS core utilities."""
import json
import os
import tempfile
import unittest
from pathlib import Path

_TEST_DIR = tempfile.TemporaryDirectory(prefix="genesis-tests-")
os.environ["GENESIS_DB"] = str(Path(_TEST_DIR.name) / "test.db")
os.environ["GENESIS_WORKSPACE"] = str(Path(_TEST_DIR.name) / "workspace")

import app  # noqa: E402


class GenesisCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.init_db()

    def test_parse_json_plain_object(self):
        self.assertEqual(app.parse_json('{"decision":"validate","score":68}'),
                         {"decision": "validate", "score": 68})

    def test_parse_json_markdown_fence_and_trailing_comma(self):
        value = app.parse_json('Here is the result:\n```json\n{decision: "validate", score: 68,}\n```')
        self.assertEqual(value["decision"], "validate")
        self.assertEqual(value["score"], 68)

    def test_parse_json_array(self):
        self.assertEqual(app.parse_json('[1, 2, 3]'), [1, 2, 3])

    def test_invalid_json_fails_loudly(self):
        with self.assertRaises(ValueError):
            app.parse_json("not json at all")

    def test_create_task_preserves_explicit_id_and_status(self):
        task_id = "test-task-regression"
        actual = app.create_task(
            "Regression task", "Test task persistence", "coordinator",
            task_id=task_id, status="completed", result="test-result"
        )
        self.assertEqual(actual, task_id)
        with app.db() as conn:
            row = conn.execute("SELECT id,status,result FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["result"], "test-result")

    def test_duplicate_explicit_task_id_is_idempotent(self):
        task_id = "test-task-idempotent"
        app.create_task("First", "First record", task_id=task_id, status="completed")
        app.create_task("Second", "Should not overwrite", task_id=task_id, status="queued")
        with app.db() as conn:
            row = conn.execute("SELECT title,status FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(row["title"], "First")
        self.assertEqual(row["status"], "completed")


if __name__ == "__main__":
    unittest.main()
