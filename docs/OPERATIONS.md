# 运行、登录与故障排查

所有命令默认在克隆的项目根目录执行，使用 `.\.venv\Scripts\python.exe` 运行 Python。在发布版本中 `config/accounts.yaml`、`config/themes.yaml`、`.env` 和 `data/` 不随 Git 分发；`scripts/setup.ps1` 会从公共模板补齐缺失文件。

## 微信读书授权与绑定

`data/weread_session.json` 是本机会话，`data/wechat_download_api_accounts.json` 是公众号身份缓存（历史命名，主路线仍为微信读书），`data/weread_health.json` 是访问保护记录。使用经过核对的公开原文长链接中 `__biz` 绑定账号；不能从名称猜测，也不要复制他人的访问凭据。

| 现象 | 处理 |
|---|---|
| 本地缺失登录或 `-2012` 正常续期失败 | `-m src.article_reader login`，需要检查特定账号时可加 `--account`。扫码确认；如果没有已启用账号，仅保存登录会话，尚未验证文章权限。 |
| 登录成功但文章报 `-2041`、验证页或 429 | 停止请求。手机微信读书 → 书架 → 文章收藏 → 点击已有公众号名称，按页面提示处理并确认可见多篇文章。没有收藏入口时，从微信文章 `… → 更多打开方式 → 微信读书` 进入。 |
| 本地保护暂停中 | `-m src.article_reader status` 检查，等待现有保护期结束；不要删除限流或阅读验证标记。 |
| 公众号身份未绑定 | 核对同一号带 `__biz` 的公开文章长链接；配置 `wechat_fakeid` 或用 `recent --source-url` 进行按需查询。 |
| 已知公开文章打不开 | 停止重复请求验证页，尝试可核对的同篇微信读书版本或发布者官网；不把搜索片段当全文。 |

普通登录与阅读权限彼此独立。扫码成功不代表能读取所有文章。使用正常登录、正常续期、低频串行请求及共享保护，不绕过人机验证。

## 日常运行

```powershell
# 配置和本地检查
.\scripts\setup.ps1
.\.venv\Scripts\python.exe -B -m src.main doctor --offline
# 检查某一确定的公众号近期列表
.\.venv\Scripts\python.exe -B -m src.article_reader recent $AccountName --limit 3
# 生成指定自然日、本地单主题报告
.\.venv\Scripts\python.exe -B -m src.main run --calendar-day 2026-01-01 --theme ai --no-email
# 最近一个应执行日补跑（不发邮件）
.\.venv\Scripts\python.exe -B -m src.main catch-up --no-email
# 只补发已有完整报告
.\.venv\Scripts\python.exe -B -m src.main send-report --date 2026-01-01 --calendar
```

日报使用配置的模型生成摘要；`--no-email` 仅禁用邮件。`--force` 会重新生成或重新发送，使用前核对日期和主题。补跑仅处理最近一个应执行日，不自动补齐全部历史缺报。每个主题的覆盖、发布窗口、摘要数、图片和异常在 `*.audit.json`，投递状态保存在 SQLite。

## 报告图片

纯图正文可用 Windows OCR，最多 20 图、每图 10 MiB，超长海报分段识别并标注待核对。公开图文也可用 `src.article_reader read ... --include-images` 补读图中文字。日报选图利用本地 OpenCV 与 OCR，在候选中优先相关图表、海报、流程图，过滤明显广告、装饰及低清晰度图片。每篇最多 3 张、每份分类最多 30 张；图像不能分析时可跳过候选。

只刷新既有报告图片，不重新调用摘要或发送邮件：

```powershell
.\.venv\Scripts\python.exe -B -m src.report_image_refresh --date 2026-01-01
# 从已记录的图片清单更新，必要时仍会访问图片 CDN
.\.venv\Scripts\python.exe -B -m src.report_image_refresh --date 2026-01-01 --cached-only
```

原报告备份至 `data/backups/`；遇验证页停止请求，保留未刷新部分。OCR 无法辨认的文字应核对原图。

## 自动任务

只有明确安装后才启用 Windows 任务。启用前配置有效账号、模型及所需 SMTP，先验证手动报告。任务默认 08:00 统计上一自然日，其余每两小时补跑到 20:00 和登录触发，全天全部分类完成投递时推迟全部触发器至次日。

```powershell
.\scripts\install_task.ps1 -TaskName WeChatDailyDigestNoon
Get-ScheduledTask -TaskName WeChatDailyDigestNoon
# 停用并结束计划任务（保留原任务配置）
Disable-ScheduledTask -TaskName WeChatDailyDigestNoon
Stop-ScheduledTask -TaskName WeChatDailyDigestNoon
```

任务设置允许电池运行、错过时恢复执行、忽略重叠实例，不唤醒、不阻止休眠。查看 `logs/task_YYYY-MM-DD.log` 与 `logs/daily_YYYY-MM-DD.log` 区分调度启动、文章访问与投递成功。只看历史报告时手动启动/停止 `scripts/start_project.ps1` 和 `scripts/stop_project.ps1`。
