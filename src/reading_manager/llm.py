import http.client
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional


DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"
DEFAULT_LLM_MODEL = "deepseek-v4-flash"
DEFAULT_LLM_TIMEOUT_SECONDS = 45
DEFAULT_LLM_MAX_ATTEMPTS = 3


class LLMError(RuntimeError):
    pass


class LLMRetryableError(LLMError):
    pass


class LLMFatalError(LLMError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str = DEFAULT_LLM_BASE_URL
    model: str = DEFAULT_LLM_MODEL
    timeout_seconds: int = DEFAULT_LLM_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_LLM_MAX_ATTEMPTS

    def __post_init__(self) -> None:
        api_key = self.api_key.strip()
        base_url = self.base_url.rstrip("/")
        model = self.model.strip()
        parsed_url = urllib.parse.urlparse(base_url)
        if not api_key:
            raise LLMFatalError("LLM_API_KEY is required")
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise LLMFatalError("LLM_BASE_URL must be a valid HTTPS URL")
        if not model:
            raise LLMFatalError("LLM_MODEL is required")
        if self.timeout_seconds <= 0:
            raise LLMFatalError("LLM_TIMEOUT_SECONDS must be a positive integer")
        if self.max_attempts <= 0:
            raise LLMFatalError("LLM_MAX_ATTEMPTS must be a positive integer")
        if self.max_attempts > 3:
            raise LLMFatalError("LLM_MAX_ATTEMPTS must be at most 3")
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "model", model)

    @classmethod
    def from_env(cls, api_key: Optional[str] = None) -> "LLMConfig":
        return cls(
            api_key=api_key if api_key is not None else os.environ.get("LLM_API_KEY", ""),
            base_url=os.environ.get("LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
            model=os.environ.get("LLM_MODEL", DEFAULT_LLM_MODEL),
            timeout_seconds=_positive_int_from_env(
                "LLM_TIMEOUT_SECONDS",
                DEFAULT_LLM_TIMEOUT_SECONDS,
            ),
            max_attempts=_positive_int_from_env(
                "LLM_MAX_ATTEMPTS",
                DEFAULT_LLM_MAX_ATTEMPTS,
            ),
        )


class OpenAICompatibleClient:
    def __init__(
        self,
        config: LLMConfig,
        opener: Callable = urllib.request.urlopen,
    ):
        self.config = config
        self._opener = opener

    def complete_json(self, prompt: str) -> str:
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(
                {
                    "model": self.config.model,
                    "messages": [
                        {"role": "system", "content": "Return valid JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "thinking": {"type": "disabled"},
                    "temperature": 0.2,
                    "max_tokens": 4096,
                    "stream": False,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.config.timeout_seconds) as response:
                raw_response = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or 500 <= exc.code < 600:
                raise LLMRetryableError(f"LLM request failed with HTTP {exc.code}") from exc
            raise LLMFatalError(f"LLM request failed with HTTP {exc.code}") from exc
        except (
            urllib.error.URLError,
            ConnectionError,
            TimeoutError,
            socket.timeout,
            http.client.IncompleteRead,
        ) as exc:
            raise LLMRetryableError("LLM request failed due to a network error") from exc

        try:
            payload = json.loads(raw_response)
            choice = payload["choices"][0]
            finish_reason = choice.get("finish_reason")
            content = choice["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMRetryableError("LLM response envelope is invalid") from exc

        if finish_reason == "length":
            raise LLMRetryableError("LLM response was truncated")
        if not isinstance(content, str) or not content.strip():
            raise LLMRetryableError("LLM response content is empty")
        return content.strip()

    def _endpoint(self) -> str:
        if self.config.base_url.endswith("/chat/completions"):
            return self.config.base_url
        return self.config.base_url + "/chat/completions"


def _positive_int_from_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise LLMFatalError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise LLMFatalError(f"{name} must be a positive integer")
    return value
