# 公开发布审计报告

审计日期：2026-08-10  
对象：商业开发工作区当前快照、全部 Git 对象、社区白名单导出结果  
结论：**公开源码候选已制作，自动化硬性门禁通过且法定项目方已确认；真实平台账号采集和平台条款仍需人工验收，最终发布决定以 `RELEASE_GATE.md` 和独立复核为准。**

## 方法与局限

- 读取项目规则、README、既有开源审计、工作树状态、源码调用方、锁文件和构建脚本。
- 本机没有 `gitleaks`、`trufflehog`、`detect-secrets`、`syft` 或 `semgrep`；秘密与隐私扫描采用项目内可复现 fallback。依赖漏洞另用 `npm audit`、`pip-audit` 和 `cargo-audit` 检查。
- 使用 `scripts/public_audit_scan.py` 对当前源码和 2133 个 Git 对象执行正则、熵、文件类型、私有域名和作者元数据扫描；输出只含规则、文件、行号和不可逆指纹。
- 启发式扫描存在测试 fixture/锁文件误报，已配合语义审查；“未命中”不等于绝对不存在秘密。
- 第三方许可以锁文件、已安装包元数据和上游官方许可页核实；本地字体因缺少可追溯下载证据而不放行。
- 真实账号扫码、四平台端到端采集和平台条款适用性不能由合成测试证明，必须人工验收。

## 发现（按严重程度）

### 严重

1. **原 Git 历史不可公开。** 216 条提交作者记录使用非 GitHub noreply 邮箱；历史 blob 还包含内部路径、内部文档、旧二进制和已删除文件。任何原地切换 Public 都会暴露历史内容。
2. **法定项目方曾缺失，现已整改。** 用户已确认杭州玄野科技有限公司为项目方，NOTICE、商业许可、CLA、商标与隐私联系文件已统一更新。该确认不替代对具体代码、品牌和资产权属的法律尽调。

### 高

1. **原工作区存在真实运行态。** `.auth/`、`downloads/`、错误截图、日志、Excel、JSON、SQLite、浏览器 Profile 和历史 `dist/` 必须排除，不读取其敏感正文进入报告。
2. **生产基础设施出现在跟踪源码。** 原 `package_manifest.json:13-19`、`scripts/client_license.py:61-62`、`scripts/update_manager.py:36` 及内部文档包含商业服务地址。地址本身不一定是秘密，但暴露运营拓扑且会让社区版默认外发。
3. **原图标权属无证据。** `branding/app-icon-source.png` 与 Tauri 图标集只有文件哈希，没有作者、授权或来源记录。
4. **字体不能建立二进制来源链。** 11 个 OTF/TTF 可识别家族和嵌入版本，但没有随附许可证或下载记录，无法证明对应上游 release。
5. **根许可证不合格。** 原 `package.json` 的 ISC 字段只描述 npm package 元数据，不能代表整个项目，也不满足要求的 AGPL-3.0-only 双重许可结构。

### 中

1. 原仓库包含客户包生成器、发货台账、密钥生成工具、runtime/DMG 发布管线和内部 Agent 计划，虽然脚本不等于私钥，但超出社区产品边界。
2. 原启发式扫描聚合结果为 0 critical、621 high、233 medium；其中大量 Feishu/secret/熵命中属于测试 fixture 或公开校验材料，但历史作者邮箱、绝对路径和商业域名已被人工确认。
3. Node、Python、Chromium、Playwright、Rust/Tauri 和字体的二进制再分发义务不能由源码锁文件代替；未来 DMG 必须对实际打包内容重新生成 notices 和 SBOM。

## 实际整改

- 新增 `PUBLIC_EXPORT_MANIFEST.json`：精确文件 allowlist、denylist、唯一允许二进制及哈希。`EXPORT_PROVENANCE.json` 把 `source_commit` 标记为基线提交，并单独记录工作树输入是否有变更、每个实际输入哈希及聚合内容指纹，不再把脏工作树声称为某一提交的可重建快照。
- 商业开发工作区新增维护者侧 `scripts/export_public_repo.py`：只复制白名单，不复制 `.git`；生产地址置空；社区访问、本机字体 fallback、Tauri 身份和原创图标以断言式变换生成。该脚本依赖私有输入树，按 manifest denylist 不进入公开候选；候选只公开输出清单、provenance 与验证工具。
- 社区 `package_id` 变换会同步替换可公开的 Ed25519 测试签名 fixture；完整 Rust 测试曾准确检出旧签名不再匹配，修复后定向测试及全量测试均通过。
- Tauri smoke 曾检出社区标题只替换了页面正文、未同步 HTML title 与 smoke 断言；导出器现将三处作为同一身份变换并用双次真实浏览器 smoke 锁定。
- 新增脱敏扫描、纯 Python staging、公开门禁测试和 SPDX SBOM 生成脚本。
- 加入 GNU 官方未经改写的 AGPL-3.0-only 全文，SHA-256 为 `0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0`。
- 原字体全部排除；使用系统字体 fallback。原图标全部排除；候选只含本次生成的原创社区占位 PNG，以及由它机械生成的多尺寸 ICNS，二者都由 allowlist 固定哈希。
- 补齐中英文 README、商业许可草案、CLA、贡献、安全、隐私、平台、商标、第三方、架构、数据流、GitHub 上架与 Issue/PR 模板。
- 候选不包含激活服务端、客户包生成器、发货台账、私钥运维、客户数据、历史产物、内部 Agent 文档、MCN/团队/白标/OEM/托管/SLA 或 HSI 私有内容。

## 网络行为

字段级目的地、触发条件、默认值、用户知情与社区处理方案见 `PRIVACY.md`。社区版默认未配置商业许可证、更新和反馈服务；平台采集与飞书同步仍是明确的外部数据流。

## 许可兼容性

社区代码采用 AGPL-3.0-only；另提供未自动生效的商业许可说明和合同模板。AGPL 允许商业使用。当前锁定直接依赖使用 MIT、BSD、Apache-2.0、MPL-2.0、PSF、OFL 等许可证，未发现与 AGPL 源码发布直接冲突的条款。完整清单和未知项以 `THIRD_PARTY_NOTICES.md`、`SBOM.spdx.json` 和最终门禁为准。

## 验证结果

- 原工作区完整测试链通过：前端契约脚本、Python、Vitest、Vite/TypeScript、Rust/Tauri 测试及 Tauri smoke 均无失败；依赖升级后又单独重跑 Python 测试。
- 190 文件候选从空依赖状态执行 Python 3.12.13 `pip install`、两处 `npm ci` 和完整测试；公开合同 14 项、Python 218 项（跳过 6）、Vitest 38 项、Rust 191 项通过，Tauri smoke 2/2 通过。
- `npm audit` 两处均为 0；`pip-audit` 为 0。`cargo-audit` 为 0 个漏洞，另报告 16 个 unmaintained 和 1 个 unsound 信息性 advisory：GTK3/glib 项仅存在于 Linux 条件依赖，macOS 目标未进入依赖图；其余为 Tauri 间接依赖，已作为升级债务记录，不冒险改写业务依赖树。
- SPDX 2.3 SBOM 经 `pyspdxtools` 校验，包含 680 个 package，许可证未知项为 0。
- 源码默认状态目录解析回归通过；另将全新 190 文件候选设为只读，在显式隔离状态目录下真实启动成功并返回 `/progress`，只监听 `127.0.0.1`。源码树未生成 `.auth`、`downloads`、`.playwright-browsers`、`.write_test` 或飞书临时 JSON。
- 纯 Python staging 成功，未含 `.so`、`.pyc`、Nuitka、DMG、runtime、字体或私有状态；最终候选当前树扫描为 0 项。导出物不含 `.git`，发布者建立全新提交后仍须再做历史扫描。
- Excel/飞书准备 fixture 覆盖四个平台、零播放过滤和同标题聚合：5 个工作表、4 条明细、2 条作品聚合、2 条增量、1 条同步日志，公式错误为 0。静态渲染发现长标题/日期列可能被默认列宽截断，记为 P2 可用性债务，不影响数据值或本次业务逻辑。
- 四个平台静态入口、脚本映射和测试均通过；真实账号扫码、平台端到端采集及当前条款适用性仍必须由账号所有者人工验收。

完整命令、数量和状态见 `RELEASE_GATE.md`。法定主体占位符阻塞已解除；以上结果不替代真实平台验收、平台条款核查或律师意见，也不构成“完全合规”保证。
