# 公开发布门禁

状态值仅使用 `PASS`、`FAIL`、`BLOCKED`、`MANUAL VERIFICATION REQUIRED`。

| 门禁 | 状态 | 证据/阻塞项 |
|---|---|---|
| 当前候选敏感信息 | PASS | 当前树扫描为 0 critical/high/medium；扫描器仅输出类型、位置和不可逆指纹 |
| 原 Git 历史 | PASS | 已判定不可公开；候选从白名单生成且不复制 `.git` |
| 候选 Git 历史 | PASS | 白名单导出物不复制原 `.git`；发布者必须在全新仓库使用 GitHub noreply 身份建立新历史，并在提交后重新扫描、回读 GitHub 默认分支 |
| 个人与客户信息 | PASS | `.auth`、`downloads`、Profile、日志、Excel/JSON/SQLite、截图和客户台账均不在导出 allowlist |
| 网络外发 | PASS | `PRIVACY.md` 已枚举；社区许可证/更新/反馈地址默认空 |
| 免费试用 | PASS | `TRIAL_ENABLED = False`；未激活时不会读取试用凭证或请求 `/trial/register` |
| Community 更新提示 | PASS | 默认不外发；未配置自动更新时显示“未配置”，源码更新指向 GitHub Releases / Git |
| 开源许可证 | PASS | GNU 官方 AGPL-3.0-only 原文，SHA-256 已核对 |
| 商业许可与 CLA | PASS | 项目方统一为杭州玄野科技有限公司；CLA 明确须经律师审阅并建立签署留痕流程后才接受外部贡献 |
| 第三方许可 | PASS | SPDX 2.3 SBOM 许可证未知项为 0，`pyspdxtools` 校验通过；notices 记录来源、版本、用途和再分发要求，Tabler `walk.svg` 已随附 MIT 原文 |
| 字体版权 | PASS | 原 11 个字体二进制全部排除，系统字体 fallback |
| 图标版权 | PASS | 原图标排除；应用图标只允许原创社区 PNG 和由其机械生成的 ICNS 固定哈希，进度卡仅新增已随附 MIT 许可证的 Tabler `walk.svg` |
| 商标 | PASS | 品牌权利主张主体为杭州玄野科技有限公司；不宣称已注册，并限制第三方平台标识和背书表述 |
| 商业/HSI 边界 | PASS | 商业发货、MCN/团队/白标/OEM/托管/SLA 与 HSI 私有内容均在 denylist |
| 纯 Python 与 Nuitka | PASS | 源码和 staging 均不含核心 `.so`、`.pyc`、Nuitka、DMG 或 runtime；由普通 `.py` 启动 |
| Python 测试 | PASS | 原工作区全量通过，安全依赖升级后复跑无失败；候选社区契约和可公开 Python 测试无失败 |
| 前端/Node 测试 | PASS | 前端契约、Vitest 38 项、Vite/TypeScript 构建通过；根目录与 `desktop/` 的 `npm audit` 均为 0 |
| Rust/Tauri 测试与构建 | PASS | Rust 191 项与 Tauri smoke 2 项通过；真实 arm64 `.app` release build 通过；`cargo-audit` 为 0 漏洞 |
| 候选重新安装 | PASS | 删除依赖后重新执行 Python 安装、根目录 `npm ci`、`desktop npm ci` 成功 |
| 状态目录与只读源码冷启动 | PASS | 默认状态解析到用户 Application Support；只读源码配合隔离状态目录真实启动成功，仅监听 `127.0.0.1`，源码树零运行写入 |
| Cookie/Profile 与登录态检查 | PASS | Cookie/Profile 位于用户状态目录；周期默认 6 小时；503、网络/探测异常保留 `authorized` 并记为 `unknown`；只有稳定登录页或明确未登录/Cookie 失效证据才转为过期/待授权，四平台单元与 Playwright 瞬态页面回归通过 |
| 纯 Python app staging | PASS | 白名单 staging 成功；二次扫描不含禁止二进制、缓存、runtime 或私有状态 |
| Excel/飞书 fixture | PASS | 四平台 fixture 生成 5 个工作表；零播放过滤、跨平台同标题聚合、增量和同步日志数量符合预期，公式错误 0 |
| 四平台静态入口 | PASS | 抖音、小红书、B站、快手脚本、runner 路由和社区测试映射均存在且通过 |
| 四平台真实账号授权与采集 | MANUAL VERIFICATION REQUIRED | 需要账号所有者扫码并人工复核日期、数量和字段 |
| 文档体系 | PASS | 中英文首页、合规、许可、架构、贡献和 GitHub 模板齐全 |
| 平台合规 | MANUAL VERIFICATION REQUIRED | 使用者仍需确认当前平台条款和账号授权；不存在“完全合规”声明 |
| GitHub 上架内容 | PASS | `docs/GITHUB_LISTING.md` 已提供可直接复制的 description、About 短句、topics、首发预发布标题与正文；v0.1.0-rc.1 release notes 已准备，无夸大宣传或虚假徽章 |

## 总结

当前状态：`MANUAL VERIFICATION REQUIRED`。硬性 `BLOCKED`/`FAIL` 为 0，法定主体占位符已清零；仓库所有者已作出公开源码发布决策，可发布源码仓库与 `v0.1.0-rc.1` 预发布。真实平台账号采集和平台条款仍须人工验收，因此不得把本状态表述为安装包、采集效果或平台合规已经全面通过，也不得在这些人工门禁完成前改为稳定版 `v0.1.0`。

仓库所有者已确认项目方为杭州玄野科技有限公司。剩余事项是账号所有者执行四平台真实授权/采集、依据使用地区与账号类型核查当期平台条款，以及对 CLA 和商业合同草案进行正式法律审阅；这些事项不能由仓库自动化测试代替。

## 候选验证命令

本公开仓库是维护者从商业开发工作区生成的白名单结果，不包含维护者侧的 `scripts/export_public_repo.py`；该脚本需要私有输入树，且已被 `PUBLIC_EXPORT_MANIFEST.json` 明确列入 denylist。公开使用者可通过 `.public-export.json`、`EXPORT_PROVENANCE.json`、扫描器和以下命令验证候选内容，但不能仅凭本仓库重新生成维护者的原始导出过程。

```bash
python3 -c "import ssl, sqlite3, xml.parsers.expat"
python3 scripts/public_audit_scan.py --root <candidate> --git-history --fail-on high
python3 -m venv .venv && .venv/bin/pip install -r requirements-test.txt
.venv/bin/pip install -r requirements-audit.txt
npm ci && npm --prefix desktop ci
npm test
npm audit --audit-level=high && npm --prefix desktop audit --audit-level=high
.venv/bin/pip-audit -r requirements.txt --no-deps --disable-pip
cargo install cargo-audit --version 0.22.2 --locked
cargo audit --file desktop/src-tauri/Cargo.lock --json
npm --prefix desktop run build:app
python3 scripts/build_community_staging.py --output <staging>
npm run sbom
.venv/bin/pyspdxtools -i SBOM.spdx.json
```

命令中的 `<candidate>` 和 `<staging>` 应使用各自机器上的临时绝对路径；不要把开发者路径写入仓库。`npm run sbom` 会直接调用 Cargo metadata 获取许可证，任一依赖为 `NOASSERTION` 都会硬失败，不会退化为仅读 `Cargo.lock` 的不完整结果。维护者发布新快照时仍须在私有开发工作区运行白名单导出器，再对生成结果执行本节全部验证。
