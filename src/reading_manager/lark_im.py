import json
import os
import subprocess
from typing import Callable, Mapping, Optional

from .config import load_lark_identity, validate_lark_identity


class LarkImError(RuntimeError):
    pass


class LarkImClient:
    def __init__(self, identity: Optional[str] = None, runner: Callable = subprocess.run):
        self.identity = (
            load_lark_identity()
            if identity is None
            else validate_lark_identity(identity)
        )
        self.runner = runner

    def send_text(
        self,
        text: str,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        if not text.strip():
            raise LarkImError("message text is empty")
        if bool(user_id) == bool(chat_id):
            raise LarkImError("exactly one of user_id or chat_id is required")

        command = ["lark-cli", "im", "+messages-send", "--as", self.identity]
        if user_id:
            command.extend(["--user-id", user_id])
        if chat_id:
            command.extend(["--chat-id", chat_id])
        command.extend(["--text", text])
        if idempotency_key:
            command.extend(["--idempotency-key", idempotency_key])

        payload = self._run(command)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if isinstance(data, Mapping):
            return str(data.get("message_id") or "")
        return ""

    def _run(self, command):
        env = os.environ.copy()
        env["LARK_CLI_NO_PROXY"] = "1"
        result = self.runner(command, capture_output=True, text=True, check=False, env=env)
        if result.returncode != 0:
            if self.identity == "user" and (
                _has_user_authorization_error(result.stdout)
                or _has_user_authorization_error(result.stderr)
            ):
                raise LarkImError(
                    "飞书用户授权缺失或已过期，请按 docs/deployment-vps.md 的重新授权步骤操作"
                )
            raise LarkImError("lark-cli im command failed")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LarkImError("lark-cli im did not return JSON") from exc
        if isinstance(payload, Mapping) and payload.get("ok") is False:
            if self.identity == "user" and _payload_has_user_authorization_error(payload):
                raise LarkImError(
                    "飞书用户授权缺失或已过期，请按 docs/deployment-vps.md 的重新授权步骤操作"
                )
            raise LarkImError("lark-cli im returned an error")
        return payload


def _has_user_authorization_error(text: str) -> bool:
    if not text:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    return _payload_has_user_authorization_error(payload)


def _payload_has_user_authorization_error(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return False
    values = (
        error.get("type"),
        error.get("subtype"),
        error.get("message"),
    )
    combined = " ".join(str(value or "").lower() for value in values)
    return (
        str(error.get("type") or "").lower() == "authorization"
        or str(error.get("subtype") or "").lower()
        in {"missing_scope", "not_authenticated", "token_expired"}
        or error.get("code") == 99991679
        or "need_user_authorization" in combined
        or "no access token available for user" in combined
        or "token expired" in combined
        or "authorization expired" in combined
    )
