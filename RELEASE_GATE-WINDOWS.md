# Windows 发布门禁（RELEASE_GATE-WINDOWS）

Windows 原生客户端（NSIS 安装包）发版前的核对清单。与 macOS 版
[RELEASE_GATE.md](RELEASE_GATE.md) 平行，社区版默认不连接许可证/更新/反馈服务。

## 自动化（CI `test-windows` 必须全绿）

| # | 项目 | 状态 |
|---|---|---|
| 1 | `npm run test:public` 公开仓库扫描 | REQUIRED |
| 2 | `npm run test:frontend` 前端契约 | REQUIRED |
| 3 | `npm run test:python`（含 test_windows_native） | REQUIRED |
| 4 | `npm run test:desktop:web` / `test:desktop:build` | REQUIRED |
| 5 | cargo `--lib` 单测（跳过 Unix 进程所有权用例） | REQUIRED |
| 6 | `npm run test:desktop:smoke` 启动页 UI | REQUIRED |
| 7 | `windows-release.yml` 产出 NSIS 安装器 | REQUIRED |

## 构建链核对

| # | 项目 | 状态 |
|---|---|---|
| 8 | `build_windows_runtime.py` 产物 sha256 锁定（python.org 锁死 / Node 走官方 SHASUMS256.txt） | REQUIRED |
| 9 | `sign_package_manifest.py` 签名后 Python 侧回验通过 | REQUIRED |
| 10 | 清单 `arch=x86_64`、`platform=win`、package_id `data-scientist-community-win-x64` | REQUIRED |
| 11 | 安装包体积记录（首版预期 ~350-400MB 压缩 / ~700MB 安装后） | REQUIRED |
| 12 | SBOM 重新生成并纳入 Python/Node/Chromium 组件许可 | REQUIRED |

## 手动验收（MANUAL VERIFICATION REQUIRED）

| # | 项目 | 状态 |
|---|---|---|
| 13 | 免管理员安装/卸载/重装/覆盖升级（NSIS currentUser 模式） | MANUAL |
| 14 | SmartScreen/杀毒软件未签名告警记录在案（正式版需代码签名证书） | MANUAL |
| 15 | 首启链路：清单校验 → 运行时解包 → runner 拉起 → 仪表盘窗口 | MANUAL |
| 16 | 四平台真实账号扫码登录与采集（抖音/小红书/B站/快手） | MANUAL |
| 17 | Excel 导出落 `%APPDATA%\数据科学家 Community\downloads`，安装目录无状态写入 | MANUAL |
| 18 | 飞书多维表格同步全流程（需用户自配 App ID/Secret） | MANUAL |
| 19 | 崩溃恢复：杀 runner 壳自动拉起；杀壳 Job Object 连带终止 runner | MANUAL |
| 20 | 源码运行模式（start.bat）与本安装包行为一致性抽检 | MANUAL |

## 已知边界

- 未数字签名：SmartScreen 会提示"未知发布者"，属预期；证书采购后补充 `windows-signing` 配置。
- Windows 模式位（0o600/0o700）降级为默认 ACL 继承；敏感文件不做 POSIX 权限隔离。
- 树哈希的 mode 契约：目录 0o755；`.exe/.dll/.bat/.cmd/.ps1` 0o755；其余文件 0o644
  （见 `scripts/build_windows_runtime.py` 与 `runtime/archive.rs` Windows 分支注释）。
