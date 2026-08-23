# AGENTS.md

本文件记录本仓库内 coding agent 的协作规则。

## 项目定位

Reading Manager 是一个 Python CLI，用于把微信读书数据同步到飞书多维表格，并
通过 OpenAI 兼容模型生成书籍级标签。项目默认本地优先，不提供 Web 服务。

## 进入项目

开始修改前：

1. 运行 `git rev-parse --show-toplevel`。
2. 阅读 `README.md`、`AGENTS.md` 和 `PROJECT_SUMMARY.md`。
3. 检查 `git status --short --branch`。
4. 区分本次任务修改和已有修改。
5. 修改前简要说明范围和验证方式。

## 目录职责

- `src/reading_manager/`：应用代码。
- `tests/`：单元测试和合成测试数据。
- `deploy/`：VPS 配置模板、安装脚本和 systemd unit。
- `docs/`：面向使用者的补充文档。
- `tmp/`：本地临时文件，不进入 Git。

不要新增与任务无关的顶层目录。

## 开发规则

- 使用 Python 3.9+ 标准库，除非需求明确要求新增依赖。
- 优先复用现有模块、异常类型和命名约定。
- 行为变更先写失败测试，再做最小实现。
- 不为了局部任务重写整个同步流程。
- 注释只解释不直观的约束和边界。

## 隐私规则

- 不提交 API Key、Cookie、Token、App Secret、设备码或 `.env` 文件。
- 不提交真实书单、划线、笔记、阅读统计或同步批次。
- 测试和示例只使用合成数据。
- 飞书 Base Token、Table ID 和通知目标必须由环境变量提供。
- 不在日志和异常中输出密钥、请求正文或模型响应正文。
- 调试输出和导出文件放入 `tmp/`。
- 发布前检查当前文件和完整 Git 历史。

## 验证

Python 变更：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest discover -s tests -v
```

部署脚本：

```bash
bash -n deploy/*.sh
```

systemd 变更应在 Ubuntu 上补充运行：

```bash
systemd-analyze verify \
  deploy/systemd/reading-manager-daily.service \
  deploy/systemd/reading-manager-daily.timer
```

## Git

- 只暂存当前任务文件，不使用 `git add .`。
- 提交前检查密钥、日志、缓存、构建产物和大文件。
- 使用 Conventional Commits。
- 未经用户明确确认，不创建远程仓库，不推送，不重写共享历史。
