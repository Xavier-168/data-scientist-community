# 架构

## 组件

```text
Tauri/React（可选桌面壳）
          |
Vanilla HTML 本地仪表盘
          |
Python HTTP 编排服务 127.0.0.1
   |         |          |
Playwright   Excel      飞书 OpenAPI/CLI
   |
四个平台创作者后台
```

- `scripts/start_monitor.py`：检查环境、创建本地 session、启动服务和页面。
- `scripts/_run.py`：纯 Python 薄入口。
- `scripts/runner.py`：HTTP 路由、任务编排、授权状态、历史和同步入口。
- `scripts/core/`、`domain/`、`orchestration/`：路径、进程、领域计算、租约和子进程监督。
- `scripts/*_export.mjs`：Playwright 平台适配。
- `scripts/normalize_*.py`、`platform_source_rows.py`：规范化与数据质量规则。
- `scripts/build_excel_export.py`：统一 Excel 输出。
- `scripts/prepare_feishu_bitable_sync_v2.py`、`sync_feishu_bitable_openapi.py`：飞书准备和同步。
- `frontend/`：本地页面，无外部 Web 框架。
- `desktop/`：Tauri 2 + React 壳、启动恢复和本地 runtime 管理。

## 运行态隔离

已封装桌面应用和直接源码运行都把 Cookie、授权状态、导出、日志、数据库及 Playwright 浏览器写入用户的 `~/Library/Application Support/数据科学家 Community/` 目录，有 `package_id` 时使用对应子目录。只有用户显式设置 `YIRENGONGIS_STATE_DIR` 时才使用其他状态目录；源码检出目录保持只读、无运行状态。`.gitignore` 和公开导出门禁继续阻止这些内容进入仓库。

## 进程与并发

- 全局采集/同步锁与单平台授权锁分开。
- 授权任务保留用户浏览器 session；批处理任务使用独立进程组。
- 单平台和一键任务都写运行历史；长时间顺序授权刷新锁心跳。
- 启动器使用 session token 与 origin 检查保护本地写端点。

## 社区差异

维护者侧公开导出器位于商业开发工作区，不随社区候选发布；它不改原仓库业务文件，而是在新目录中执行可验证覆盖：

- `COMMUNITY_EDITION` 标记启用社区本地访问；
- 许可证、更新和反馈默认端点置空；
- 字体改为系统 fallback；
- 原图标替换为已记录哈希的社区占位图标；
- 客户打包、台账、密钥运维和内部发布文件不进入 allowlist。

社区仓库中的 `PUBLIC_EXPORT_MANIFEST.json`、`.public-export.json` 与 `EXPORT_PROVENANCE.json` 用于核对公开结果。`source_commit` 仅是维护者工作树的基线提交；`source_snapshot` 明确记录输入是否相对该提交有变更，并用实际 `source_files`、`template_files` 和资产哈希计算内容指纹。这些记录不声称仅凭公开仓库即可重建私有输入树或重新执行完整导出流水线。

## 构建

- 社区 Python staging：`python scripts/build_community_staging.py`。
- Web/Tauri：`npm --prefix desktop run build:web` 与 `build:app`。
- 社区仓库不包含预构建 runtime、Chromium、node_modules、DMG 或签名材料。

## 信任边界

本地应用、浏览器平台、飞书、用户自建服务和包管理器是不同信任域。字段级数据流见 [DATA_FLOW.md](DATA_FLOW.md)，安全报告流程见 [../SECURITY.md](../SECURITY.md)。
