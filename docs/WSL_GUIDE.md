# WSL / Linux 使用指南

本文档面向在 Windows Subsystem for Linux（WSL2）或 Linux 桌面环境下运行本项目
（数据科学家 Community）的用户。macOS 用户请直接参考根目录 `README.md`。

## 环境要求

| 依赖 | 要求 | 说明 |
|---|---|---|
| OS | Linux（含 WSL2） | 已适配；WSL2 推荐启用 WSLg 以便授权弹窗 |
| Python | >= 3.11（实测 3.12） | 推荐用 uv 安装，用户级，无需 root |
| Node.js | >= 22.12 且 < 23 | 无系统 Node 22 时脚本自动使用 `~/.local/node22` |
| 磁盘 | 约 1 GB | Python 3.12 + Node 22 + Playwright Chromium |

## 一键安装

```bash
bash scripts/install_wsl_deps.sh
./start.sh
```

`install_wsl_deps.sh` 会依次完成：uv 安装 Python 3.12 → Node 22（下载到
`~/.local/node22` 或复用已有 Node）→ 创建 `.venv` 并安装 Python 依赖 →
`npm ci` → Playwright 系统依赖（优先 `sudo npx playwright install-deps chromium`，
无 sudo 时自动回退 `scripts/install_linux_browser_libs.sh` 用户级解包）→
安装 Chromium。

## 启动与访问

```bash
./start.sh
```

`start.sh` 在 Linux 下自动：

- 检测 WSLg 并注入 `DISPLAY=:0`（授权浏览器窗口将显示到 **Windows 桌面**）
- 无图形界面时自动加 `--no-open`
- 绑定 `0.0.0.0`（WSL2 NAT 下仅 Windows 宿主可达），并注入用户级 Chromium
  系统库 `LD_LIBRARY_PATH`

服务监听 `127.0.0.1:8811`（WSL 内）与 `0.0.0.0:8811`（Windows 侧）。Windows
浏览器访问：

```
http://<WSL_IP>:8811/monitor
```

`<WSL_IP>` 用 `hostname -I` 查询，重启 WSL 后可能变化。页面会由服务端注入
session token，无需手动带参数。停止服务：`fuser -k 8811/tcp`。

## 便捷启动脚本

`wsl_start.sh` 封装了日常启停与访问：

```bash
./wsl_start.sh          # 启动（已运行则跳过），打印访问地址，并自动在 Windows 侧打开浏览器
./wsl_start.sh status   # 查看运行状态
./wsl_start.sh stop     # 停止服务
./wsl_start.sh restart  # 重启
```

特性：

- 自动等待服务就绪后打印本地/Windows 侧访问地址与会话摘要
- 通过 WSL2 互操作（`explorer.exe`）在 Windows 默认浏览器打开仪表盘，无需手动复制地址
- 端口可用环境变量 `PORT` 覆盖（默认 8811）

## 工作台说明

侧边栏：主仪表盘 / 同步记录 / 系统设置 / 产品信箱。
首次启动向导：1. 平台选择 → 2. 账号授权 → 3. 基本设置 → 4. 飞书同步 → 5. 首次启动。

主仪表盘提供：

- 顶部指标：已启用平台数、已授权平台数、汇总数据行数、历史运行次数
- 平台卡片：各平台采集状态、授权状态、开始同步 / 去授权
- 一键操作：开始全面同步、仅同步飞书、修改同步日期、导出 Excel

## 授权与登录

- 首次授权需在 **Windows 桌面弹出的浏览器窗口**中扫码/登录，流程依赖 WSLg。
- 若点击「去授权」后无窗口弹出：确认 `.wslconfig` 未设置
  `guiApplications=false`，并在 PowerShell 执行 `wsl --shutdown` 后重进。
- 登录态保存在 `~/.local/share/data-scientist-community/profiles/`，授权后
  服务每 6 小时做一次保守的登录态检查；503/网络异常记为 `unknown`，不会因
  单次探测失败要求重新授权。
- 平台登录态可能因安全策略、异地登录、账号风控要求重新验证，属平台行为。

## 数据与状态目录

Linux 下遵循 XDG，运行数据默认写入：

```
~/.local/share/data-scientist-community/
  ├── .auth/          # 会话 token、授权标志（0600）
  ├── downloads/      # Excel、JSON、SQLite、日志、运行历史
  └── profiles/       # 平台 Cookie / 浏览器 Profile
```

可用 `YIRENGONGIS_STATE_DIR` 环境变量显式覆盖。这些目录**不要提交到 Git**。

## 注意事项

### 平台合规

- 采集仅适用于你有权访问的账号数据，须遵守各平台条款与账号规则。
- 控制采集频率；频繁访问可能触发平台频控、验证码或账号风控，项目不承诺规避。
- 平台页面和接口可能随时变化，采集适配不保证持续可用。

### 安全与隐私

- 服务绑定 `0.0.0.0` 仅在 WSL2 NAT 下将端口暴露给 Windows 宿主；数据接口仍
  要求 session token，跨站 Origin 请求会被拒绝。
- 数据默认不出本机；仅当你在仪表盘配置并触发飞书同步时，作品字段、表结构与
  已有记录会发送到**你自己的飞书租户**。
- 社区版不包含默认遥测；许可证与更新服务默认「未配置」。

### WSL 特有

- headless 采集（自动同步）不需要图形界面；只有授权需要 WSLg 弹窗。
- WSL IP 重启会变；换地址后用新 IP 访问 Windows 侧入口。
- 无 sudo 环境请运行 `scripts/install_linux_browser_libs.sh` 安装用户级
  Chromium 系统库，`start.sh` 会自动注入 `LD_LIBRARY_PATH`。

### 其他

- 真实账号扫码及四平台完整采集需人工验收，自动化测试不等于平台授权通过。
- 社区版无自动更新，新版本请关注 GitHub Releases 或 Git 拉取。
