# Reading Manager

Reading Manager 是一个 Python 命令行工具，用于把微信读书中的书籍、阅读进度、
划线、想法和日级阅读时长同步到飞书多维表格，并通过 OpenAI 兼容模型生成书籍级
主题标签。

项目支持本地手动运行，也支持在 Ubuntu VPS 上通过 systemd timer 定时运行。服务
不监听公网端口。

## 功能

- 同步最近阅读或有笔记的书籍。
- 同步书架中已读完的书籍。
- 按日期写入阅读进度快照。
- 写入划线、想法和书评，并按稳定键避免重复。
- 使用微信读书日级统计接口写入阅读时长。
- 使用 OpenAI 兼容 API 生成书籍级标签。
- 上传书籍封面到飞书附件字段。
- 补跑最近一段时间缺失的数据。
- 记录每次同步的状态和写入数量。

每日任务依次执行：

```text
sync-finished-books -> sync-catchup -> sync-covers
```

任一阶段失败时，其余阶段仍会执行，最终任务返回非零退出码。

## 数据流

```text
WeRead Agent Gateway
        |
        v
Reading Manager CLI ----> OpenAI-compatible model API
        |
        v
Lark Base
```

凭据不会写入仓库。macOS 本地运行可将微信读书和模型 API Key 保存到 Keychain；
Linux 部署使用权限为 `0600` 的环境文件。AI 打标会把待处理的书籍标题和笔记文本
发送给所配置的模型服务。

## 环境要求

- Python 3.9+
- `lark-cli` 1.0.44
- 微信读书 Agent Gateway API Key
- 可用的飞书应用及用户授权
- 一个包含六张目标表的飞书多维表格
- 可选的 OpenAI 兼容模型服务，默认配置为 DeepSeek

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

验证 CLI：

```bash
reading-manager doctor
```

未配置飞书资源时，`doctor` 会报告 `Lark Base: not configured`，但仍可完成离线
项目检查。

## 飞书表结构

创建以下六张表，名称和字段名应与代码中的
`src/reading_manager/schema.py` 保持一致。

### 书籍

`书名`、`微信读书 Book ID`、`作者`、`译者`、`出版社`、`ISBN`、`分类`、
`封面`、`封面图片`、`简介`、`阅读状态`、`阅读进度`、`最近阅读时间`、
`书籍级标签`、`首次同步时间`、`最近同步时间`、`数据来源`

### 阅读记录

`记录标题`、`书籍`、`微信读书 Book ID`、`记录日期`、`日级记录键`、
`阅读状态`、`阅读进度`、`阅读时长`、`最近阅读时间`、`读完时间`、
`同步批次`、`同步时间`

### 笔记划线

`笔记标题`、`书籍`、`微信读书 Note ID`、`类型`、`章节`、`位置`、
`划线原文`、`个人笔记`、`标签主题`、`创建时间`、`更新时间`、`内容哈希`、
`同步批次`、`最近同步时间`

### 每日阅读统计

`统计标题`、`统计日期`、`日级统计键`、`当天阅读时长（分钟）`、
`当天阅读时长（秒）`、`有效阅读日（1/0）`、`统计状态`、`同步批次`、
`计算时间`、`数据来源`、`备注`

### 标签主题

`标签名`、`规范化名称`、`标签说明`、`标签来源`、`适用层级`、
`最近生成时间`、`最近人工调整时间`

### 同步记录

`同步批次 ID`、`同步状态`、`同步范围`、`选择书籍`、`开始时间`、
`结束时间`、`写入书籍数`、`写入阅读记录数`、`写入笔记划线数`、
`写入标签数`、`错误摘要`、`工具版本`

关联字段需要分别关联到上面对应的目标表。表创建完成后，记录 Base Token 和六张表
的 Table ID。

## 配置

所有运行配置均通过环境变量提供。可以从
`deploy/reading-manager.env.example` 复制一份本地配置，但不要提交填写后的文件。

| 变量 | 必需 | 用途 |
| --- | --- | --- |
| `WEREAD_API_KEY` | 是 | 微信读书 Agent Gateway |
| `LARK_IDENTITY` | 是 | `user` 或 `bot` |
| `LARK_BASE_TOKEN` | 是 | 目标飞书 Base |
| `LARK_BASE_URL` | 否 | 仅用于 `doctor` 展示 |
| `LARK_TABLE_BOOKS` | 是 | 「书籍」表 ID |
| `LARK_TABLE_READING_RECORDS` | 是 | 「阅读记录」表 ID |
| `LARK_TABLE_NOTES` | 是 | 「笔记划线」表 ID |
| `LARK_TABLE_DAILY_STATS` | 是 | 「每日阅读统计」表 ID |
| `LARK_TABLE_TAGS` | 是 | 「标签主题」表 ID |
| `LARK_TABLE_SYNC_RUNS` | 是 | 「同步记录」表 ID |
| `LLM_API_KEY` | 打标时 | 模型 API Key |
| `LLM_BASE_URL` | 否 | 默认 `https://api.deepseek.com` |
| `LLM_MODEL` | 否 | 默认 `deepseek-v4-flash` |
| `LLM_TIMEOUT_SECONDS` | 否 | 默认 `45` |
| `LLM_MAX_ATTEMPTS` | 否 | 取值 `1` 至 `3`，默认 `3` |
| `READING_MANAGER_LARK_NOTIFY_USER_ID` | 否 | 失败通知用户 |
| `READING_MANAGER_LARK_NOTIFY_CHAT_ID` | 否 | 失败通知群聊 |

示例：

```bash
cp deploy/reading-manager.env.example .env.local
chmod 600 .env.local
# Edit .env.local and provide your own values.
set -a
. ./.env.local
set +a
```

示例文件中的资源 ID 和密钥为空，运行同步前必须填写到仓库外的私有副本中。

## 飞书授权

初始化 `lark-cli` 配置：

```bash
lark-cli config init --new --name reading-manager --brand feishu --lang zh
```

发起用户授权：

```bash
lark-cli auth login --domain base --domain im --no-wait --json
```

在浏览器完成确认后，用本次返回的设备码完成登录：

```bash
lark-cli auth login --device-code "<device-code>"
lark-cli auth status --verify
```

不要把 App Secret 或设备码提交到 Git。

## 本地使用

macOS 可以把 API Key 写入 Keychain：

```bash
reading-manager auth-weread-api-key
reading-manager auth-llm-api-key
```

常用命令：

```bash
reading-manager list-books --limit 20
reading-manager preview-sync --title "示例书籍甲"
reading-manager sync-weread --title "示例书籍甲"
reading-manager sync-daily
reading-manager sync-catchup --lookback-days 30
reading-manager sync-daily-stats --all
reading-manager retry-tagging
reading-manager sync-covers
reading-manager run-daily
```

`preview-sync` 不写入飞书。其他同步命令会使用 Base 中的稳定键进行查询和 upsert。

## VPS 定时运行

仓库提供：

- `deploy/install-vps-release.sh`
- `deploy/configure-vps-secrets.sh`
- `deploy/systemd/reading-manager-daily.service`
- `deploy/systemd/reading-manager-daily.timer`

默认 timer 每天北京时间 23:30 运行，并启用 `Persistent=true`。完整发布、授权、
验收和回滚步骤见 [Ubuntu VPS 部署与运维](docs/deployment-vps.md)。

## 测试

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest discover -s tests -v
bash -n deploy/*.sh
```

测试只使用合成数据，不应包含真实账号、阅读内容或资源 ID。

## 仓库结构

```text
.
├── README.md
├── AGENTS.md
├── PROJECT_SUMMARY.md
├── pyproject.toml
├── deploy/
├── docs/
│   └── deployment-vps.md
├── src/
│   └── reading_manager/
└── tests/
```

## 隐私与安全

- 不提交 `.env`、Cookie、Token、API Key、App Secret 或设备码。
- 不提交真实笔记、划线、书单、阅读统计、日志或调试导出。
- 不在命令参数、systemd unit 或日志中写入密钥。
- 生产配置放在仓库外，并限制文件权限。
- 发布前同时扫描工作树和完整 Git 历史。
- 如果敏感值曾进入 Git，仅删除当前文件不够，应重新生成干净历史并轮换凭据。

## 已知限制

- 微信读书接口和 Gateway skill 版本可能变化。
- 飞书表名、字段名和关联关系必须与 schema 一致。
- 用户授权会过期，需要重新执行 Device Flow。
- AI 输出经过结构校验，但标签质量仍取决于模型和输入内容。
- 项目当前没有 Web 管理界面。

## 许可证

本仓库暂未附带开源许可证。公开可见不等于授予复制、修改或分发许可。
