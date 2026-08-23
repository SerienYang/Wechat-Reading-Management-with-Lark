import json
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Set

from .llm import (
    LLMConfig,
    LLMFatalError,
    LLMRetryableError,
    OpenAICompatibleClient,
)


class TaggingParseError(ValueError):
    pass


@dataclass(frozen=True)
class BookTag:
    name: str
    description: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class NoteTag:
    note_key: str
    tags: List[str]
    reason: str = ""


@dataclass(frozen=True)
class TaggingResult:
    book_tags: List[BookTag]
    note_tags: List[NoteTag]


@dataclass(frozen=True)
class TaggingNote:
    note_key: str
    title: str
    text: str


class TaggingServiceError(RuntimeError):
    pass


def build_tagging_prompt(notes: List[TaggingNote], existing_tags: Iterable[str]) -> str:
    payload = {
        "existing_tags": list(existing_tags),
        "notes": [
            {"note_key": note.note_key, "title": note.title, "text": note.text}
            for note in notes
        ],
    }
    return (
        "你是一个个人阅读管理系统的 AI 打标器。\n"
        "请根据输入的书籍标题和划线内容生成书籍级粗标签，并把这些粗标签关联回每条 note_key。\n"
        "规则：\n"
        "- 返回 JSON，且只能返回 JSON，不要 Markdown，不要解释。\n"
        "- book_tags 必须是书籍级粗标签，用来给整本书归类，建议整批 3-8 个。\n"
        "- 标签名要像书架分类或长期知识地图，例如：自然科学、软件工程、历史、艺术。\n"
        "- 不要生成章节级、概念级或单条划线级的小主题。\n"
        "- note_tags 中的 note_key 必须来自输入。\n"
        "- note_tags 只能从 book_tags 中选择标签，单条笔记可关联 1-3 个标签。\n"
        "- 优先复用 existing_tags 中已经存在且合适的标签；只有确实需要时再生成新标签。\n"
        "输出字段：book_tags[{name,description,confidence}], note_tags[{note_key,tags,reason}]。\n"
        "输入 JSON：\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def run_api_tagging(
    prompt: str,
    valid_note_keys: Iterable[str],
    client: Optional[OpenAICompatibleClient] = None,
    max_attempts: Optional[int] = None,
    request_timeout_seconds: Optional[int] = None,
    total_timeout_seconds: int = 140,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> TaggingResult:
    client = client or OpenAICompatibleClient(LLMConfig.from_env())
    attempts = max_attempts or client.config.max_attempts
    if attempts <= 0:
        raise TaggingServiceError("AI tagging attempts must be positive")
    if attempts > 3:
        raise TaggingServiceError("AI tagging attempts must be at most 3")
    request_timeout = request_timeout_seconds
    if request_timeout is None:
        request_timeout = getattr(getattr(client, "config", None), "timeout_seconds", 45)
    if request_timeout <= 0:
        raise TaggingServiceError("AI tagging request timeout must be positive")

    deadline = clock() + total_timeout_seconds
    last_error: Optional[BaseException] = None
    attempted_requests = 0
    for attempt in range(attempts):
        now = clock()
        if now >= deadline:
            break
        delay = 2 ** (attempt - 1) if attempt else 0
        if now + delay + request_timeout > deadline:
            break
        if delay:
            sleeper(delay)
        try:
            attempted_requests += 1
            raw = client.complete_json(prompt)
            return parse_tagging_response(raw, valid_note_keys=valid_note_keys)
        except LLMFatalError as exc:
            raise TaggingServiceError(str(exc)) from exc
        except (LLMRetryableError, TaggingParseError) as exc:
            last_error = exc

    detail = str(last_error) if last_error else "retry deadline reached"
    raise TaggingServiceError(
        f"AI tagging failed after {attempted_requests} attempts: {detail}"
    )


def parse_tagging_response(raw: str, valid_note_keys: Iterable[str]) -> TaggingResult:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TaggingParseError("AI tagging output is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise TaggingParseError("AI tagging output must be a JSON object")
    if set(payload) != {"book_tags", "note_tags"}:
        raise TaggingParseError("AI tagging output must contain only book_tags and note_tags")
    if not isinstance(payload["book_tags"], list):
        raise TaggingParseError("book_tags must be an array")
    if not isinstance(payload["note_tags"], list):
        raise TaggingParseError("note_tags must be an array")

    valid_keys: Set[str] = set(valid_note_keys)
    book_tags = [_parse_book_tag(item) for item in payload["book_tags"]]
    note_tags = [
        note_tag
        for note_tag in (_parse_note_tag(item) for item in payload["note_tags"])
        if note_tag.note_key in valid_keys
    ]
    if valid_keys and (not book_tags or not note_tags):
        raise TaggingParseError("AI tagging output is empty for non-empty input")
    returned_keys = {note_tag.note_key for note_tag in note_tags}
    if valid_keys - returned_keys:
        raise TaggingParseError("AI tagging output is missing known note keys")
    book_tag_names = {tag.name for tag in book_tags}
    if any(set(note_tag.tags) - book_tag_names for note_tag in note_tags):
        raise TaggingParseError("note_tags contains a tag not declared in book_tags")

    return TaggingResult(book_tags=book_tags, note_tags=note_tags)


def _parse_book_tag(item: object) -> BookTag:
    if not isinstance(item, dict):
        raise TaggingParseError("book_tags items must be objects")
    if set(item) != {"name", "description", "confidence"}:
        raise TaggingParseError("book_tags items have invalid fields")

    name_value = item["name"]
    description = item["description"]
    confidence = item["confidence"]
    if not isinstance(name_value, str):
        raise TaggingParseError("book tag name must be a string")
    if not isinstance(description, str):
        raise TaggingParseError("book tag description must be a string")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise TaggingParseError("book tag confidence must be a number")

    name = name_value.strip()
    if not name:
        raise TaggingParseError("book tag name cannot be empty")
    if len(name) > 40:
        raise TaggingParseError("book tag name is too long")
    if not 0 <= float(confidence) <= 1:
        raise TaggingParseError("book tag confidence must be between 0 and 1")

    return BookTag(
        name=name,
        description=description.strip(),
        confidence=float(confidence),
    )


def _parse_note_tag(item: object) -> NoteTag:
    if not isinstance(item, dict):
        raise TaggingParseError("note_tags items must be objects")
    if set(item) != {"note_key", "tags", "reason"}:
        raise TaggingParseError("note_tags items have invalid fields")

    note_key_value = item["note_key"]
    tags_value = item["tags"]
    reason = item["reason"]
    if not isinstance(note_key_value, str) or not note_key_value.strip():
        raise TaggingParseError("note_key must be a non-empty string")
    if not isinstance(tags_value, list):
        raise TaggingParseError("note tags must be an array")
    if not 1 <= len(tags_value) <= 3:
        raise TaggingParseError("note tags must contain between one and three values")
    if not all(isinstance(tag, str) and tag.strip() for tag in tags_value):
        raise TaggingParseError("note tags must contain non-empty strings")
    if not isinstance(reason, str):
        raise TaggingParseError("note tag reason must be a string")

    return NoteTag(
        note_key=note_key_value.strip(),
        tags=[tag.strip() for tag in tags_value],
        reason=reason.strip(),
    )
