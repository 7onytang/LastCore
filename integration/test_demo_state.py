"""Regression checks for private demo state-file creation."""

import json
import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from integration import demo


class DemoStateFileTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not an NTFS ACL check")
    def test_state_temp_file_is_private_before_serializing_tokens(self):
        with __import__("tempfile").TemporaryDirectory() as directory:
            workdir = Path(directory)
            serialize = json.dumps

            def assert_private_then_serialize(*args, **kwargs):
                candidates = list(workdir.glob(f".{demo.STATE_NAME}.*.tmp"))
                self.assertEqual(len(candidates), 1)
                temp_path = candidates[0]
                self.assertEqual(stat.S_IMODE(temp_path.stat().st_mode), 0o600)
                self.assertEqual(temp_path.stat().st_size, 0)
                return serialize(*args, **kwargs)

            with patch.object(demo.json, "dumps", side_effect=assert_private_then_serialize):
                state_path = demo.write_state(workdir, {"token": "test-secret"})

            self.assertEqual(state_path.read_text(encoding="utf-8"), '{\n  "token": "test-secret"\n}\n')
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
            self.assertEqual(list(workdir.glob(f".{demo.STATE_NAME}.*.tmp")), [])

    def test_failed_serialization_preserves_old_state_and_removes_temp_file(self):
        with __import__("tempfile").TemporaryDirectory() as directory:
            workdir = Path(directory)
            state_path = workdir / demo.STATE_NAME
            state_path.write_text('{"old": true}\n', encoding="utf-8")

            with patch.object(demo.json, "dumps", side_effect=ValueError("serialize failed")):
                with self.assertRaisesRegex(ValueError, "serialize failed"):
                    demo.write_state(workdir, {"token": "test-secret"})

            self.assertEqual(state_path.read_text(encoding="utf-8"), '{"old": true}\n')
            self.assertEqual(list(workdir.glob(f".{demo.STATE_NAME}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
