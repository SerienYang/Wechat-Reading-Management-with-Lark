import os
from dataclasses import dataclass
from typing import Mapping, Optional


class ConfigurationError(RuntimeError):
    pass


LARK_TABLE_ENV_BY_NAME = {
    "书籍": "LARK_TABLE_BOOKS",
    "阅读记录": "LARK_TABLE_READING_RECORDS",
    "笔记划线": "LARK_TABLE_NOTES",
    "每日阅读统计": "LARK_TABLE_DAILY_STATS",
    "标签主题": "LARK_TABLE_TAGS",
    "同步记录": "LARK_TABLE_SYNC_RUNS",
}


@dataclass(frozen=True)
class LarkResourceConfig:
    base_token: str
    base_url: str
    table_ids: tuple[tuple[str, str], ...]

    def table_id(self, table_name: str) -> str:
        for name, table_id in self.table_ids:
            if name == table_name:
                return table_id
        raise ConfigurationError(f"Unknown Lark table: {table_name}")


def load_lark_resource_config(
    environ: Optional[Mapping[str, str]] = None,
) -> LarkResourceConfig:
    values = os.environ if environ is None else environ
    required_variables = ["LARK_BASE_TOKEN", *LARK_TABLE_ENV_BY_NAME.values()]
    missing = [
        variable
        for variable in required_variables
        if not str(values.get(variable, "")).strip()
    ]
    if missing:
        raise ConfigurationError(
            "Missing required Lark configuration: " + ", ".join(missing)
        )

    table_ids = tuple(
        (table_name, str(values[variable]).strip())
        for table_name, variable in LARK_TABLE_ENV_BY_NAME.items()
    )
    return LarkResourceConfig(
        base_token=str(values["LARK_BASE_TOKEN"]).strip(),
        base_url=str(values.get("LARK_BASE_URL", "")).strip(),
        table_ids=table_ids,
    )


def load_lark_identity() -> str:
    return validate_lark_identity(os.environ.get("LARK_IDENTITY", ""))


def validate_lark_identity(value: str) -> str:
    identity = str(value or "").strip().lower()
    if identity not in {"user", "bot"}:
        raise ConfigurationError("LARK_IDENTITY must be user or bot")
    return identity
