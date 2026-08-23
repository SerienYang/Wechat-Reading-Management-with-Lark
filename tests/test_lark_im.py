import json
import unittest

from reading_manager.lark_im import LarkImClient, LarkImError


class LarkImTests(unittest.TestCase):
    def test_send_text_to_user_uses_messages_send(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return _Result(
                stdout=json.dumps({"ok": True, "data": {"message_id": "om_123"}}),
                stderr="",
                returncode=0,
            )

        client = LarkImClient(identity="user", runner=runner)

        message_id = client.send_text("hello", user_id="ou_123")

        self.assertEqual(message_id, "om_123")
        self.assertIn("+messages-send", calls[0])
        self.assertIn("--user-id", calls[0])
        self.assertIn("--text", calls[0])
        self.assertEqual(calls[0][3:5], ["--as", "user"])

    def test_send_text_requires_exactly_one_target(self):
        client = LarkImClient(identity="user", runner=lambda command, **kwargs: None)

        with self.assertRaises(LarkImError):
            client.send_text("hello")

    def test_send_text_reports_user_reauthorization_without_upstream_payload(self):
        def runner(command, **kwargs):
            return _Result(
                stdout="",
                stderr=json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "message": "need_user_authorization",
                            "hint": "token=must-not-leak",
                        },
                    }
                ),
                returncode=1,
            )

        client = LarkImClient(identity="user", runner=runner)

        with self.assertRaisesRegex(LarkImError, "重新授权") as raised:
            client.send_text("hello", user_id="ou_123")

        self.assertNotIn("must-not-leak", str(raised.exception))


class _Result:
    def __init__(self, stdout: str, stderr: str, returncode: int):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


if __name__ == "__main__":
    unittest.main()
