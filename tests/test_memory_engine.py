import json, tempfile, unittest
from pathlib import Path
import memory_engine as m

class FileMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def test_append_and_hash_chain(self):
        m.remember("company", "Mission", {"text": "Build useful tools"}, root=self.root)
        m.remember("project", "Prototype", {"status": "started"}, root=self.root)
        result = m.verify(self.root)
        self.assertTrue(result["valid"])
        self.assertEqual(result["events"], 2)
    def test_search(self):
        m.remember("research", "Market signal", {"summary": "farm records"}, root=self.root)
        self.assertEqual(len(m.search("farm", "research", root=self.root)), 1)
    def test_reject_secrets(self):
        with self.assertRaises(ValueError):
            m.remember("work", "bad", {"api_key": "never"}, root=self.root)
    def test_snapshot_and_verified_restore(self):
        m.remember("learning", "Lesson", {"text": "test before release"}, root=self.root)
        snap = m.create_snapshot(self.root)
        restored = m.restore_snapshot(snap["file"], self.root)
        self.assertTrue(restored["restored"])
        self.assertTrue(m.verify(self.root)["valid"])
        self.assertEqual(m.verify(self.root)["events"], 2)
    def test_detect_tampering(self):
        m.remember("company", "Original", {"text": "ok"}, root=self.root)
        journal = self.root / m.JOURNAL_NAME
        event = json.loads(journal.read_text())
        event["content"]["text"] = "tampered"
        journal.write_text(json.dumps(event) + "\n")
        self.assertFalse(m.verify(self.root)["valid"])

if __name__ == "__main__":
    unittest.main()
