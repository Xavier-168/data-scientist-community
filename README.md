# 数据科学家 Community

本地优先的创作者数据采集、规范化与分析工具：在用户自己的 Mac 上连接抖音、小红书、B站和快手创作者后台，生成本地 Excel，并可由用户主动同步到飞书多维表格。

> 本项目是独立社区项目，与抖音、小红书、哔哩哔哩、快手、飞书及其关联公司不存在官方合作、授权或背书关系。

English documentation: [README_EN.md](README_EN.md)

最新候选版本：[`v0.1.0-rc.2` 发布说明](docs/RELEASE_NOTES_v0.1.0-rc.2.md)。

## 写在开源之前

Hi，你们好呀！我是逍遥，抖音科技博主“赵逍遥 Xavier”。这是我上传的第一个 GitHub 仓库。自从成为一名 Vibe Coder，我几乎每天都在做一些真正能帮到自己的工具和工作流。

“数据科学家”就是其中一个，而且我相信，它也能帮到正在做自媒体的你。

作为一名全职 OPC（一人公司）创业者，我需要持续复盘自己的内容：哪些做对了，哪些还不够好，下一次应该怎么调整。因此，我想把各个平台的详细数据整理到自己的飞书里，再结合仪表盘进行更系统的分析。

但四个平台的数据指标和字段口径并不一致，很难直接放在一起做统计和对比。所以，我手动梳理、映射并对齐了抖音、小红书、B站和快手的核心字段，让四个平台的数据尽量归一化，方便我们后续复盘和分析。

数据科学家会高频的更新，会成为我工作场景中必不缺少的工作台，更多有趣的功能可以等待～

当前版本先支持 macOS，Windows 版本也在计划中。如果这个项目对你有帮助，欢迎点一个 Star，也欢迎把你的真实使用感受和建议告诉我。

想了解更多关于逍遥的信息，可以来我的OPC官网：[https://zhaoxiaoyao.com](https://zhaoxiaoyao.com)。开源项目与网站上的学习资源、商业服务彼此独立；如有收费内容，以官网页面标注为准。

不见不散～

## 解决的问题

创作者经常需要在多个后台重复登录、导出、清洗和合并数据。本项目把四个平台的浏览器自动化、本地数据规范化、Excel 导出和基础分析串成一条本机工作流。浏览器登录态、导出文件和分析数据库默认留在用户设备上。

## 主要功能

- 支持抖音、小红书、B站、快手四个平台。
- 复用用户本人登录的本地浏览器 Profile；Cookie 与 Profile 只保存在用户目录，项目不托管 Cookie。
- 在服务持续运行期间定期检查已授权平台的登录态，默认周期为 6 小时。
- 统一作品字段、日期、播放/阅读等指标并过滤无效零值记录。
- 导出 Excel，保留可追踪的平台来源字段。
- 可选飞书多维表格同步；凭据和目标表由用户自行配置。
- 本地运行历史、分析数据库和仪表盘。
- 纯 Python 后端、Vanilla HTML 仪表盘与可选 Tauri/React 桌面壳。

## 本地优先架构

```text
用户操作
  -> 本地页面 127.0.0.1
  -> Python 编排服务
  -> Playwright + 用户浏览器 Profile
  -> 四个平台创作者后台
  -> 本地 JSON / Excel / SQLite
  -> 用户主动选择后同步飞书
```

许可证、更新和反馈服务在社区版中默认未配置；不存在默认遥测。社区源码模式无需许可证，14 天免费试用流程已关闭。完整字段和触发条件见 [PRIVACY.md](PRIVACY.md) 与 [docs/DATA_FLOW.md](docs/DATA_FLOW.md)。

## 登录态与 Cookie 检查

平台 Cookie 随本地 Playwright/Chrome Profile 保存在 `~/Library/Application Support/数据科学家 Community/`，不写入源码目录，也不会上传给本项目。平台首次登录、扫码和后续重新授权都必须由账号本人在可见浏览器窗口中完成。

服务持续运行时，会对已启用且已授权的平台做保守的后台登录态检查，默认每 6 小时一次：

- 检查成功时显示最近检查时间并继续使用现有 Profile。
- HTTP 503、网络异常、空白页或页面状态不稳定时，检查结果记为 `unknown`，保留上一次 `authorized`，不会仅因一次探测失败要求重新授权。
- 只有稳定可见的登录页、采集脚本明确报告未登录/Cookie 失效，或本地 Profile 已不存在等明确证据，才会显示过期或提示重新授权。

这是状态检查，不是 Cookie 自动刷新；它不会延长平台会话，也不承诺登录态永不过期。平台仍可能因安全策略、频控、异地登录或账号风控要求用户重新验证。

## 安装与快速开始

当前发布候选是源码仓库，不提供已签名或已公证的 DMG。运行源码建议使用 macOS 11+、Python 3.11+（已实测 3.12）、Node.js `>=22.12.0 <23` 和 Git。普通源码运行不需要 Rust；只有构建或测试 Tauri 桌面壳时才需要 Rust stable 与 Apple 平台构建环境。

社区源码默认不连接商业更新服务；界面会显示“未配置”而不是网络故障。获取新版本请关注本仓库的 Releases，或在自行部署时通过 Git 拉取更新。商业安装包的签名自动更新链路不属于本仓库。

```bash
# 先确认 Python 标准库完整（本项目已实测 Python 3.12）
python3 -c "import ssl, sqlite3, xml.parsers.expat"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
npm ci
npx playwright install chromium
chmod +x start.sh scripts/*.sh
./start.sh
```

如果第一条检查或 `python3 -m venv` 出现 `dlopen`、`pyexpat` 或 `ensurepip` 错误，说明当前 Python 安装的标准库已损坏；先重装一个完整的 Python（推荐 3.12），并将上述命令中的 `python3` 替换为对应命令（例如 `python3.12`）。不要复用创建失败的 `.venv`。

服务默认监听 `127.0.0.1:8811`。首次采集需要在系统浏览器窗口中由用户本人完成扫码或登录。运行数据、Cookie/Profile、登录态检查元数据和 Playwright 浏览器默认写入 `~/Library/Application Support/数据科学家 Community/`，不会写入源码目录；可通过 `YIRENGONGIS_STATE_DIR` 显式覆盖。不要把状态目录、日志、截图或导出数据提交到 Git。

开发者可执行：

```bash
npm ci --prefix desktop
npm test
python scripts/build_community_staging.py
```

Rust/Tauri 构建还需要 Rust stable 工具链和 Apple 平台构建环境：

```bash
npm --prefix desktop run build:web
npm --prefix desktop run test:rust
npm --prefix desktop run build:app
```

## Excel 与飞书

Excel 由 `scripts/build_excel_export.py` 和各平台 normalizer 生成。飞书同步只有在用户开启同步并配置 App ID、App Secret 与多维表格后才发生；作品字段、表结构和已有记录会发送到飞书开放平台。CLI 模式会优先使用本地 `lark-cli`，否则通过固定版本 `@larksuite/cli@1.0.43` 自动下载；用户仍需在首次使用时完成飞书登录或授权。CLI 配置和临时请求文件均保存在上述用户状态目录。详情见 [PRIVACY.md](PRIVACY.md)。

## 项目结构

```text
desktop/       Tauri + React 桌面壳与测试
frontend/      本地仪表盘和启动页
scripts/       Python 编排、平台采集、规范化、导出、同步和测试
docs/          架构、数据流、版本边界和发布资料
```

## 社区版与商业版

| 能力 | Community | 商业产品 |
|---|---:|---:|
| 单机四平台采集、Excel、基础飞书同步 | 是 | 是 |
| 本地分析与仪表盘 | 是 | 是 |
| 源码修改与自部署 | 遵守 AGPL | 按合同 |
| MCN 多账号、团队权限、白标、OEM | 否 | 可选 |
| 托管服务、商业支持、SLA | 社区自助 | 可选 |
| Human State Intelligence 私有能力与研究数据 | 否 | 不属于本仓库 |

AGPL 允许商业使用；“公司使用”本身不自动产生付费义务。商业许可面向不希望承担 AGPL 义务的闭源使用，以及社区仓库之外的私有能力与服务。详见 [docs/COMMUNITY_VS_COMMERCIAL.md](docs/COMMUNITY_VS_COMMERCIAL.md)。

## 隐私、安全与平台合规

- 仅绑定本机回环地址；外部请求及字段均有文档清单。
- 社区版不包含客户数据、许可证台账、私钥、生产服务地址、历史 DMG、来源无法证明的原字体或原图标。为还原当前界面，仓库仅分发可追溯到 Google Fonts 官方仓库并随附 OFL 1.1 的 Noto Serif SC；平台 SVG 只用于数据来源识别，权利归对应平台，不表示合作或背书。
- 采集功能只应用于用户有权访问的数据，并应遵守平台条款、账号规则、隐私法律和内容权利。
- 不提供绕过验证码、访问控制、付费墙、频率限制或平台安全措施的功能承诺。
- 安全问题请按 [SECURITY.md](SECURITY.md) 处理；不要在公开 Issue 粘贴密钥、Cookie 或客户数据。

## 已知限制

- 平台页面和接口可能随时变化，采集适配不保证持续可用。
- 频繁访问可能触发平台频控、验证码、安全校验或账号风控；本项目不承诺规避这些机制，使用者应控制频率并遵守账号与平台规则。
- 真实账号扫码及四平台完整采集需要人工验收，自动化测试不等于平台授权通过。
- 当前优先验证 Apple Silicon；Intel、代码签名、公证和 DMG 不在 v0.1.0 社区候选范围。
- 飞书同步会把用户选择的数据发送到用户自己的飞书租户，不是纯离线功能。
- 项目方、品牌权利主张主体与 CLA 签约相对方已确认为杭州玄野科技有限公司；CLA 与商业合同仍须完成律师审阅和正式签署流程。

## Roadmap 与贡献

路线图见 [ROADMAP.md](ROADMAP.md)。提交代码前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；为了保留双重许可能力，外部贡献需要签署 CLA，仅签 DCO 不足够。

## License 与商业合作

社区代码采用 [GNU AGPL-3.0-only](LICENSE)。代码许可证不授予产品名称、Logo 或商标使用权，详见 [TRADEMARKS.md](TRADEMARKS.md)。闭源许可、白标、OEM、MCN、多账号、托管和支持需求，请在 GitHub Issues 或 Discussions 中仅提交不含敏感信息的合作意向。

本仓库文档是产品与合同草案，不构成法律意见。
