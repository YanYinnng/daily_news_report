# 微信公众号多主题每日摘要

新对话中的 Codex 接手时，建议先读 `HANDOFF.md`，再读本 README。`HANDOFF.md` 记录通用维护建议；本 README 记录长期维护方法。

这个项目每天北京时间 12:00 汇总启用主题下各公众号在前 24 小时内的新文章，按主题分别生成 Markdown/HTML 日报，并按主题分别发送邮件。电脑 12:00 未运行时，会在下次开机或登录后执行 `catch-up` 补跑缺失主题。

## 当前状态

- 公众号配置：`config/accounts.yaml`，请在本地配置自己的公众号。
- 主题配置：`config/themes.yaml`，请在本地配置自己的主题。
- 报告模式：每个主题一份报告，每篇推文单独总结。
- 邮件模式：每个主题一封邮件，标题包含主题名和日期。
- 数据源顺序：`wechat-download-api` 后台文章列表源为主链路，稳定渠道、Just One、opencli 依次兜底。
- 自动任务：Windows 任务计划程序每天 12:00 运行，错过后通过登录/启动补跑。
- 本地浏览：`serve-reports` 可在 `http://127.0.0.1:8765` 查看历史日报，兼容多主题目录和旧版扁平报告。

## 项目结构

```text
config/
  accounts.yaml       公众号清单
  settings.yaml       全局运行设置
  themes.yaml         主题开关和邮件标题前缀
scripts/
  setup.ps1           创建虚拟环境并安装依赖
  install_task.ps1    安装 Windows 自动任务
  run_daily.ps1       自动任务实际调用入口
  setup_wechat_download_api.ps1  准备本地 wechat-download-api 服务
src/
  main.py             CLI 入口
  report.py           Markdown/HTML 报告生成
  report_viewer.py    本地日报浏览网页和 API
  summarizer.py       LLM 摘要
  mailer.py           邮件发送
  source_health.py    数据源健康缓存
  sources/            数据源适配器
data/                 运行状态、来源审计、健康缓存
logs/                 日常任务日志
reports/              每日生成的报告
```

`data/`、`logs/`、`reports/`、`.env`、`.venv/` 和导入用的 Excel 文件不会提交到 Git。

## 首次安装

```powershell
Set-Location "<项目目录>"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

然后复制 `.env.example` 为 `.env`，填入密钥和邮箱配置：

```text
JUSTONE_API_TOKEN=
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=
SMTP_HOST=
SMTP_PORT=465
SMTP_USERNAME=
SMTP_PASSWORD=
MAIL_FROM=
MAIL_TO=
```

`MAIL_TO` 只发给自己时填一个邮箱即可；多个收件人用英文逗号分隔。

## 常用命令

所有命令都在 `项目根目录` 下执行：

```powershell
.\.venv\Scripts\python.exe -B -m src.main run --no-email
.\.venv\Scripts\python.exe -B -m src.main run --no-email --theme ai
.\.venv\Scripts\python.exe -B -m src.main run
.\.venv\Scripts\python.exe -B -m src.main catch-up
.\.venv\Scripts\python.exe -B -m src.main send-report --date 2026-06-03 --theme ai
.\.venv\Scripts\python.exe -B -m src.main test-email
.\.venv\Scripts\python.exe -B -m src.main send-login-reminder --dry-run
.\.venv\Scripts\python.exe -B -m src.main check-source --account 示例公众号
.\.venv\Scripts\python.exe -B -m src.main check-source --account 示例公众号 --recent
.\.venv\Scripts\python.exe -B -m src.main probe-justone
.\.venv\Scripts\python.exe -B -m src.main validate-accounts
.\.venv\Scripts\python.exe -B -m src.main serve-reports --host 127.0.0.1 --port 8765
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_wechat_download_api.ps1
```

`run --no-email` 只生成文件，不发邮件。`run` 会生成并发送所有启用主题。`catch-up` 只补跑还没成功生成/发送的主题。

## 本地报告浏览器

这个网页只绑定本机地址，不申请 HTTPS，默认入口：

```text
http://127.0.0.1:8765
```

前台运行，窗口保持打开时服务保持运行，按 `Ctrl+C` 停止：

```powershell
.\.venv\Scripts\python.exe -B -m src.main serve-reports --host 127.0.0.1 --port 8765
```

后台运行：

```powershell
$p = Start-Process -FilePath .\.venv\Scripts\python.exe -ArgumentList "-B","-m","src.main","serve-reports","--host","127.0.0.1","--port","8765" -WorkingDirectory (Get-Location) -PassThru -WindowStyle Hidden
$p.Id
```

查看是否正在运行：

```powershell
Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen
```

关闭后台进程：

```powershell
$conn = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen
Stop-Process -Id $conn.OwningProcess
```

如果启动时提示端口被占用，可以把 `--port 8765` 换成其他端口。网页接口包括：

```text
/api/reports
/api/report?date=YYYY-MM-DD&theme=<theme_id>&format=html
/api/report?date=YYYY-MM-DD&theme=<theme_id>&format=md
```

新版报告读取 `reports/YYYY-MM-DD/<theme>.html`；旧版 `reports/YYYY-MM-DD.html` 会显示为“全部主题（旧版）”。

## 自动任务

安装或重装自动任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_task.ps1 -TaskName WeChatDailyDigest -IncludeStartupTrigger
```

如果 Windows 不允许注册完整的登录/启动触发器，脚本会退化为：

- `WeChatDailyDigestNoon`：每天 12:00 运行。
- 启动文件夹中的 `WeChatDailyDigestCatchUp.cmd`：登录后补跑错过的日报。

检查任务状态：

```powershell
schtasks /Query /TN WeChatDailyDigestNoon /V /FO LIST
```

自动任务调用的是 `scripts/run_daily.ps1`，该脚本会写入 `logs/task_YYYY-MM-DD.log`。

## 配置说明

`config/themes.yaml` 控制主题是否生成。把某个主题的 `enabled` 改成 `false` 后，该主题不会生成报告，也不会发邮件。

`config/accounts.yaml` 控制公众号。新增公众号时至少填写：

```yaml
- name: 示例公众号
  wxid: example_wxid
  theme: ai
  enabled: true
  opencli_query: 示例公众号
  wechat_fakeid: ""
  source_priority:
    - official
    - wechat_api
    - justone
    - opencli
```

如果有稳定渠道，优先补充 `rss_url`、`wordpress_api_url` 或 `article_library_api_url`。`source_priority` 目前作为配置说明和后续切换预留；实际运行顺序仍按全局策略执行。

`config/settings.yaml` 里常用设置：

```yaml
daily_run_time: "12:00"
lookback_hours: 24
summary_mode: individual
send_empty_report: true
include_images: auto
max_images_per_article: 2
max_total_images_per_theme: 20
opencli_verify_candidates: true
opencli_max_opened_candidates_per_account: 3
login_reminder_interval_days: 3
wechat_download_api:
  enabled: true
  base_url: "http://localhost:5000"
  account_cache_path: data/wechat_download_api_accounts.json
  search_count: 5
  articles_page_size: 50
  max_pages: 3
  fetch_full_content: true
  request_interval_seconds: 1.5
  article_fetch_interval_seconds: 3.0
  freq_control_sleep_seconds: 75
  freq_control_retries: 1
```

## 数据源策略

运行时按这个顺序采集：

1. `wechat-download-api`：通过微信公众号后台登录态按 `fakeid` 拉取公众号文章列表，并按 12:00-12:00 日报窗口过滤。
2. 稳定渠道：WordPress API、文章库 API、RSS，仅在主链路失败时兜底。
3. Just One：只有 `config/settings.yaml` 中启用且 `data/source_health.json` 没有临时禁用时才使用。
4. opencli 微信搜索：最后兜底，会打开候选微信原文页，核验公众号名和发布时间后才收录。

`probe-justone` 如果发现 Just One 不可用，会把状态写入 `data/source_health.json`，在缓存期内跳过 Just One，避免空跑和扣费。

### wechat-download-api 接入

参考实现来自 [adennng/wechat-query-skill](https://github.com/adennng/wechat-query-skill)。它内置的 `wechat-download-api` 需要本地 Docker 部署，并用公众号管理员微信扫码登录微信公众平台。登录后可调用 `GET /api/public/searchbiz` 搜索公众号并获取 `fakeid`，再调用 `GET /api/public/articles` 按公众号拉取文章列表。

本项目只接入它的 HTTP API，不复制服务源码。准备服务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_wechat_download_api.ps1
```

脚本完成后访问 `http://localhost:5000/login.html` 扫码。确认登录成功后，运行：

```powershell
.\.venv\Scripts\python.exe -B -m src.main validate-accounts --limit 3
```

如果某些账号无法自动匹配，可在 `config/accounts.yaml` 对应账号下补 `wechat_fakeid`。`data/wechat_download_api_accounts.json` 会缓存已匹配的 `fakeid`，属于运行数据，不提交 Git。

### 登录提醒邮件

`scripts/run_daily.ps1` 会在每日 `catch-up` 后调用：

```powershell
.\.venv\Scripts\python.exe -B -m src.main send-login-reminder
```

该命令使用 `data/state.sqlite3` 记录上次提醒时间，满 `login_reminder_interval_days` 天才发送一次。当前配置为每 3 天提醒一次。邮件标题和正文写死在 `src/main.py` 中，用于提醒打开 `http://localhost:5000/login.html` 扫码登录。

验证脚本但不发邮件：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_daily.ps1 -SkipCatchUp -ReminderDryRun
```

## 报告和审计

多主题报告保存在：

```text
reports/YYYY-MM-DD/<theme_id>.md
reports/YYYY-MM-DD/<theme_id>.html
```

旧版历史报告仍保留在：

```text
reports/YYYY-MM-DD.md
reports/YYYY-MM-DD.html
```

来源审计保存在：

```text
data/source_audit.json
data/source_audit.md
```

运行状态保存在：

```text
data/state.sqlite3
```

如果某天日报异常，优先看：

```text
logs/task_YYYY-MM-DD.log
logs/daily_YYYY-MM-DD.log
data/source_audit.md
data/source_health.json
```

## 维护建议

- 新增公众号后，先跑 `validate-accounts --theme <theme_id>` 或 `check-source --account 示例公众号 --recent`。
- 启用 `wechat-download-api` 后，优先看 `validate-accounts` 输出中 “微信后台 API 精确来源” 的账号数量。
- 改主题开关后，先跑 `run --no-email --theme <theme_id>` 预览。
- 改本地报告浏览器后，先跑 `serve-reports` 并检查 `/api/reports` 和一个具体 `/api/report`。
- 正式发邮件前，先跑 `test-email`。
- opencli 兜底源比较慢，公众号很多时不要随意调大 `opencli_max_opened_candidates_per_account`。
- 导入 Excel 只用于生成配置，运行时以 `config/accounts.yaml` 为准。

## 隐私说明

仓库中的公众号与主题配置均为占位示例。请在本地填写自己的配置，不要提交真实账号清单、邮件地址或凭据。
