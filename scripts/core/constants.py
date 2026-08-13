"""共享常量 — 全局运行参数的唯一来源 (L1 基础层)。

收敛原先散落在 runner.py 顶部、各业务模块各自定义的共享常量。
业务模块从这里 import,禁止重复定义同名常量。
"""

# ── 锁与超时 ────────────────────────────────────────────────────────────
LOCK_STALE_SECONDS = 1800  # 30 分钟，超时判定锁过期（auth_all 用心跳续命）
LOG_MAX_BYTES = 2 * 1024 * 1024  # 2 MB，日志轮转阈值
LOG_KEEP_BYTES = 1 * 1024 * 1024  # 轮转后保留最后 1 MB
SCRIPT_TIMEOUT = 600  # 10 分钟，单平台采集子进程超时

# ── 飞书同步超时（CLI 模式比 App 模式长，CLI 走子进程较慢）───────────────
FEISHU_SYNC_TIMEOUT_APP = 180
FEISHU_SYNC_TIMEOUT_CLI = 900
FEISHU_TEST_TIMEOUT_APP = 120
FEISHU_TEST_TIMEOUT_CLI = 180

# ── 浏览器 ──────────────────────────────────────────────────────────────
VALID_BROWSER_CHANNELS = {"chrome", "msedge", "chromium"}
FORCED_BROWSER_CHANNEL = "chrome"

# ── 飞书业务表（变更检测必须覆盖每张业务表，否则会漏检重分组/图表变化）──
FEISHU_BUSINESS_TABLE_NAMES = ("平台明细V2", "作品总表V2", "作品图表表", "作品增量表")
FEISHU_BASELINE_RUNTIME_ONLY_FIELDS = {"日期", "最近更新时间"}
FEISHU_DETAIL_RUNTIME_ONLY_FIELDS = FEISHU_BASELINE_RUNTIME_ONLY_FIELDS
