# 贡献指南

感谢参与。贡献必须同时满足可复现、最小权限、隐私安全和平台合规。

## 提交前

1. 先在 Issue 或 Discussion 描述问题、证据和建议边界；安全问题按 [SECURITY.md](SECURITY.md) 私下报告。
2. 不要提交 `.auth/`、Cookie、浏览器 Profile、真实导出、客户数据、日志、截图、密钥或生产服务地址。
3. 新的第三方代码、字体、图片或数据必须列出来源、版本、许可证和再分发条件。
4. 外部贡献者必须与杭州玄野科技有限公司完成 [CLA.md](CLA.md)。在正式签署流程和留痕机制启用前，维护者只能讨论或审阅，不能合并外部贡献。

## 开发环境

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm ci
npm ci --prefix desktop
```

不要把真实平台账号作为自动测试前提。测试使用合成 fixture；需要真实扫码的步骤标记为人工验收。

## 变更要求

- 保留四平台范围：抖音、小红书、B站、快手。
- 先复现再修复；PR 写明症状证据、根因、修改和验证。
- 不绕过验证码、访问控制、频率限制或平台安全措施。
- 新增网络请求必须更新 `PRIVACY.md` 和 `docs/DATA_FLOW.md`。
- 新依赖必须更新锁文件、`THIRD_PARTY_NOTICES.md` 和 `SBOM.spdx.json`。
- 不把 MCN 多账号、团队、白标、OEM、私有服务端或 HSI 能力加入社区仓库。

## 验证

```bash
npm test
python scripts/build_community_staging.py
python scripts/public_audit_scan.py --root . --fail-on high
```

如果只修改一个模块，可先运行最小相关测试，但 PR 合并前需要完整门禁。真实账号端到端采集由维护者在隔离账号中人工验收。

## Pull Request

PR 必须包含：

- 目标和不在范围内的事项；
- 测试命令及原始结果摘要；
- 数据、网络、许可证和平台风险；
- CLA 状态；
- 需要人工验收的步骤。

提交信息建议采用 `类型: 中文简述`，例如 `修复: 防止授权状态竞态覆盖`。

## CLA 流程

1. 在 PR 中勾选 CLA 声明。
2. 维护者通过不公开个人信息的签署渠道提供最终 CLA。
3. 签署完成后，维护者仅在 PR 记录 `CLA: verified`，不公开地址或签名文件。

DCO 可作为提交来源声明的补充，但不能替代 CLA。
