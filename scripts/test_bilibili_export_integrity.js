const assert = require("assert");
const path = require("path");
const { pathToFileURL } = require("url");

async function loadHelpers() {
  const sourcePath = path.resolve(__dirname, "bilibili_export.mjs");
  return import(pathToFileURL(sourcePath).href);
}

async function main() {
  const mod = await loadHelpers();
  assert.deepStrictEqual(
    mod.validateTargetCoverage(
      [
        { title: "作品A", publishText: "2026-07-01" },
        { title: "作品B", publishText: "2026-07-02" },
      ],
      [
        { 标题: "作品A", 发布日期: "2026-07-01" },
        { 标题: "作品B", 发布时间: "2026-07-02 10:00:00" },
      ]
    ),
    { ok: true, missing: [] }
  );
  assert.deepStrictEqual(
    mod.validateTargetCoverage(
      [
        { title: "作品A", publishText: "2026-07-01" },
        { title: "作品B", publishText: "2026-07-02" },
      ],
      [{ 标题: "作品A", 发布日期: "2026-07-01" }]
    ),
    { ok: false, missing: ["作品B|2026-07-02"] }
  );
  assert.strictEqual(
    mod.buildOfficialBatchError(1, new Error("选择数量异常")).message,
    "official_batch_failed:2:选择数量异常"
  );
  assert.strictEqual(
    mod.shouldStopBilibiliTargetScroll({
      found: 9,
      target: 12,
      scrollChanged: false,
      atBottom: true,
      reachedDateBoundary: false,
      stableRounds: 10,
    }),
    false,
    "未到接口目标日期边界时，弹窗暂时滚不动也不得早停"
  );
  assert.strictEqual(
    mod.shouldStopBilibiliTargetScroll({
      found: 12,
      target: 12,
    }),
    true
  );
  assert.strictEqual(
    mod.classifyBilibiliTargetDiscovery({ responseShapeOk: false }),
    "response_shape_changed"
  );
  assert.strictEqual(
    mod.classifyBilibiliTargetDiscovery({ rawWorks: 0 }),
    "api_empty"
  );
  assert.strictEqual(
    mod.classifyBilibiliTargetDiscovery({ rawWorks: 3, invalidWorks: 3 }),
    "unparseable_items"
  );
  assert.strictEqual(
    mod.classifyBilibiliTargetDiscovery({ rawWorks: 3, outsideDateWorks: 3 }),
    "outside_date_range"
  );
  assert.strictEqual(
    mod.classifyBilibiliTargetDiscovery({ rawWorks: 3, acceptedWorks: 2 }),
    "ok"
  );
  assert.deepStrictEqual(
    mod.buildBilibiliOfficialFallbackTargets(
      [
        { title: "范围外", publishText: "2025-12-31", publishTs: new Date(2025, 11, 31).getTime() },
        { title: "范围内A", publishText: "2026-01-02", publishTs: new Date(2026, 0, 2).getTime() },
        { title: "范围内B", publishText: "2026-01-03", publishTs: new Date(2026, 0, 3).getTime() },
      ],
      { minDate: "2026-01-01", videoLimit: 1 }
    ).map((item) => item.title),
    ["范围内B"]
  );
  assert.strictEqual(
    mod.shouldStopBilibiliOfficialFallbackScroll({
      scrollChanged: true,
      atBottom: false,
      reachedDateBoundary: false,
      stableRounds: 5,
    }),
    false
  );
  assert.strictEqual(
    mod.shouldStopBilibiliOfficialFallbackScroll({
      scrollChanged: true,
      atBottom: false,
      reachedDateBoundary: true,
      stableRounds: 2,
    }),
    true
  );
  assert.strictEqual(
    mod.shouldStopBilibiliOfficialFallbackScroll({
      scrollChanged: false,
      atBottom: true,
      reachedDateBoundary: false,
      stableRounds: 2,
    }),
    true
  );
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
