from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from desktop_assistant.ai_config import (
    load_openai_api_key,
    normalize_openai_api_key,
    save_openai_api_key,
)
from desktop_assistant.manager import Manager


class AiConfigTests(unittest.TestCase):
    def test_api_key_is_validated_and_never_saved_in_plaintext(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            key = "sk-test-abcdefghijklmnopqrstuvwxyz"
            with patch("desktop_assistant.ai_config.protect", return_value="dpapi-value"):
                save_openai_api_key(root, key)
            raw = (root / "control" / "ai-provider.json").read_text(encoding="utf-8")
            self.assertNotIn(key, raw)
            self.assertIn("dpapi-value", raw)
            with patch("desktop_assistant.ai_config.unprotect", return_value=key):
                self.assertEqual(load_openai_api_key(root), key)

    def test_api_key_validation_rejects_incomplete_or_whitespace_values(self) -> None:
        for value in ("", "not-a-key", "sk-short", "sk-test key"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "AI 密钥"):
                normalize_openai_api_key(value)

    def test_manager_injects_key_only_into_child_process_environment(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch) / "erp"
            manager = Manager(root, b"test-public-key")
            shared = root / "shared"
            (shared / "environment.json").write_text(
                '{"ERP_PORT":"8000","ERP_BIND_HOST":"127.0.0.1"}',
                encoding="utf-8",
            )
            release = Path(scratch) / "release"
            release.mkdir()
            key = "sk-test-abcdefghijklmnopqrstuvwxyz"
            with (
                patch.dict("os.environ", {"OPENAI_API_KEY": "sk-parent-must-not-leak"}),
                patch("desktop_assistant.ai_config.load_openai_api_key", return_value=key),
            ):
                environment = manager._environment(release)
            self.assertEqual(environment["OPENAI_API_KEY"], key)
            self.assertNotIn(
                "OPENAI_API_KEY",
                (shared / "environment.json").read_text(encoding="utf-8"),
            )

    def test_manager_never_inherits_an_unmanaged_parent_api_key(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch) / "erp"
            manager = Manager(root, b"test-public-key")
            shared = root / "shared"
            (shared / "environment.json").write_text(
                '{"ERP_PORT":"8000","ERP_BIND_HOST":"127.0.0.1"}',
                encoding="utf-8",
            )
            release = Path(scratch) / "release"
            release.mkdir()
            with (
                patch.dict("os.environ", {"OPENAI_API_KEY": "sk-parent-must-not-leak"}),
                patch("desktop_assistant.ai_config.load_openai_api_key", return_value=None),
            ):
                environment = manager._environment(release)
            self.assertNotIn("OPENAI_API_KEY", environment)


if __name__ == "__main__":
    unittest.main()
