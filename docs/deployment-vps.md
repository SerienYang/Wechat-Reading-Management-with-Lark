# Ubuntu VPS 部署与运维

本文说明如何将 Reading Manager 部署到 Ubuntu VPS，并使用 systemd timer
每天在指定时区运行。

## 运行结构

默认路径如下：

```text
/opt/reading-manager/current                   当前版本
/opt/reading-manager/releases/<release-sha>    历史版本
/opt/reading-manager/tools/                    lark-cli
/var/lib/reading-manager/                      服务用户状态目录
/etc/reading-manager/reading-manager.env       私有运行配置
```

服务以独立低权限用户 `reading-manager` 运行，不监听网络端口，只主动访问微信读书、
模型 API 和飞书。

## 前置条件

- Ubuntu 22.04 或更新版本。
- root 或具备等价系统管理权限的账号。
- Python 3.9+、Node.js 和 npm。
- 可访问 PyPI、npm、微信读书、模型 API 和飞书 API。
- 已创建包含项目所需六张表的飞书多维表格。
- 已准备微信读书 Agent Gateway API Key 和模型 API Key。

在本机设置连接信息：

```bash
export VPS_HOST="<vps-host>"
export SSH_KEY="$HOME/.ssh/<ssh-key-file>"
```

## 发布版本

在项目根目录执行：

```bash
release_sha="$(git rev-parse --short=12 HEAD)"
archive="/tmp/reading-manager-${release_sha}.tar.gz"

git archive --format=tar.gz --output="${archive}" HEAD
scp -i "${SSH_KEY}" \
  "${archive}" \
  deploy/install-vps-release.sh \
  "root@${VPS_HOST}:/tmp/"
ssh -i "${SSH_KEY}" "root@${VPS_HOST}" \
  "bash /tmp/install-vps-release.sh \
    /tmp/reading-manager-${release_sha}.tar.gz ${release_sha}"
```

安装脚本会：

- 创建低权限系统用户。
- 安装固定版本的 `lark-cli`。
- 按 Git SHA 创建发布目录和 Python 虚拟环境。
- 在服务器上运行单元测试。
- 安装 systemd unit。
- 原子切换 `current` 符号链接。
- 保持 timer 关闭，等待配置和鉴权验证。

## 获取飞书资源 ID

项目需要一个 Base Token 和六张表的 ID：

| 环境变量 | 对应表 |
| --- | --- |
| `LARK_TABLE_BOOKS` | 书籍 |
| `LARK_TABLE_READING_RECORDS` | 阅读记录 |
| `LARK_TABLE_NOTES` | 笔记划线 |
| `LARK_TABLE_DAILY_STATS` | 每日阅读统计 |
| `LARK_TABLE_TAGS` | 标签主题 |
| `LARK_TABLE_SYNC_RUNS` | 同步记录 |

Base Token 位于多维表格链接中。表 ID 可通过飞书开放平台、`lark-cli` 或浏览器地址
获取。不要把真实值写入仓库、Issue 或日志。

`LARK_BASE_URL` 仅用于 `doctor` 展示，可以留空。

## 配置运行环境

通过交互脚本输入配置，输入内容不会回显：

```bash
ssh -t -i "${SSH_KEY}" "root@${VPS_HOST}" \
  /opt/reading-manager/current/deploy/configure-vps-secrets.sh
```

脚本写入：

```text
/etc/reading-manager/reading-manager.env
```

只检查文件所有者和权限，不读取文件内容：

```bash
ssh -i "${SSH_KEY}" "root@${VPS_HOST}" \
  "stat -c '%U:%G %a %n' \
    /etc/reading-manager/reading-manager.env"
```

预期权限为：

```text
root:root 600 /etc/reading-manager/reading-manager.env
```

模型默认为 DeepSeek 的 OpenAI 兼容接口。可在服务器环境文件中修改
`LLM_BASE_URL` 和 `LLM_MODEL`，但模型地址必须使用 HTTPS。

## 配置飞书授权

以下命令必须以服务用户执行，并固定 `HOME`：

```bash
sudo -u reading-manager env HOME=/var/lib/reading-manager \
  /opt/reading-manager/tools/node_modules/.bin/lark-cli \
  config init --new --name reading-manager --brand feishu --lang zh
```

如果已有应用凭据，也可以通过标准输入初始化，避免 App Secret 出现在命令历史：

```bash
sudo -u reading-manager env HOME=/var/lib/reading-manager \
  /opt/reading-manager/tools/node_modules/.bin/lark-cli \
  config init --app-id "<app-id>" --app-secret-stdin
```

发起 Device Flow：

```bash
sudo -u reading-manager env HOME=/var/lib/reading-manager \
  /opt/reading-manager/tools/node_modules/.bin/lark-cli \
  auth login --domain base --domain im --no-wait --json
```

在浏览器完成授权后，使用本次返回的设备码完成登录：

```bash
sudo -u reading-manager env HOME=/var/lib/reading-manager \
  /opt/reading-manager/tools/node_modules/.bin/lark-cli \
  auth login --device-code "<device-code>"
```

验证身份：

```bash
sudo -u reading-manager env HOME=/var/lib/reading-manager \
  /opt/reading-manager/tools/node_modules/.bin/lark-cli \
  auth status --verify
```

设备码只能使用一次。授权过期后应重新发起 Device Flow。

## 上线检查

验证 systemd 配置：

```bash
systemd-analyze verify \
  /etc/systemd/system/reading-manager-daily.service \
  /etc/systemd/system/reading-manager-daily.timer
```

检查应用配置，不输出 Token 和表 ID：

```bash
set -a
. /etc/reading-manager/reading-manager.env
set +a

sudo --preserve-env=WEREAD_API_KEY,LLM_API_KEY,LLM_BASE_URL,LLM_MODEL,LLM_TIMEOUT_SECONDS,LLM_MAX_ATTEMPTS,LARK_IDENTITY,LARK_BASE_TOKEN,LARK_BASE_URL,LARK_TABLE_BOOKS,LARK_TABLE_READING_RECORDS,LARK_TABLE_NOTES,LARK_TABLE_DAILY_STATS,LARK_TABLE_TAGS,LARK_TABLE_SYNC_RUNS \
  -u reading-manager env HOME=/var/lib/reading-manager \
  /opt/reading-manager/current/.venv/bin/reading-manager doctor
```

执行只读微信读书探针：

```bash
sudo --preserve-env=WEREAD_API_KEY \
  -u reading-manager env HOME=/var/lib/reading-manager \
  /opt/reading-manager/current/.venv/bin/reading-manager \
  list-books --limit 1
```

执行只读飞书探针：

```bash
sudo --preserve-env=LARK_BASE_TOKEN,LARK_IDENTITY \
  -u reading-manager env HOME=/var/lib/reading-manager \
  /opt/reading-manager/tools/node_modules/.bin/lark-cli \
  base +base-get --base-token "${LARK_BASE_TOKEN}" --as "${LARK_IDENTITY}"
```

确认探针成功后，手动运行完整同步：

```bash
systemctl start reading-manager-daily.service
systemctl show reading-manager-daily.service \
  --property=Result,ExecMainStatus,ActiveState,SubState
journalctl -u reading-manager-daily.service --since "10 minutes ago" --no-pager
```

核对写入结果后再启用定时器：

```bash
systemctl enable --now reading-manager-daily.timer
systemctl list-timers reading-manager-daily.timer --no-pager
```

## 手动补跑

默认任务检查最近 30 天。补跑更长窗口前，先确认日常服务未运行：

```bash
systemd-run \
  --unit=reading-manager-catchup \
  --description="Reading Manager catch-up" \
  --uid=reading-manager \
  --gid=reading-manager \
  --working-directory=/opt/reading-manager/current \
  --property=EnvironmentFile=/etc/reading-manager/reading-manager.env \
  --property=TimeoutStartSec=60min \
  --property=PrivateTmp=true \
  --property=NoNewPrivileges=true \
  --setenv=HOME=/var/lib/reading-manager \
  --setenv=PATH=/opt/reading-manager/tools/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  --no-block \
  /opt/reading-manager/current/.venv/bin/reading-manager \
  sync-catchup --lookback-days 90
```

查看进度：

```bash
systemctl status reading-manager-catchup.service
journalctl -u reading-manager-catchup.service -f
```

## 日常运维

```bash
systemctl status reading-manager-daily.timer
systemctl status reading-manager-daily.service
journalctl -u reading-manager-daily.service -n 200 --no-pager
```

临时停止定时任务：

```bash
systemctl disable --now reading-manager-daily.timer
```

## 回滚

停止 timer，选择已验证的旧版本，再原子切换链接：

```bash
systemctl disable --now reading-manager-daily.timer
ls -1 /opt/reading-manager/releases
ln -sfn "/opt/reading-manager/releases/<previous-release-sha>" \
  /opt/reading-manager/.current-rollback
mv -Tf /opt/reading-manager/.current-rollback \
  /opt/reading-manager/current
systemctl daemon-reload
```

回滚后重新运行上线检查。只有只读探针全部通过，才能再次执行同步并启用 timer。
