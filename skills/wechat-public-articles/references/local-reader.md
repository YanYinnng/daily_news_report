# 本机命令与结果

使用 PowerShell；当前目录不必是项目目录。安装版入口会调用项目的 `src.article_reader`，无需复制会话或另装依赖。在项目目录内也可直接执行 `.\.venv\Scripts\python.exe -B -m src.article_reader status`（其余子命令相同）。

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$CodexDir = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$SkillDir = Join-Path $CodexDir 'skills\wechat-public-articles'
$ProjectRoot = (Get-Content -LiteralPath (Join-Path $SkillDir 'project-root.txt') -Raw -Encoding UTF8).Trim()
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Reader = Join-Path $SkillDir 'scripts\wechat_articles.py'

# 离线检查：只确认文件是否存在和是否处于本地保护期，不证明线上可读
& $Python -B $Reader status

# 查本机配置过的公众号名称或 ID；查不到不能推断公众号不存在
& $Python -B $Reader accounts '公众号名称'

# 验证已配置公众号最新 3 篇文章的标题、发布时间和微信读书链接
& $Python -B $Reader recent '公众号名称' --limit 3

# 新公众号：参数必须是已核对的公众号名及该号带 __biz 的微信原文长链接
& $Python -B $Reader recent $AccountName --source-url $VerifiedPublicArticleUrl --limit 3

# 将搜索所得 mp.weixin.qq.com/s 链接或 recent 输出的完整 url 作为一个参数传入
& $Python -B $Reader read $ArticleUrl

# 读取公开微信页面时可再核对标题；预期标题不符不会返回正文
& $Python -B $Reader read $PublicArticleUrl --expected-title $ExpectedTitle

# 纯图片正文自动识别。公开图文文章包含海报、表格等时，可显式补读图中文字
& $Python -B $Reader read $PublicArticleUrl --include-images
```

`recent` 是按发布时间返回的近期列表，不做跨号全文搜索。找某一篇时，按标题和日期筛选结果，必要时适度增大 `--limit`（最多 50）；旧文章优先用网页搜索。`read` 返回 `body_chars`、`body` 和 `truncated`；若截断，可按需提高 `--max-chars`（最多 200000）。公网页面成功时也有本机 `local_audit`；只需读当前文章的结果，不批量读取无关审计。读到 `windows_ocr_unverified` 时注明 OCR 可能有错；读不到音视频内容时不要把该文章当成已读全文。

`busy` 表示项目采集锁正被使用，等待现有操作结束。`identity_not_bound` 或 `identity_not_resolved` 表示缺少可确认的公众号身份，核对本机名称或找可靠的微信原文长链接。`access_restricted` 或 `verification_required` 按技能正文处理；若有 `retry_after_unix`，用 `[DateTimeOffset]::FromUnixTimeSeconds([long]$Seconds).ToOffset([TimeSpan]::FromHours(8))` 换成北京时间。不要把本地保护状态当成一次新的线上失败。多次运行独立命令时，微信读书请求至少间隔 5 秒，避免并发；公开微信请求遇验证后停止该批次。

## 只在需要时重新扫码

```powershell
& $Python -B $Reader login
```

以可继续读取输出的终端会话执行。看到 `请用微信扫码并在手机确认微信读书登录` 后，在当前聊天显示本次新二维码（项目的 `data/weread_login.png`），等待用户操作，保留同一个正在等待确认的终端进程。不要展示任何会话文件、Cookie 或刷新凭据。默认选择一个已启用的公众号检查，可用 `login --account` 指定。没有配置账号时只登录，此时 `article_probe_ok` 为 false，不能宣称可读。出现 `login_probe_failed` 且 `fresh_local_session: true` 时，看已有诊断结果，区分登录与阅读权限，不盲目重新扫码。`login` 只在成功登录和列表检查之后处理旧 `-2012` 暂停，不解除阅读验证或限流状态。
