# 微信公众号每日摘要

每天北京时间 12:00 汇总 `config/accounts.yaml` 中启用账号在前 24 小时内的新文章，生成 Markdown/HTML 日报并发送到邮箱。

## 当前策略

- 数据源优先使用公开站点接口：WordPress API、文章库 API、RSS；Just One API 只作兜底。
- 日报按公众号分组展示。
- 每篇推文单独获取正文、单独调用大模型生成摘要。
- 顶部总览只做事实说明，不把所有推文合并交给模型总结。
- 如果 12:00 电脑没运行，登录后通过启动项自动执行 `catch-up` 补跑。
- 同一天成功发过邮件后，后续 `catch-up` 不会重复发送。

## 常用命令

```powershell
.\.venv\Scripts\python.exe -B -m src.main check-source --account 示例公众号
.\.venv\Scripts\python.exe -B -m src.main run --no-email
.\.venv\Scripts\python.exe -B -m src.main run
.\.venv\Scripts\python.exe -B -m src.main catch-up
.\.venv\Scripts\python.exe -B -m src.main send-report --date 2026-05-26
```

## 安装或重装自动任务

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_task.ps1 -TaskName WeChatDailyDigest -IncludeStartupTrigger
```

如果 Windows 拒绝注册登录触发器，脚本会自动退化为：

- `WeChatDailyDigestNoon`：每天 12:00 运行。
- 启动文件夹中的 `WeChatDailyDigestCatchUp.cmd`：登录后补跑错过的日报。

## 文件位置

- 配置：`config/settings.yaml`、`config/accounts.yaml`
- 密钥：`.env`
- 日报：`reports/`
- 日志：`logs/`
- 运行状态：`data/state.sqlite3`

## 调整速度与成本

正式配置默认总结全部文章：

```yaml
max_articles_per_account: 0
max_total_articles: 0
summary_mode: individual
llm_parallel_workers: 3
```

如果某天文章太多，可以临时把 `max_articles_per_account` 改成 `2` 或 `3`，再运行 `run --no-email --force` 预览。

## 隐私说明

仓库中的公众号与主题配置均为占位示例。请在本地填写自己的配置，不要提交真实账号清单、邮件地址或凭据。
