"""Standard-library-only checks; no ROS, model requests or simulator."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    "release_audit", Path(__file__).resolve().parents[1] / "scripts/release_audit.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReleaseAuditTests(unittest.TestCase):
    def test_credential_values_are_never_returned(self):
        value = b"sk-" + b"A" * 32
        self.assertEqual(module.scan_text(value), ["model_key"])
        self.assertNotIn(value.decode(), repr(module.scan_text(value)))

    def test_template_paths_and_runtime_paths(self):
        self.assertFalse(module.private_path("config/launch.env.example"))
        self.assertTrue(module.private_path("config/launch.env"))
        self.assertTrue(module.private_path("runtime.sqlite3"))

    def test_history_catches_secret_deleted_from_current_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def run(*args):
                return subprocess.run(
                    ["git", "-C", str(root), *args], check=True, capture_output=True
                )

            run("init")
            path = root / "bad.txt"
            path.write_text("sk-" + "A" * 32)
            run("add", "bad.txt")
            run(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "fixture",
            )
            path.write_text("removed")
            result = module.audit(root, history=True)
            self.assertEqual(result["history_blobs_checked"], 1)
            self.assertTrue(
                any(
                    row["kind"] == "model_key"
                    and row["location"].startswith("history:")
                    for row in result["findings"]
                )
            )


if __name__ == "__main__":
    unittest.main()
