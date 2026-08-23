import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Optional

from .config import (
    ConfigurationError,
    LarkResourceConfig,
    load_lark_identity,
    load_lark_resource_config,
    validate_lark_identity,
)
from .sync_keys import normalize_label_name, note_dedupe_key
from .tagging import BookTag
from .weread import BookMetadata, WeReadHighlight, WeReadProgress


class LarkBaseError(RuntimeError):
    pass


class LarkAuthorizationError(LarkBaseError):
    pass


class LarkBaseClient:
    def __init__(
        self,
        base_token: Optional[str] = None,
        resources: Optional[LarkResourceConfig] = None,
        identity: Optional[str] = None,
        runner: Callable = subprocess.run,
    ):
        if resources is None and base_token is None:
            resources = load_lark_resource_config()
        self.resources = resources
        self.base_token = (
            str(base_token).strip()
            if base_token is not None
            else resources.base_token
        )
        self.identity = (
            load_lark_identity()
            if identity is None
            else validate_lark_identity(identity)
        )
        self.runner = runner

    def table_id(self, table_name: str) -> str:
        if self.resources is None:
            raise ConfigurationError("Lark table resources are not configured")
        return self.resources.table_id(table_name)

    def upsert_record(self, table_id: str, fields: Mapping[str, object], record_id: Optional[str] = None) -> str:
        command = [
            "lark-cli",
            "base",
            "+record-upsert",
            "--base-token",
            self.base_token,
            "--table-id",
            table_id,
            "--json",
            json.dumps(dict(fields), ensure_ascii=False),
            "--as",
            self.identity,
        ]
        if record_id:
            command.extend(["--record-id", record_id])
        payload = self._run(command)
        parsed_record_id = extract_record_id(payload)
        return parsed_record_id or record_id or ""

    def find_record_id(self, table_id: str, search_field: str, keyword: str) -> str:
        body = {
            "keyword": keyword,
            "search_fields": [search_field],
            "select_fields": [search_field],
            "limit": 10,
        }
        command = [
            "lark-cli",
            "base",
            "+record-search",
            "--base-token",
            self.base_token,
            "--table-id",
            table_id,
            "--format",
            "json",
            "--json",
            json.dumps(body, ensure_ascii=False),
            "--as",
            self.identity,
        ]
        payload = self._run(command)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            return ""
        record_ids = data.get("record_id_list")
        if isinstance(record_ids, list) and record_ids:
            return str(record_ids[0])
        return ""

    def list_records(self, table_id: str, fields) -> list:
        records = []
        page_size = 200
        offset = 0
        while True:
            command = [
                "lark-cli",
                "base",
                "+record-list",
                "--base-token",
                self.base_token,
                "--table-id",
                table_id,
                "--format",
                "json",
                "--limit",
                str(page_size),
                "--offset",
                str(offset),
                "--as",
                self.identity,
            ]
            for field in fields:
                command.extend(["--field-id", field])
            payload = self._run(command)
            data = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(data, Mapping):
                return records
            field_names = data.get("fields") or []
            rows = data.get("data") or []
            record_ids = data.get("record_id_list") or []
            for index, row in enumerate(rows):
                record = {"record_id": record_ids[index] if index < len(record_ids) else ""}
                for field_index, field_name in enumerate(field_names):
                    record[field_name] = row[field_index] if field_index < len(row) else None
                records.append(record)
            if len(rows) < page_size:
                break
            offset += page_size
        return records

    def upload_attachment(
        self,
        table_id: str,
        record_id: str,
        field_id: str,
        file_path: Path,
    ) -> Mapping[str, object]:
        file_path = Path(file_path)
        command_cwd = str(file_path.parent) if file_path.is_absolute() else None
        file_argument = file_path.name if file_path.is_absolute() else str(file_path)
        command = [
            "lark-cli",
            "base",
            "+record-upload-attachment",
            "--base-token",
            self.base_token,
            "--table-id",
            table_id,
            "--record-id",
            record_id,
            "--field-id",
            field_id,
            "--file",
            file_argument,
            "--as",
            self.identity,
        ]
        payload = self._run(command, cwd=command_cwd)
        ignored_fields = extract_ignored_fields(payload)
        if ignored_fields:
            details = "; ".join(
                f"{field.get('name') or field.get('id')}: {field.get('reason') or 'ignored'}"
                for field in ignored_fields
                if isinstance(field, Mapping)
            )
            raise LarkBaseError(f"attachment upload ignored by Lark Base: {details or ignored_fields}")
        return payload

    def replace_attachment_field(
        self,
        table_id: str,
        record_id: str,
        field_id: str,
        file_tokens: list,
    ) -> None:
        command = [
            "lark-cli",
            "api",
            "PUT",
            f"/open-apis/bitable/v1/apps/{self.base_token}/tables/{table_id}/records/{record_id}",
            "--data",
            json.dumps(
                {"fields": {field_id: [{"file_token": token} for token in file_tokens]}},
                ensure_ascii=False,
            ),
            "--as",
            self.identity,
        ]
        self._run(command)

    def _run(self, command, cwd: Optional[str] = None):
        env = os.environ.copy()
        env["LARK_CLI_NO_PROXY"] = "1"
        result = self.runner(command, capture_output=True, text=True, check=False, env=env, cwd=cwd)
        if result.returncode != 0:
            if self.identity == "user" and (
                _has_user_authorization_error(result.stdout)
                or _has_user_authorization_error(result.stderr)
            ):
                raise LarkAuthorizationError(
                    "飞书用户授权缺失或已过期，请按 docs/deployment-vps.md 的重新授权步骤操作"
                )
            detail = _extract_error_message(result.stdout) or _extract_error_message(result.stderr)
            raise LarkBaseError(f"lark-cli command failed: {detail}" if detail else "lark-cli command failed")
        try:
            payload = parse_json_payload(result.stdout)
        except json.JSONDecodeError as exc:
            raise LarkBaseError("lark-cli did not return JSON") from exc
        if isinstance(payload, Mapping) and payload.get("ok") is False:
            if self.identity == "user" and _payload_has_user_authorization_error(payload):
                raise LarkAuthorizationError(
                    "飞书用户授权缺失或已过期，请按 docs/deployment-vps.md 的重新授权步骤操作"
                )
            raise LarkBaseError("lark-cli returned an error")
        if isinstance(payload, Mapping) and payload.get("code") not in (None, 0):
            raise LarkBaseError(str(payload.get("msg") or payload.get("message") or "lark-cli returned an error"))
        return payload


def extract_record_id(payload: Mapping[str, object]) -> str:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return ""
    record = data.get("record")
    if isinstance(record, Mapping):
        return str(record.get("record_id") or record.get("id") or "")
    record_id_list = data.get("record_id_list")
    if isinstance(record_id_list, list) and record_id_list:
        return str(record_id_list[0])
    return ""


def parse_json_payload(text: str):
    decoder = json.JSONDecoder()
    last_payload = None
    for index, char in enumerate(text or ""):
        if char not in "[{":
            continue
        try:
            payload, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if text[index + end :].strip():
            continue
        last_payload = payload
    if last_payload is None:
        raise json.JSONDecodeError("No JSON object found", text or "", 0)
    return last_payload


def _extract_error_message(text: str) -> str:
    if not text:
        return ""
    try:
        payload = parse_json_payload(text)
    except json.JSONDecodeError:
        return text.strip().splitlines()[-1] if text.strip() else ""
    if not isinstance(payload, Mapping):
        return ""
    error = payload.get("error")
    if isinstance(error, Mapping):
        return str(error.get("message") or error.get("type") or "")
    return str(payload.get("message") or payload.get("msg") or "")


def _has_user_authorization_error(text: str) -> bool:
    if not text:
        return False
    try:
        payload = parse_json_payload(text)
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


def extract_ignored_fields(payload: Mapping[str, object]) -> list:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return []
    record = data.get("record")
    if not isinstance(record, Mapping):
        return []
    ignored_fields = record.get("ignored_fields")
    if isinstance(ignored_fields, list):
        return ignored_fields
    return []


def extract_attachment_file_token(payload: Mapping[str, object], field_id: str, name: Optional[str] = None) -> str:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return ""
    direct_token = data.get("file_token")
    if direct_token:
        return str(direct_token)
    record = data.get("record")
    if not isinstance(record, Mapping):
        return ""
    fields = record.get("fields")
    if not isinstance(fields, Mapping):
        return ""
    attachments = fields.get(field_id) or fields.get(str(field_id))
    if not isinstance(attachments, list):
        return ""
    fallback = ""
    for item in attachments:
        if not isinstance(item, Mapping):
            continue
        token = str(item.get("file_token") or item.get("token") or "")
        if not token:
            continue
        fallback = token
        if not name or item.get("name") == name:
            return token
    return fallback


def link_value(record_id: str):
    return [{"id": record_id}]


def make_book_fields(
    metadata: BookMetadata,
    now: str,
    last_read_time: Optional[str],
    reading_status: str = "未知",
    reading_progress: Optional[int] = None,
) -> dict:
    fields = {
        "书名": metadata.title,
        "微信读书 Book ID": metadata.book_id,
        "作者": metadata.author,
        "译者": metadata.translator,
        "出版社": metadata.publisher,
        "ISBN": metadata.isbn,
        "分类": metadata.category,
        "封面": metadata.cover,
        "简介": metadata.intro,
        "阅读状态": reading_status,
        "阅读进度": book_progress_ratio(reading_progress, reading_status),
        "最近阅读时间": last_read_time,
        "首次同步时间": now,
        "最近同步时间": now,
        "数据来源": "微信读书",
    }
    return _drop_empty(fields)


def make_progress_fields(
    title: str,
    book_id: str,
    book_record_id: str,
    sync_record_id: str,
    progress: WeReadProgress,
    now: str,
    record_date: Optional[str] = None,
    daily_key: Optional[str] = None,
) -> dict:
    status = _reading_status(progress)
    record_title = f"{title} / {record_date or now}"
    return _drop_empty(
        {
            "记录标题": record_title,
            "书籍": link_value(book_record_id),
            "微信读书 Book ID": book_id,
            "记录日期": record_date,
            "日级记录键": daily_key,
            "阅读状态": status,
            "阅读进度": progress_ratio(progress.progress),
            "阅读时长": progress.reading_minutes,
            "最近阅读时间": epoch_to_lark_datetime(progress.last_read_time),
            "读完时间": epoch_to_lark_datetime(progress.finish_time),
            "同步批次": link_value(sync_record_id),
            "同步时间": now,
        }
    )


def make_daily_stats_fields(
    record_date: str,
    stats_key: str,
    duration_seconds: int,
    duration_minutes: float,
    effective_reading_day: int,
    status: str,
    now: str,
    sync_record_id: str = "",
    note: str = "",
) -> dict:
    fields = {
        "统计标题": f"{record_date} 阅读统计",
        "统计日期": record_date,
        "日级统计键": stats_key,
        "当天阅读时长（分钟）": duration_minutes,
        "当天阅读时长（秒）": duration_seconds,
        "有效阅读日（1/0）": effective_reading_day,
        "统计状态": status,
        "同步批次": link_value(sync_record_id) if sync_record_id else None,
        "计算时间": now,
        "数据来源": "微信读书阅读统计",
        "备注": note,
    }
    return _drop_empty(fields)


def make_highlight_fields(
    title: str,
    highlight: WeReadHighlight,
    book_record_id: str,
    sync_record_id: str,
    now: str,
) -> dict:
    note_text = note_text_for_hash(highlight)
    content_hash = note_dedupe_key(
        book_id=highlight.book_id,
        note_id=highlight.note_id,
        position=highlight.position,
        text=note_text,
    )
    chapter = highlight.chapter or highlight.note_type or "未知章节"
    return _drop_empty(
        {
            "笔记标题": f"{title} / {chapter}",
            "书籍": link_value(book_record_id),
            "微信读书 Note ID": highlight.note_id,
            "类型": highlight.note_type or "划线",
            "章节": highlight.chapter,
            "位置": highlight.position,
            "划线原文": highlight.highlight_text,
            "个人笔记": highlight.personal_note,
            "创建时间": epoch_to_lark_datetime(highlight.create_time),
            "更新时间": epoch_to_lark_datetime(highlight.create_time),
            "内容哈希": content_hash,
            "同步批次": link_value(sync_record_id),
            "最近同步时间": now,
        }
    )


def note_text_for_hash(highlight: WeReadHighlight) -> str:
    parts = [highlight.highlight_text, highlight.personal_note]
    return "\n".join(str(part).strip() for part in parts if str(part or "").strip())


def make_sync_fields(
    batch_id: str,
    status: str,
    scope: str,
    selected_books,
    started_at: str,
    ended_at: Optional[str],
    books_count: int,
    reading_records_count: int,
    highlights_count: int,
    tags_count: int,
    error_summary: str,
) -> dict:
    return _drop_empty(
        {
            "同步批次 ID": batch_id,
            "同步状态": status,
            "同步范围": scope,
            "选择书籍": "\n".join(selected_books),
            "开始时间": started_at,
            "结束时间": ended_at,
            "写入书籍数": books_count,
            "写入阅读记录数": reading_records_count,
            "写入笔记划线数": highlights_count,
            "写入标签数": tags_count,
            "错误摘要": error_summary,
            "工具版本": "0.1.0",
        }
    )


def make_tag_fields(tag: BookTag, now: str) -> dict:
    tag_name = tag.name.strip()
    return _drop_empty(
        {
            "标签名": tag_name,
            "规范化名称": normalize_label_name(tag_name),
            "标签说明": tag.description,
            "标签来源": "AI",
            "适用层级": "书籍",
            "最近生成时间": now,
        }
    )


def epoch_to_lark_datetime(value: Optional[int]) -> Optional[str]:
    if not value:
        return None
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def progress_ratio(value: Optional[int]) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(min(max(number, 0.0), 100.0) / 100.0, 4)


def book_progress_ratio(value: Optional[int], reading_status: str) -> Optional[float]:
    ratio = progress_ratio(value)
    if reading_status == "读完" and (ratio is None or ratio == 0):
        return 1.0
    return ratio


def _reading_status(progress: WeReadProgress) -> str:
    if progress.finish_time or progress.progress >= 95:
        return "读完"
    if progress.progress > 0:
        return "在读"
    return "未知"


def _drop_empty(fields: Mapping[str, object]) -> dict:
    return {key: value for key, value in fields.items() if value not in ("", None)}
