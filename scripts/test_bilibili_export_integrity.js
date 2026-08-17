const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");

async function loadHelpers() {
  const sourcePath = path.resolve(__dirname, "bilibili_export.mjs");
  return import(pathToFileURL(sourcePath).href);
}

async function main() {
  const mod = await loadHelpers();
  const source = fs.readFileSync(path.resolve(__dirname, "bilibili_export.mjs"), "utf8");

  const officialPlan = mod.buildBilibiliOfficialExportPlan(
    Array.from({ length: 21 }, (_, index) => ({ targetId: `work-${index + 1}` }))
  );
  assert.strictEqual(officialPlan.authority, "official_csv");
  assert.strictEqual(officialPlan.totalWorks, 21);
  assert.strictEqual(officialPlan.expectedFiles, 3);
  assert.deepStrictEqual(
    officialPlan.batches.map((batch) => batch.length),
    [10, 9, 2],
    "21 条 B 站稿件必须生成 3 个无单条尾批的官方导出批次"
  );
  assert.strictEqual(officialPlan.validForOfficialComparison, true);
  const flattenedTargetIds = officialPlan.batches.flat().map((item) => item.targetId);
  assert.deepStrictEqual(
    flattenedTargetIds,
    Array.from({ length: 21 }, (_, index) => `work-${index + 1}`),
    "调整尾批时必须保持原始顺序且不得遗漏稿件"
  );
  assert.strictEqual(new Set(flattenedTargetIds).size, 21, "官方批次不得重复选择稿件");
  assert.deepStrictEqual(
    mod.buildBilibiliOfficialExportBatches(
      Array.from({ length: 11 }, (_, index) => ({ targetId: `work-${index + 1}` })),
      50
    ).map((batch) => batch.length),
    [9, 2],
    "即使外部传入更大的批次，也必须满足官方每批 2 至 10 条约束"
  );
  assert.deepStrictEqual(
    mod.buildBilibiliOfficialExportBatches(
      Array.from({ length: 31 }, (_, index) => ({ targetId: `work-${index + 1}` }))
    ).map((batch) => batch.length),
    [10, 10, 9, 2]
  );
  assert.strictEqual(
    mod.buildBilibiliOfficialExportPlan([{ targetId: "only-work" }]).validForOfficialComparison,
    false,
    "单条目标必须明确判定为不满足官方对比导出条件"
  );
  assert.strictEqual(
    mod.buildBilibiliOfficialExportPlan([]).validForOfficialComparison,
    false,
    "空目标不得因 every([]) 误判为可执行官方对比"
  );
  const progressSequence = [
    mod.buildBilibiliOfficialProgressSnapshot({ totalWorks: 21, completedWorks: 0, currentIndex: 21 }),
    mod.buildBilibiliOfficialProgressSnapshot({ totalWorks: 21, completedWorks: 0, currentIndex: 21 }),
    mod.buildBilibiliOfficialProgressSnapshot({ totalWorks: 21, completedWorks: 0, currentIndex: 10 }),
    mod.buildBilibiliOfficialProgressSnapshot({ totalWorks: 21, completedWorks: 10, currentIndex: 10 }),
    mod.buildBilibiliOfficialProgressSnapshot({ totalWorks: 21, completedWorks: 10, currentIndex: 19 }),
    mod.buildBilibiliOfficialProgressSnapshot({ totalWorks: 21, completedWorks: 19, currentIndex: 19 }),
    mod.buildBilibiliOfficialProgressSnapshot({ totalWorks: 21, completedWorks: 19, currentIndex: 21 }),
    mod.buildBilibiliOfficialProgressSnapshot({ totalWorks: 21, completedWorks: 21, currentIndex: 21 }),
  ];
  assert.deepStrictEqual(
    progressSequence.map((item) => item.processedWorks),
    [0, 0, 0, 10, 10, 19, 19, 21],
    "B 站进度只能在官方文件下载成功后递增"
  );
  assert.deepStrictEqual(
    progressSequence.map((item) => item.successWorks),
    [0, 0, 0, 10, 10, 19, 19, 21]
  );
  assert.deepStrictEqual(
    progressSequence[6],
    { totalWorks: 21, processedWorks: 19, successWorks: 19, queuedWorks: 2, currentIndex: 21 },
    "第三批下载失败时必须保持 19/21，不得把已定位的最后 2 条算作成功"
  );
  for (let index = 1; index < progressSequence.length; index += 1) {
    assert.ok(progressSequence[index].processedWorks >= progressSequence[index - 1].processedWorks);
    assert.ok(progressSequence[index].successWorks >= progressSequence[index - 1].successWorks);
    assert.ok(progressSequence[index].processedWorks <= progressSequence[index].totalWorks);
    assert.ok(progressSequence[index].successWorks <= progressSequence[index].totalWorks);
  }
  const delayedDownload = { suggestedFilename: () => "delayed.csv" };
  const downloadOrder = [];
  const waitedWithoutMenu = await mod.waitForBilibiliOfficialDownload({
    startDownloadWait: () => {
      downloadOrder.push("listener");
      return new Promise((resolve) => setTimeout(() => resolve(delayedDownload), 20));
    },
    clickExport: async () => {
      downloadOrder.push("click");
    },
    clickOptionalMenu: async () => false,
    menuProbeDelayMs: 1,
  });
  assert.strictEqual(
    waitedWithoutMenu,
    delayedDownload,
    "二次菜单未出现时仍要继续等待直接下载，不得提前报错"
  );
  assert.deepStrictEqual(downloadOrder, ["listener", "click"], "下载监听必须先于导出点击建立");
  let lateMenuAttempts = 0;
  let resolveMenuDownload;
  const menuDownload = { suggestedFilename: () => "menu.csv" };
  const waitedForLateMenu = await mod.waitForBilibiliOfficialDownload({
    startDownloadWait: () => new Promise((resolve) => {
      resolveMenuDownload = resolve;
    }),
    clickExport: async () => {},
    clickOptionalMenu: async () => {
      lateMenuAttempts += 1;
      if (lateMenuAttempts < 3) return false;
      resolveMenuDownload(menuDownload);
      return true;
    },
    menuProbeDelayMs: 1,
    menuProbeIntervalMs: 1,
    menuProbeAttempts: 4,
  });
  assert.strictEqual(waitedForLateMenu, menuDownload, "延迟出现的二次确认菜单必须被有限轮询捕获");
  assert.strictEqual(lateMenuAttempts, 3);
  for (const forbiddenEarlySuccess of [
    "processedWorks: worksByKey.size",
    "processedWorks: worksByTargetId.size",
    "processedWorks: eligibleWorks.length",
    "successWorks: confirmed.length",
    "processedWorks: batchIndex * MAX_WORKS_PER_OFFICIAL_EXPORT",
  ]) {
    assert.ok(
      !source.includes(forbiddenEarlySuccess),
      `发现、定位或选中阶段不得提前写采集成功：${forbiddenEarlySuccess}`
    );
  }
  assert.ok(
    source.indexOf("const officialPath = await downloadOfficialExport")
      < source.indexOf("completedWorks += batch.length"),
    "completedWorks 必须在官方文件下载完成后递增"
  );
  assert.ok(
    !source.includes("|| candidates[candidates.length - 1]"),
    "稿件选择确认按钮未激活时不得点击普通确认按钮继续假执行"
  );
  const confirmFunctionSource = source.slice(
    source.indexOf("async function confirmWorkSelection"),
    source.indexOf("async function collectWorksByTargetSet")
  );
  assert.ok(
    confirmFunctionSource.includes("workDialog.locator('.arcp-queue-confirm.active')")
      && confirmFunctionSource.includes("workDialog.waitFor({ state: 'hidden'")
      && !confirmFunctionSource.includes("el.tagName === 'BUTTON'"),
    "必须只点击当前可见稿件弹窗内的 active 确认，并验证该弹窗真正关闭"
  );
  const menuFunctionSource = source.slice(
    source.indexOf("async function clickVisibleExportMenuItem"),
    source.indexOf("export async function waitForBilibiliOfficialDownload")
  );
  assert.ok(
    menuFunctionSource.includes("popup.querySelectorAll")
      && menuFunctionSource.includes("'[role=\"menu\"]'")
      && !menuFunctionSource.includes("document.querySelectorAll('button, [role=\"button\"], a, li, div, span')"),
    "二次菜单轮询只能在可见浮层内查找，不得重复点击页面主导出按钮"
  );
  const downloadFunctionSource = source.slice(
    source.indexOf("async function downloadOfficialExport"),
    source.indexOf("async function normalizeOfficialExport")
  );
  assert.ok(
    downloadFunctionSource.includes("timeout: 60_000")
      && !downloadFunctionSource.includes("timeout: 15_000"),
    "B 站官方直下文件必须使用完整 60 秒监听，不得在 15 秒时提前失败"
  );
  assert.ok(
    !source.includes("await tryXhrCollection")
      && !source.includes("if (xhrResult.ok && xhrCoveredAll)"),
    "XHR 不得成为最终数据源或跳过官方 CSV"
  );
  assert.ok(
    source.includes("buildBilibiliOfficialExportPlan(works)")
      && source.includes("await normalizeOfficialExport(officialPaths)")
      && source.includes("await assertNormalizedMetricCoverage()")
      && source.includes("validateTargetCoverage(works, normalizedRows)"),
    "官方 CSV 必须经过 normalizer、字段校验和目标覆盖校验"
  );
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
  const partialSecondPageShapeChange = mod.finalizeBilibiliTargetDiscovery(
    Array.from({ length: 30 }, (_, index) => ({ targetId: `partial-${index + 1}` })),
    {
      responseShapeOk: false,
      pagesRequested: 2,
      rawWorks: 30,
      invalidWorks: 0,
      outsideDateWorks: 0,
    }
  );
  assert.deepStrictEqual(
    partialSecondPageShapeChange.works,
    [],
    "第二页结构变化时必须丢弃第一页的部分目标并回退官方 UI 日期扫描"
  );
  assert.strictEqual(partialSecondPageShapeChange.diagnostics.acceptedWorks, 30);
  assert.strictEqual(partialSecondPageShapeChange.diagnostics.reason, "response_shape_changed");
  const partialUnparseableItem = mod.finalizeBilibiliTargetDiscovery(
    [{ targetId: "valid-1" }],
    {
      responseShapeOk: true,
      pagesRequested: 1,
      rawWorks: 2,
      invalidWorks: 1,
      outsideDateWorks: 0,
    }
  );
  assert.deepStrictEqual(
    partialUnparseableItem.works,
    [],
    "任意稿件解析失败时不得继续使用部分接口目标"
  );
  assert.strictEqual(partialUnparseableItem.diagnostics.reason, "unparseable_items");
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
