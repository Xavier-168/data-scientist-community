# 第三方软件与资产声明

本文件适用于 v0.1.0 社区源码候选。完整机器可读依赖清单见 `SBOM.spdx.json`。各组件继续适用自己的许可证，本项目的 AGPL 不改变第三方许可证。

## 1. 当前源码依赖

| 组件 | 锁定版本 | 许可证 | 来源 | 使用位置 | 再分发与随附要求 |
|---|---:|---|---|---|---|
| Playwright / playwright-core | 1.58.1 | Apache-2.0 | <https://github.com/microsoft/playwright> | 根 `package-lock.json`、四平台浏览器自动化 | 可再分发；二进制发行物保留 LICENSE，并随附上游 NOTICE |
| Tabler Icons | vendored `walk.svg` | MIT | <https://github.com/tabler/tabler-icons> | 采集进度卡行走人物图标 | 已随附 `frontend/assets/vendor/tabler/LICENSE`，保留版权与 MIT 文本 |
| React / React DOM | 19.2.7 | MIT | <https://github.com/facebook/react> | `desktop/` UI | 可再分发；保留版权与 MIT 文本 |
| Tauri API / CLI | 2.11.1 / 2.11.4 | MIT OR Apache-2.0 | <https://github.com/tauri-apps/tauri> | `desktop/` 壳与构建 | 可再分发；按所选许可证随附文本；Tauri Logo 不在本仓库 |
| Vite | 8.1.4 | MIT | <https://github.com/vitejs/vite> | 桌面前端构建 | 保留版权与 MIT 文本 |
| Vitest | 4.1.10 | MIT | <https://github.com/vitest-dev/vitest> | 桌面测试 | 保留版权与 MIT 文本 |
| TypeScript | 7.0.2 | Apache-2.0 | <https://github.com/microsoft/TypeScript> | 桌面类型检查 | 保留 LICENSE/NOTICE 要求 |
| Rust crates | `Cargo.lock` 锁定 | 逐包见 SBOM | crates.io 与各上游仓库 | Tauri 原生壳 | 二进制发行前按 SBOM 收集相应 LICENSE/NOTICE |

### Python 依赖

| 包 | 版本 | 许可证 | 来源 | 使用位置 |
|---|---:|---|---|---|
| certifi | 2026.6.17 | MPL-2.0 | <https://pypi.org/project/certifi/2026.6.17/> | TLS CA 证书定位 |
| cffi | 2.0.0 | MIT | <https://pypi.org/project/cffi/2.0.0/> | cryptography 依赖 |
| cryptography | 50.0.0 | Apache-2.0 OR BSD-3-Clause | <https://pypi.org/project/cryptography/50.0.0/> | Ed25519 和签名校验 |
| et_xmlfile | 2.0.0 | MIT | <https://pypi.org/project/et-xmlfile/2.0.0/> | Excel 依赖 |
| numpy | 2.4.6 | BSD-3-Clause | <https://pypi.org/project/numpy/2.4.6/> | pandas 数值依赖 |
| openpyxl | 3.1.5 | MIT | <https://pypi.org/project/openpyxl/3.1.5/> | Excel 读写 |
| pandas | 3.0.3 | BSD-3-Clause | <https://pypi.org/project/pandas/3.0.3/> | 规范化、合并、Excel 与分析 |
| pycparser | 3.0 | BSD-3-Clause | <https://pypi.org/project/pycparser/3.0/> | cffi 依赖 |
| python-dateutil | 2.9.0.post0 | Apache-2.0 OR BSD-3-Clause | <https://pypi.org/project/python-dateutil/2.9.0.post0/> | 日期解析 |
| six | 1.17.0 | MIT | <https://pypi.org/project/six/1.17.0/> | python-dateutil 依赖 |

上述 Python 包在源码仓库中只以版本清单出现，由用户安装；若在 DMG 中捆绑其代码或二进制，发行方必须从实际安装环境收集对应许可证、版权和 NOTICE。

## 2. 开发/运行时工具（当前源码仓库不捆绑）

| 工具 | 本次验证版本或范围 | 许可证 | 来源 | 当前是否分发 | 二进制发行要求 |
|---|---:|---|---|---:|---|
| Python | 3.11+；审计机 3.14.6 | PSF License Agreement + 历史条款 | <https://docs.python.org/3/license.html> | 否 | 捆绑时随附该发行版完整 LICENSE |
| Node.js | 22.12.x；审计机 22.14.0 | Node.js MIT + bundled third-party notices | <https://github.com/nodejs/node/blob/main/LICENSE> | 否 | 捆绑时随附对应版本完整 Node LICENSE |
| Chromium / Chrome for Testing | 由 Playwright 安装的对应版本 | BSD-3-Clause + 大量第三方许可证 | <https://chromium.googlesource.com/chromium/src/+/main/LICENSE> | 否 | 不能只附 Chromium 主许可证；必须随附实际构建的完整 credits/licenses |
| Rust stable | `rust-toolchain.toml` | Apache-2.0 OR MIT + 组件条款 | <https://github.com/rust-lang/rust> | 否 | 捆绑工具链时按发行版随附许可证 |

## 3. 字体审计与排除决定

上游官方仓库显示下列字体使用 SIL Open Font License 1.1，可在满足版权声明、许可证和 Reserved Font Name 条件时再分发：

- Noto Serif SC：<https://github.com/google/fonts/tree/main/ofl/notoserifsc>
- Source Han Sans：<https://github.com/adobe-fonts/source-han-sans/blob/master/LICENSE.txt>
- LXGW WenKai：<https://github.com/lxgw/LxgwWenKai/blob/main/OFL.txt>
- Sarasa Gothic：<https://github.com/be5invis/Sarasa-Gothic/blob/main/LICENSE>
- Geist：<https://github.com/vercel/geist-font/blob/main/OFL.txt>

本轮为匹配 Figma 28:3 画板，从 Google Fonts 官方仓库直接取得 `NotoSerifSC[wght].ttf`，以 `frontend/assets/fonts/NotoSerifSC-Variable.ttf` 随源码分发，并随附原始 `OFL.txt`。该文件 SHA-256 为 `050080d9255a86808f2945bffac582b31ef32bc36411ce29563b4961670c66f9`，许可证文件 SHA-256 为 `5e0da210fb04058a8c0087985d2d456b931c2579811a49655721d3cf0c36b6d6`。

其余原工作区本地字体二进制没有下载记录或随附许可证，不能证明与某个上游 release 一一对应，因此仍从社区候选排除，基础主题继续使用系统字体 fallback。下表仅保留这些被排除文件的审计证据，不表示它们已获准发布。

| 原本地文件 | 内嵌家族/版本 | SHA-256 | 候选处理 |
|---|---|---|---|
| `GeistMono-Bold.otf` | Geist Mono 1.700 | `798717121053eb28db4b7bded89c9ad990d45f1bebe4f56381db2fde9bc8c8ec` | 排除 |
| `GeistMono-Medium.otf` | Geist Mono 1.700 | `a3aff5d30e9bb905b3d950bc85e7260307750aa32ac4e0d259991d481431c577` | 排除 |
| `GeistMono-Regular.otf` | Geist Mono 1.700 | `71daf60b3107cab27da2c99fdb7b19b1a04b2df4ab8aac3af8b730b581ebbc61` | 排除 |
| `LXGWWenKai-Medium.ttf` | LXGW WenKai 1.521 | `1b74667333612eb05b7a1a70a9b324458251cd81e9a1d6c1bfd22da871734b9c` | 排除 |
| `LXGWWenKai-Regular.ttf` | LXGW WenKai 1.521 | `b64b7add297672bf04c54ce229678ddf09b4f9671cb1ece1f24c868f4226edd0` | 排除 |
| `LXGWWenKaiMono-Regular.ttf` | LXGW WenKai Mono 1.521 | `cd957f870149d1fa7c1080bd079418fe9d5d60899316da1e12d7302e769ed3be` | 排除 |
| `SarasaFixedSC-Regular.ttf` | Sarasa Fixed SC 34.000 | `0b5ff3c109cf6940c2e83eda0174d2867ae70220e2359bf9a59817e6913e0ea5` | 排除 |
| `SarasaFixedSC-SemiBold.ttf` | Sarasa Fixed SC 34.000 | `93985132f8195eeea30ba205c663811ca91f5381a0f72a886cc9fc80ccef36f7` | 排除 |
| `SourceHanSansCN-Bold.otf` | Source Han Sans CN 2.005 | `62383707c086a32f3afd5e293f34c7eff64c7fea31f579fdc6cbe34d920519a6` | 排除 |
| `SourceHanSansCN-Medium.otf` | Source Han Sans CN 2.005 | `a94e558a2fe972bee4f46bce0843abff37063fd68c33f1e7d9058f6f09432b01` | 排除 |
| `SourceHanSansCN-Regular.otf` | Source Han Sans CN 2.005 | `e2bc8a2e7f37474b774fff8db758681ece40bb6947a90d571bce9dd60671a8e4` | 排除 |

## 4. 图标

原工作区中没有可验证权属记录的应用图标未进入候选仓库。采集进度卡新增有明确 MIT 授权且已随附许可证的 Tabler `walk.svg`。应用图标只包含本次为社区候选生成的无文字、无平台标识原创占位图标：PNG SHA-256 为 `502089826929f27186035466480d0d00ad5552b6274765778b51168935b7690e`，由同一 PNG 机械生成的 macOS ICNS SHA-256 为 `37a2896d1dff2735f5388b63f5273a24879169d0a9387c732c4cf34921d51ff4`。产品信箱 `frontend/assets/figma/mailbox.svg` 是本项目 UI 插图，随仓库许可提供。

`frontend/assets/platforms/douyin.svg`、`bilibili.svg`、`kuaishou.svg` 是用于表格中识别数据来源的单色平台标识。相关名称、图形和商标权利归各自权利人；仓库中的兼容性说明和来源标签不构成官方合作、授权、认证或背书，也不授予超出合理来源识别范围的商标使用权。衍生发行版应结合适用法和平台品牌规范自行评估并替换这些标识。

## 5. AGPL 兼容性结论

当前源码直接依赖使用 MIT、BSD、Apache-2.0、MPL-2.0、PSF 和类似宽松/文件级许可证；这些许可证没有发现阻止本项目源码采用 AGPL-3.0-only 的条款。该结论只覆盖本次锁文件和源码候选，不替代律师意见，也不自动放行未来 DMG 中新增的 runtime、浏览器、字体或系统组件。

任何依赖变更都必须重新生成 SBOM、检查 `NOASSERTION` 项并更新本文件。存在未知许可证或无法取得许可证文本时，二进制发布门禁应为 `BLOCKED`。
