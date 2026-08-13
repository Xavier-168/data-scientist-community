# Data Scientist Community

Data Scientist Community is a local-first macOS tool that helps creators collect, normalize, analyze, and export data from their own Douyin, Xiaohongshu, Bilibili, and Kuaishou creator dashboards. It can optionally sync user-selected records to Feishu Bitable.

This is an independent community project. It is not affiliated with, endorsed by, or officially partnered with any supported platform or Feishu.

## A note from the creator

Hi! I am Xiaoyao, the technology creator behind **Zhao Xiaoyao Xavier** on Douyin. This is the first repository I have published on GitHub. Since becoming a Vibe Coder, I have spent almost every day building tools and workflows that solve real problems in my own work.

Data Scientist Community is one of those projects, and I believe it can also help other creators who want to understand their content through data.

As a full-time OPC (one-person company) founder, I need to review my own content continuously: what worked, what did not, and what I should change next. I wanted to bring detailed data from each creator platform into my own Feishu workspace and analyze it through a unified dashboard.

The difficulty is that every platform uses different metrics, fields, and definitions. To make meaningful comparisons possible, I manually mapped and aligned the core fields from Douyin, Xiaohongshu, Bilibili, and Kuaishou, then normalized them into a shared structure for review and analysis.

The current release supports macOS first, with a Windows version planned. If this project helps you, please consider giving it a Star and sharing your real-world feedback.

You can learn more about me, my recorded courses, and my knowledge base on my one-person company website: [https://zhaoxiaoyao.com](https://zhaoxiaoyao.com). The open-source project is independent from the learning resources and commercial services on that website; any paid offering will be clearly identified there.

See you there!

## Highlights

- Four community adapters: Douyin, Xiaohongshu, Bilibili, and Kuaishou.
- Local browser profiles created from the user's own login; cookies and profiles stay in the user state directory and are not hosted by this project.
- Conservative background login-state checks for authorized platforms, scheduled every six hours by default while the service remains running.
- Normalized local JSON, Excel, SQLite history, and analytics dashboard.
- Optional user-initiated Feishu synchronization.
- Pure-Python backend, a vanilla local dashboard, and an optional Tauri/React shell.
- No default community-edition license, update, feedback, or telemetry endpoint. Community source mode does not require activation, and the 14-day free-trial flow is disabled.

## Login state and cookies

Platform cookies stay inside the local Playwright/Chrome profile under `~/Library/Application Support/数据科学家 Community/`. They are not written to the source checkout or uploaded to this project. Initial login, QR authorization, and any later reauthorization must be completed by the account owner in a visible browser window.

While the service remains running, enabled and authorized platforms receive a conservative background login-state check every six hours by default:

- A successful probe records the latest check time and continues using the existing profile.
- HTTP 503 responses, network errors, blank pages, and unstable page state are recorded as `unknown`; the previous `authorized` state is preserved and one transient probe does not trigger reauthorization.
- Reauthorization is shown only when there is explicit evidence, such as a stable visible login page, an explicit logged-out/expired-cookie result from the collector, or a missing local profile.

This checks state; it does not refresh cookies, extend a platform session, or promise that a login will never expire. Platform security policies, rate limits, unusual-login checks, or account risk controls may still require the user to verify the account again.

## Quick start

The v0.1.0 release candidate is source-only and does not include a signed or notarized DMG. Recommended prerequisites for running the source are macOS 11+, Python 3.11+ (3.12 tested), Node.js `>=22.12.0 <23`, and Git. Rust is not required for the normal source workflow; Rust stable and the Apple platform build environment are needed only to build or test the optional Tauri desktop shell.

Community source does not connect to the commercial update service. The UI reports automatic updates as not configured rather than as a network failure. Follow this repository's Releases or pull through Git for source updates; the signed commercial installer update channel is outside this repository.

```bash
# Verify that the Python standard library is complete (Python 3.12 is tested).
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

If the first check or `python3 -m venv` reports a `dlopen`, `pyexpat`, or `ensurepip` error, the selected Python installation has a broken standard-library linkage. Reinstall a complete Python runtime (3.12 recommended), replace `python3` above with that interpreter (for example, `python3.12`), and discard the failed `.venv` before retrying.

The local service listens on `127.0.0.1:8811` by default. The user must complete platform login or QR authorization in the visible browser window. Never commit `.auth/`, `downloads/`, logs, screenshots, exported data, or browser profiles.

Runtime state and Playwright browsers are stored under `~/Library/Application Support/数据科学家 Community/` by default and are never written into the source checkout. Set `YIRENGONGIS_STATE_DIR` only when an explicit custom state location is required.

For the complete developer test suite, install the optional desktop dependencies first:

```bash
npm ci --prefix desktop
npm test
python scripts/build_community_staging.py
```

Feishu CLI mode uses an installed `lark-cli` when available, otherwise it downloads the pinned `@larksuite/cli@1.0.43` package through `npx`. The user must still complete the first Feishu login or authorization. CLI configuration and temporary request files stay inside the user state directory.

## Data and network boundaries

Collection traffic goes directly from the user's local browser to the selected creator platform. Feishu traffic occurs only after the user configures and requests synchronization. See [PRIVACY.md](PRIVACY.md), [PLATFORM_COMPLIANCE.md](PLATFORM_COMPLIANCE.md), and [docs/DATA_FLOW.md](docs/DATA_FLOW.md).

## Community and commercial licensing

Community code is offered under GNU AGPL-3.0-only. The AGPL permits commercial use; organizational use does not automatically require payment. A separate commercial license may be offered to users who need closed-source terms or private products and services such as MCN multi-account workflows, teams, white-label/OEM delivery, hosting, support, or SLAs.

Human State Intelligence capabilities, private research data, customer records, activation-server code, release infrastructure, and signing keys are not part of this repository.

External contributions require a CLA so the maintainers can preserve dual-licensing options. See [CONTRIBUTING.md](CONTRIBUTING.md) and [CLA.md](CLA.md).

## Limitations

- Platform pages and interfaces can change without notice.
- Frequent access may trigger platform throttling, CAPTCHAs, security checks, or account risk controls. This project does not promise to bypass those controls; users must choose appropriate request frequency and follow platform and account rules.
- Real-account authorization and end-to-end collection require manual verification.
- Apple Silicon is the current priority; Intel, signing, notarization, and a community DMG are outside v0.1.0.
- Hangzhou Xuanye Technology Co., Ltd. has been confirmed as the project entity, asserted brand-rights holder, and CLA counterparty. The CLA and commercial contract templates still require legal review and a formal signing workflow.

Use GitHub Issues or Discussions for non-sensitive support and commercial inquiries. This repository and its contract templates are not legal advice.
