from dataclasses import dataclass
from typing import Iterable, List, Optional


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    link_table: Optional[str] = None


@dataclass(frozen=True)
class TableSpec:
    name: str
    fields: List[FieldSpec]


@dataclass(frozen=True)
class BaseSchema:
    tables: List[TableSpec]


BASE_SCHEMA = BaseSchema(
    tables=[
        TableSpec(
            name="书籍",
            fields=[
                FieldSpec("书名", "text"),
                FieldSpec("微信读书 Book ID", "text"),
                FieldSpec("作者", "text"),
                FieldSpec("译者", "text"),
                FieldSpec("出版社", "text"),
                FieldSpec("ISBN", "text"),
                FieldSpec("分类", "text"),
                FieldSpec("封面", "text"),
                FieldSpec("封面图片", "attachment"),
                FieldSpec("简介", "text"),
                FieldSpec("阅读状态", "select"),
                FieldSpec("阅读进度", "number"),
                FieldSpec("最近阅读时间", "datetime"),
                FieldSpec("书籍级标签", "link", link_table="标签主题"),
                FieldSpec("首次同步时间", "datetime"),
                FieldSpec("最近同步时间", "datetime"),
                FieldSpec("数据来源", "select"),
            ],
        ),
        TableSpec(
            name="阅读记录",
            fields=[
                FieldSpec("记录标题", "text"),
                FieldSpec("书籍", "link", link_table="书籍"),
                FieldSpec("微信读书 Book ID", "text"),
                FieldSpec("记录日期", "datetime"),
                FieldSpec("日级记录键", "text"),
                FieldSpec("阅读状态", "select"),
                FieldSpec("阅读进度", "number"),
                FieldSpec("阅读时长", "number"),
                FieldSpec("最近阅读时间", "datetime"),
                FieldSpec("读完时间", "datetime"),
                FieldSpec("同步批次", "link", link_table="同步记录"),
                FieldSpec("同步时间", "datetime"),
            ],
        ),
        TableSpec(
            name="笔记划线",
            fields=[
                FieldSpec("笔记标题", "text"),
                FieldSpec("书籍", "link", link_table="书籍"),
                FieldSpec("微信读书 Note ID", "text"),
                FieldSpec("类型", "select"),
                FieldSpec("章节", "text"),
                FieldSpec("位置", "text"),
                FieldSpec("划线原文", "text"),
                FieldSpec("个人笔记", "text"),
                FieldSpec("标签主题", "link", link_table="标签主题"),
                FieldSpec("创建时间", "datetime"),
                FieldSpec("更新时间", "datetime"),
                FieldSpec("内容哈希", "text"),
                FieldSpec("同步批次", "link", link_table="同步记录"),
                FieldSpec("最近同步时间", "datetime"),
            ],
        ),
        TableSpec(
            name="每日阅读统计",
            fields=[
                FieldSpec("统计标题", "text"),
                FieldSpec("统计日期", "datetime"),
                FieldSpec("日级统计键", "text"),
                FieldSpec("当天阅读时长（分钟）", "number"),
                FieldSpec("当天阅读时长（秒）", "number"),
                FieldSpec("有效阅读日（1/0）", "number"),
                FieldSpec("统计状态", "select"),
                FieldSpec("同步批次", "link", link_table="同步记录"),
                FieldSpec("计算时间", "datetime"),
                FieldSpec("数据来源", "select"),
                FieldSpec("备注", "text"),
            ],
        ),
        TableSpec(
            name="标签主题",
            fields=[
                FieldSpec("标签名", "text"),
                FieldSpec("规范化名称", "text"),
                FieldSpec("标签说明", "text"),
                FieldSpec("标签来源", "select"),
                FieldSpec("适用层级", "select"),
                FieldSpec("最近生成时间", "datetime"),
                FieldSpec("最近人工调整时间", "datetime"),
            ],
        ),
        TableSpec(
            name="同步记录",
            fields=[
                FieldSpec("同步批次 ID", "text"),
                FieldSpec("同步状态", "select"),
                FieldSpec("同步范围", "text"),
                FieldSpec("选择书籍", "text"),
                FieldSpec("开始时间", "datetime"),
                FieldSpec("结束时间", "datetime"),
                FieldSpec("写入书籍数", "number"),
                FieldSpec("写入阅读记录数", "number"),
                FieldSpec("写入笔记划线数", "number"),
                FieldSpec("写入标签数", "number"),
                FieldSpec("错误摘要", "text"),
                FieldSpec("工具版本", "text"),
            ],
        ),
    ],
)


def core_table_names(schema: BaseSchema = BASE_SCHEMA) -> List[str]:
    return [table.name for table in schema.tables]


def table_by_name(name: str, schema: BaseSchema = BASE_SCHEMA) -> TableSpec:
    for table in schema.tables:
        if table.name == name:
            return table
    raise KeyError(f"Unknown table: {name}")


def field_names(table: TableSpec) -> Iterable[str]:
    return (field.name for field in table.fields)
