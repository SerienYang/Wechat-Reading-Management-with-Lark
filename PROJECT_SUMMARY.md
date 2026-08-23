# 项目摘要

## 用途

Reading Manager 是一个 Python CLI，用于将微信读书中的书籍、阅读进度、划线、
想法和日级阅读统计同步到飞书多维表格，并通过 OpenAI 兼容模型生成书籍级标签。

## 当前能力

- 微信读书 Agent Gateway 数据读取。
- 飞书多维表格查询、创建和更新记录。
- 书籍、阅读快照、笔记、统计、标签和同步批次管理。
- 稳定键去重和缺失日期补跑。
- OpenAI 兼容模型 JSON 打标及有限重试。
- 封面附件下载和上传。
- macOS Keychain 凭据保存。
- Ubuntu systemd service 和 timer 部署。

## 配置

运行时需要：

- 微信读书 API Key。
- 飞书用户或机器人身份。
- Base Token 和六张表的 Table ID。
- 执行 AI 打标时需要模型 API Key。

所有用户资源标识和凭据均通过环境变量或系统凭据存储提供，源码中不包含具体账号
配置。

完整变量说明见 `README.md` 和 `deploy/reading-manager.env.example`。

## 结构

```text
src/reading_manager/    应用代码
tests/                  单元测试和合成数据
deploy/                 VPS 安装、配置和 systemd 文件
docs/                   补充使用文档
```

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest discover -s tests -v
bash -n deploy/*.sh
```

## 重要约束

- 不提交真实凭据、账号标识、阅读数据、日志或导出文件。
- 测试和示例必须使用合成数据。
- 模型地址必须使用 HTTPS。
- 模型请求最多尝试三次。
- 定时任务中的单阶段失败不能阻止后续阶段执行。
- 自动同步只能有一个调度源，避免并发写入。

## 已知限制

- 微信读书接口和 Gateway skill 可能发生变化。
- 飞书表结构需要由使用者预先创建。
- 飞书用户授权到期后需要重新授权。
- 当前不提供 Web 管理界面。

## 接手方式

1. 阅读 `README.md` 和 `AGENTS.md`。
2. 检查 Git 状态。
3. 运行完整测试。
4. 使用合成配置执行 `reading-manager doctor`。
5. 涉及真实同步前，单独核对凭据、权限和目标 Base。
