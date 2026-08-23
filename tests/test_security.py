import unittest
from unittest.mock import patch

from reading_manager.security import (
    KeychainError,
    build_keychain_get_command,
    build_keychain_set_command,
    is_keychain_available,
    load_secret_from_keychain,
    redact_secret,
    save_secret_to_keychain,
)


class SecurityTests(unittest.TestCase):
    def test_redacts_long_secret_but_keeps_shape(self):
        self.assertEqual(redact_secret("abcdefghijklmnopqrstuvwxyz"), "abcd...wxyz")

    def test_redacts_short_secret_completely(self):
        self.assertEqual(redact_secret("short"), "*****")

    def test_keychain_commands_do_not_embed_secret_in_get(self):
        command = build_keychain_get_command(service="reading-manager.weread", account="default")

        self.assertEqual(command[:3], ["security", "find-generic-password", "-s"])
        self.assertNotIn("password", command)

    def test_keychain_set_command_uses_generic_password(self):
        command = build_keychain_set_command(
            service="reading-manager.weread",
            account="default",
            password="secret-value",
        )

        self.assertIn("add-generic-password", command)
        self.assertIn("-U", command)
        self.assertIn("secret-value", command)

    @patch("reading_manager.security.shutil.which")
    @patch("reading_manager.security.sys.platform", "linux")
    def test_keychain_is_unavailable_on_linux(self, which):
        self.assertFalse(is_keychain_available())
        which.assert_not_called()

    @patch("reading_manager.security.subprocess.run")
    @patch("reading_manager.security.shutil.which")
    @patch("reading_manager.security.sys.platform", "linux")
    def test_load_secret_returns_none_without_keychain(self, which, run):
        self.assertIsNone(load_secret_from_keychain("svc", "acct"))
        which.assert_not_called()
        run.assert_not_called()

    @patch("reading_manager.security.subprocess.run")
    @patch("reading_manager.security.shutil.which")
    @patch("reading_manager.security.sys.platform", "linux")
    def test_save_secret_rejects_unavailable_keychain(self, which, run):
        with self.assertRaisesRegex(KeychainError, "macOS Keychain is unavailable"):
            save_secret_to_keychain("svc", "acct", "secret")
        which.assert_not_called()
        run.assert_not_called()

    @patch("reading_manager.security.subprocess.run")
    def test_load_secret_from_keychain_returns_none_when_missing(self, run):
        run.return_value.returncode = 44
        run.return_value.stdout = ""
        run.return_value.stderr = "not found"

        with patch("reading_manager.security.is_keychain_available", return_value=True):
            self.assertIsNone(load_secret_from_keychain("svc", "acct"))

    @patch("reading_manager.security.subprocess.run")
    def test_load_secret_from_keychain_returns_secret(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "secret\n"
        run.return_value.stderr = ""

        with patch("reading_manager.security.is_keychain_available", return_value=True):
            self.assertEqual(load_secret_from_keychain("svc", "acct"), "secret")

    @patch("reading_manager.security.subprocess.run")
    def test_save_secret_to_keychain_raises_on_failure(self, run):
        run.return_value.returncode = 1
        run.return_value.stdout = ""
        run.return_value.stderr = "failed"

        with patch("reading_manager.security.is_keychain_available", return_value=True):
            with self.assertRaises(KeychainError):
                save_secret_to_keychain("svc", "acct", "secret")


if __name__ == "__main__":
    unittest.main()
