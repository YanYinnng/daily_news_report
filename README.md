# 微信公众号搜索与多主题日报 · v3.0

在 Windows 本机通过微信读书获取公众号近期文章与正文，支持公开微信文章直读、本地 OCR、分主题摘要、HTML/Markdown 日报、报告网页与邮件投递。提供独立的 Codex skill，可从其他聊天按需使用同一套读取能力。

本项目仅发布程序、测试和空白配置模板，不分发账号清单、登录会话、API 密钥、邮箱配置、日报或日志。

## v3.0 更新

- 微信读书成为默认来源，支持正常会话续期、账号身份校验、按推送组分页、访问受限时停止请求。
- 新增 `$wechat-public-articles` skill 和 `src.article_reader` 按需读取入口，不调用日报模型或邮件。
- 支持图片正文的 Windows 本地 OCR、长图分段识别、图文补读及日报图片筛选。
- 日报默认统计前一自然日，支持报告覆盖审计、失败补跑和成功投递后停止当天后续检查。
- 重组源码、操作脚本、诊断工具和文档，新增离线回归测试。

完整变更见 [CHANGELOG](CHANGELOG.md)。

## 1. 安装与配置

推荐 Windows 10/11、Python 3.12、PowerShell。微信读书路线不需要 Docker。

```powershell
git clone https://github.com/YanYinnng/daily_news_report.git
cd daily_news_report
.\scripts\setup.ps1
```

安装器创建 `.venv`、安装锁定依赖，并在缺失时从模板生成本地配置。已有文件不会被覆盖。升级 v2.0 时请先备份自己的账号、主题及 `.env`；v3.0 将实际个人配置与公开模板分开。

| 公开模板/配置 | 用途 |
|---|---|
| [`config/accounts.example.yaml`](config/accounts.example.yaml) | 公众号示例：名称、微信号、主题、公众号身份；复制成 `config/accounts.yaml` 后填写 |
| [`config/themes.example.yaml`](config/themes.example.yaml) | 分类示例；实际填写 `config/themes.yaml` |
| [`config/settings.yaml`](config/settings.yaml) | 通用来源、限速、运行时间、摘要和图片设置 |
| [`.env.example`](.env.example) | `LLM_*` 与 `SMTP_*`/`MAIL_*` 配置模板；实际填写 `.env` |

新安装的示例账号和主题默认禁用。需要读取指定公众号时，从其已核对的公开原文长链接获取 `__biz`，将该值填写到对应账号的 `wechat_fakeid`；启用真实配置后的主题与账号。另一种方式是将已经核对的身份保存在本机 `data/wechat_download_api_accounts.json`。首次安装空白身份缓存会由 setup 创建。不要根据同名账号猜测身份。

```powershell
# 只读本地检查，不请求公众号、模型或邮件
.\.venv\Scripts\python.exe -B -m src.main doctor --offline
# 首次登录；如有已启用的账号会检查一个，否则仅登录
.\.venv\Scripts\python.exe -B -m src.article_reader login
```

扫码得到的新会话只保存在本机 `data/`。看到登录成功后，还要实读列表和正文确认文章权限。

## 2. 按需查询与 Codex skill

```powershell
.\scripts\install_skill.ps1
```

在本机 Codex 中说：

> 使用 `$wechat-public-articles` 搜索指定公众号近期与 AI Agent 有关的文章，读原文并整理。

也可从项目根目录直接运行：

```powershell
$Python = '.\.venv\Scripts\python.exe'
& $Python -B -m src.article_reader status
& $Python -B -m src.article_reader accounts '公众号名称'
& $Python -B -m src.article_reader recent '公众号名称' --limit 3
# 将变量替换为已核对的公开文章地址，或 recent 返回的微信读书地址
& $Python -B -m src.article_reader read $ArticleUrl
# 公开图文文章需要补读海报等图中文字时
& $Python -B -m src.article_reader read $PublicArticleUrl --include-images
# 非配置内公众号：提供该号已核对、带 __biz 的原文长链接
& $Python -B -m src.article_reader recent $AccountName --source-url $VerifiedArticleUrl --limit 3
```

技能可以用公开索引按主题、标题寻找文章，再获取目标正文；`recent` 按账号读列表，不是全平台关键词搜索。不承诺覆盖未群发、已删除、付费或受限文章。纯图 OCR 会标明未人工核验。

如果 `-2012` 正常续期失败，请重新扫码；如果 `-2041` 或出现阅读授权、安全验证，应先停止请求，在手机微信读书的“书架 → 文章收藏”点击某个公众号名称，按实际提示操作并确认多篇文章列表可见，待本地保护期结束后再有限复查。详见[使用与排错](docs/OPERATIONS.md)及 [skill](skills/wechat-public-articles/SKILL.md)。

## 3. 日报与本机查看

需要自行配置启用的主题和账号、模型 API（可能产生模型费用），如需邮件再配置 SMTP。先按明确日期生成单主题本地报告；`--no-email` 禁止投递，但不会禁用摘要模型。

```powershell
# 明确某一天的文章窗口；示例日期请自行替换
.\.venv\Scripts\python.exe -B -m src.main run --calendar-day 2026-01-01 --theme ai --no-email
# 生成并发送全部启用主题的报告（省略 --no-email）
.\.venv\Scripts\python.exe -B -m src.main run --calendar-day 2026-01-01
# 只补发已生成报告，不重新摘要
.\.venv\Scripts\python.exe -B -m src.main send-report --date 2026-01-01 --calendar
```

自然日报告输出 `reports/calendar/YYYY-MM-DD/<theme>.md`、`.html`、`.audit.json`，SQLite 分别记录生成和投递状态。账号列表覆盖不完整时停止该主题摘要；正文或摘要失败时不发送不完整日报。

```powershell
.\scripts\start_project.ps1
# 停止查看服务
.\scripts\stop_project.ps1
```

本机报告查看：[http://127.0.0.1:8765/](http://127.0.0.1:8765/)。支持分类与日期切换，图片点击放大、原图及长图滚动。默认每篇最多 3 张附图、每个分类最多 30 张，使用本地 OCR 和图像特征筛选。

## 4. Windows 自动任务（可选）

默认不自动注册任务。完成手动验收后，按需执行：

```powershell
.\scripts\install_task.ps1 -TaskName WeChatDailyDigestNoon
# 只读检查
Get-ScheduledTask -TaskName WeChatDailyDigestNoon
# 暂停
Disable-ScheduledTask -TaskName WeChatDailyDigestNoon
Stop-ScheduledTask -TaskName WeChatDailyDigestNoon
```

任务默认 08:00 汇总前一天 00:00 至次日 00:00；当天尚未完成时 10:00—20:00 每两小时补跑，成功发送后移到次日，不唤醒电脑或阻止休眠，登录或恢复后补最近应执行日。任务名称沿用旧版本。

## 5. 目录结构

```text
src/                        核心业务逻辑
  article_reader.py         按需查询
  main.py                   日报 CLI
  task_runner.py            Windows 任务包装
  sources/                  来源适配器
  image_*.py                OCR 与图片筛选
  report*.py                报告及本地查看
config/                     通用配置和空白个人配置模板
skills/                     可安装的 Codex 技能
scripts/                    操作入口，legacy/、maintenance/ 分组
tests/                      离线回归测试
docs/                       操作与架构文档
data/, logs/, reports/      本机运行时生成，Git 忽略
```

## 6. 验证与贡献

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
```

测试使用离线样本和替身，不请求真实公众号、模型或邮件。提交变更前检查 Git 暂存文件，切勿提交 `.env`、真实 `accounts.yaml`、`themes.yaml`、会话及身份缓存、截图、个人文件或运行数据。仅在有权访问文章且遵守平台提示和使用范围时使用本工具。

- [运行、登录与故障排查](docs/OPERATIONS.md)
- [架构与模块索引](docs/ARCHITECTURE.md)
- [Codex skill 使用说明](skills/wechat-public-articles/SKILL.md)
