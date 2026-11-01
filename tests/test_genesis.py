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



    def test_prototype_validator_accepts_valid_html_and_python(self):
        workspace = Path(_TEST_DIR.name) / "validator-workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "index.html").write_text(
            '<!doctype html><html><head><title>Demo</title>'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<link rel="stylesheet" href="styles.css"></head><body>Hello</body></html>',
            encoding="utf-8"
        )
        (workspace / "styles.css").write_text("body { margin: 0; }", encoding="utf-8")
        (workspace / "main.py").write_text("value = 42\\n", encoding="utf-8")
        checks = app._validate_generated_files(
            workspace, ["index.html", "styles.css", "main.py"]
        )
        self.assertTrue(all(item["status"] == "passed" for item in checks), checks)

    def test_prototype_validator_flags_missing_html_assets_and_viewport(self):
        workspace = Path(_TEST_DIR.name) / "validator-invalid"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "index.html").write_text(
            '<html><head><title>Demo</title><link href="missing.css"></head><body></body></html>',
            encoding="utf-8"
        )
        checks = app._validate_generated_files(workspace, ["index.html"])
        self.assertEqual(checks[0]["status"], "failed")
        self.assertTrue(any("Missing local asset" in item for item in checks[0]["detail"]))
        self.assertTrue(any("viewport" in item.lower() for item in checks[0]["detail"]))

    def test_prototype_validator_flags_invalid_python(self):
        workspace = Path(_TEST_DIR.name) / "validator-python"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "broken.py").write_text("def unfinished(:\\n", encoding="utf-8")
        checks = app._validate_generated_files(workspace, ["broken.py"])
        self.assertEqual(checks[0]["status"], "failed")
        self.assertEqual(checks[0]["check"], "python_syntax")

if __name__ == "__main__":
    unittest.main()
