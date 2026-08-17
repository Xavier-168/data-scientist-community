# GitHub 仓库上架资料

以下字段均可直接复制。上架前仍须完成 `RELEASE_GATE.md` 中的人工验收项。

## Repository name

`data-scientist-community`

## GitHub description

`Local-first macOS creator analytics for Douyin, Xiaohongshu, Bilibili and Kuaishou, with Excel export and optional Feishu sync.`

## About 短句

中文：`在自己的 Mac 上整理自己有权访问的四平台创作者数据。`

English: `Local-first creator data collection, Excel export, and optional Feishu sync.`

## Topics

`creator-tools`, `creator-analytics`, `data-analysis`, `playwright`, `python`, `tauri`, `excel`, `feishu`, `douyin`, `xiaohongshu`, `bilibili`, `kuaishou`, `macos`, `local-first`, `agpl`

## Website

首发时留空。不填写商业激活、客户下载或个人联系地址。

## Social preview

使用候选仓库中的原创社区占位图标，配合纯文字标题“数据科学家 Community”和副标题“Local-first creator analytics”。不使用四个平台或飞书 Logo，不写“官方”“授权”“完全合规”、虚构用户数、虚构准确率或未执行的测试徽章。

## 当前 Release 标题

`v0.1.0-rc.2 — Collection Integrity & Native Export Fixes`

## 当前 Release 正文

```markdown
## Data Scientist Community v0.1.0-rc.2

这是社区源码候选的第二轮稳定性修复，重点处理四平台采集完整性、B站官方批次与进度、抖音长任务恢复、macOS 原生 Excel 保存、飞书首次初始化和 Figma 仪表盘布局。本项目是独立社区项目，与抖音、小红书、哔哩哔哩、快手、飞书及其关联公司不存在官方合作、授权或背书关系。

### 重点修复

- B站按日期锁定目标、真实滚动确认、有效官方批次导出和单调进度；
- 抖音分页慢响应保护、瞬态重试和部分结果保留；
- 小红书、快手页面滚动与接口分页双重确认；
- 应用内 Excel 范围选择与 macOS 系统保存窗口；
- 飞书首次初始化的自动打开、可信 URL 限制和明确超时；
- 1920 × 1080 设计基线下统一缩放的本地仪表盘。

完整修复、验证结果、升级步骤与人工边界见 [v0.1.0-rc.2 发布说明](https://github.com/Xavier-168/data-scientist-community/blob/main/docs/RELEASE_NOTES_v0.1.0-rc.2.md)。

### 本地数据与登录态

- Cookie、浏览器 Profile、日志和导出数据默认保存在当前用户的 `~/Library/Application Support/数据科学家 Community/`，不写入源码目录。
- 平台登录、扫码和重新授权必须由账号本人在可见浏览器窗口中完成。
- 服务运行期间默认每 6 小时检查已授权平台的登录态。HTTP 503、网络异常或不稳定页面会记为 `unknown` 并保留上次 `authorized`；只有明确的未登录/Cookie 失效证据才提示重新授权。
- 登录态检查不会自动刷新 Cookie，也不承诺登录永不过期。

### 运行边界

- 当前发布的是源码候选，不包含已签名或已公证的 DMG。
- 建议环境：macOS 11+、Python 3.11+（已实测 3.12）、Node.js `>=22.12.0 <23` 和 Git。
- 普通源码运行不需要 Rust；构建或测试可选 Tauri 桌面壳时需要 Rust stable 与 Apple 平台构建环境。
- 安装前先执行 `python3 -c "import ssl, sqlite3, xml.parsers.expat"`。如遇 `dlopen`、`pyexpat` 或 `ensurepip` 错误，请重装完整 Python 3.12，并丢弃创建失败的 `.venv` 后重试。

### 使用前请了解

- 平台页面和接口可能变化，真实账号授权与端到端采集仍需人工验收。
- 频繁访问可能触发频控、验证码、安全校验或账号风控。本项目不提供绕过这些措施的功能承诺。
- 飞书同步会将用户选择的数据发送到用户自己的飞书租户，因此不是纯离线功能。
- 只处理你有权访问的数据，并遵守适用的平台条款、账号规则、隐私法律和内容权利。

### 安装与验证

安装命令、Python 3.12 排错、完整测试和数据边界说明见仓库根目录 `README.md`、`README_EN.md`、`PRIVACY.md` 与 `RELEASE_GATE.md`。

### License

社区代码采用 GNU AGPL-3.0-only。商业许可与社区仓库之外的能力说明见仓库文档。
```

## 置顶介绍

> 在自己的 Mac 上整理自己有权访问的四平台创作者数据。Cookie/Profile 和运行数据默认留在用户目录，飞书由用户可选配置。项目独立且非平台官方产品。

## 建议启用的 GitHub 功能

- Discussions；
- Issues 与仓库中的模板；
- Private Vulnerability Reporting；
- Branch protection 与必需测试；
- 在 `RELEASE_GATE.md` 的硬性阻塞清零并由维护者完成发布决策前，不启用自动上传或自动发布。
