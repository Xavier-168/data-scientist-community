const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");

async function loadDouyinHelpers() {
  const sourcePath = path.resolve(__dirname, "douyin_export.mjs");
  const helperPath = path.resolve(__dirname, "browser_auth_utils.mjs");
  const runtimePathsPath = path.resolve(__dirname, "runtime_paths.mjs");
  const titleCleanupPath = path.resolve(__dirname, "title_cleanup_utils.mjs");
  const source = fs.readFileSync(sourcePath, "utf8");
  const sanitized = source.replace(
    /\nawait main\(\);\s*$/,
    "\nexport { assertExportOutcome, buildFinalCompletionPatch, classifyExportOutcome, normalizeFinalMetrics };\n"
  );
  const tempDir = fs.mkdtempSync(path.join(__dirname, ".douyin-export-test-"));
  const tempPath = path.join(tempDir, "douyin_export.testable.mjs");
  const tempHelperPath = path.join(tempDir, "browser_auth_utils.mjs");
  const tempRuntimePathsPath = path.join(tempDir, "runtime_paths.mjs");
  const tempTitleCleanupPath = path.join(tempDir, "title_cleanup_utils.mjs");
  fs.writeFileSync(tempPath, sanitized, "utf8");
  fs.copyFileSync(helperPath, tempHelperPath);
  fs.copyFileSync(runtimePathsPath, tempRuntimePathsPath);
  if (fs.existsSync(titleCleanupPath)) fs.copyFileSync(titleCleanupPath, tempTitleCleanupPath);
  try {
    return await import(pathToFileURL(tempPath).href);
  } finally {
    try {
      fs.rmSync(tempDir, { recursive: true, force: true });
    } catch {
      // ignore
    }
  }
}

async function main() {
  const mod = await loadDouyinHelpers();

  const reused = mod.buildFinalCompletionPatch(
    {
      totalWorks: 3,
      processedWorks: 3,
      successWorks: 0,
      skippedWorks: 3,
      failedWorks: 0,
    },
    6
  );
  assert.strictEqual(reused.totalWorks, 3);
  assert.strictEqual(reused.skippedWorks, 3);
  assert(reused.message.includes("沿用已有 6 份本地作品结果"));

  const empty = mod.buildFinalCompletionPatch(
    {
      totalWorks: 0,
      processedWorks: 0,
      successWorks: 0,
      skippedWorks: 0,
      failedWorks: 0,
    },
    0
  );
  assert.strictEqual(empty.totalWorks, 0);
  assert.strictEqual(empty.successWorks, 0);
  assert.strictEqual(empty.message, "抖音任务完成，当前账号暂无可采集作品");

  assert.throws(
    () => mod.assertExportOutcome({
      totalWorks: 3,
      processedWorks: 3,
      successWorks: 0,
      skippedWorks: 0,
      failedWorks: 3,
    }),
    /all_candidates_failed/
  );
  assert.strictEqual(
    mod.classifyExportOutcome({
      totalWorks: 3,
      processedWorks: 3,
      successWorks: 2,
      skippedWorks: 0,
      failedWorks: 1,
    }),
    "partial_failure"
  );
  const partialMetrics = mod.normalizeFinalMetrics({
    totalWorks: 28,
    processedWorks: 23,
    successWorks: 21,
    skippedWorks: 0,
    failedWorks: 2,
  });
  assert.strictEqual(partialMetrics.queuedWorks, 5);
  assert.throws(
    () => mod.assertExportOutcome(partialMetrics),
    (error) => error?.code === "partial_failure"
      && /成功 21 条/.test(error.message)
      && /失败 2 条/.test(error.message)
      && /待重试 5 条/.test(error.message)
  );
  assert.strictEqual(
    mod.classifyExportOutcome({
      totalWorks: 3,
      processedWorks: 3,
      successWorks: 0,
      skippedWorks: 3,
      failedWorks: 0,
    }),
    "success"
  );

  const page = mod.extractDouyinWorkListPage({
    items: [{ id: "1" }, { id: "2" }],
    has_more: true,
    max_cursor: 123456,
    total: 174,
  });
  assert.strictEqual(page.items.length, 2);
  assert.strictEqual(page.hasMore, true);
  assert.strictEqual(page.nextCursor, 123456);
  assert.strictEqual(page.total, 174);

  assert.strictEqual(
    mod.shouldStopDouyinListScan({
      eligibleCount: 11,
      limit: 999,
      staleRounds: 3,
      staleRoundsLimit: 3,
      apiHasMore: true,
    }),
    false,
    "API 明确 has_more=true 时不能因为页面窗口不滚动就提前完成"
  );
  assert.strictEqual(
    mod.shouldStopDouyinListScan({
      eligibleCount: 11,
      limit: 999,
      staleRounds: 3,
      staleRoundsLimit: 3,
      apiHasMore: false,
    }),
    true
  );
  assert.strictEqual(
    mod.shouldStopDouyinListScan({
      eligibleCount: 18,
      limit: 999,
      staleRounds: 0,
      staleRoundsLimit: 3,
      apiHasMore: true,
      apiReachedDateBoundary: true,
    }),
    true,
    "跨过用户起始日期后应停止继续翻取更老作品"
  );
  assert.strictEqual(
    mod.isDouyinPaginationStalled({ apiHasMore: true, staleRounds: 7, stallRoundsLimit: 8 }),
    false,
  );
  assert.strictEqual(
    mod.isDouyinPaginationStalled({ apiHasMore: true, staleRounds: 8, stallRoundsLimit: 8 }),
    true,
    "平台声称仍有下一页但连续多轮没有新响应时必须明确失败，不能永久循环",
  );
  assert.strictEqual(
    mod.isDouyinPaginationStalled({ apiHasMore: false, staleRounds: 8, stallRoundsLimit: 8 }),
    false,
  );

  const attempts = [];
  const sleeps = [];
  const recovered = await mod.retryTransientOperation(
    async (attempt) => {
      attempts.push(attempt);
      if (attempt === 1) throw new Error("page.goto: Timeout 30000ms exceeded.");
      if (attempt === 2) throw new Error("page.goto: net::ERR_CONNECTION_CLOSED");
      return "recovered";
    },
    {
      maxAttempts: 3,
      retryDelaysMs: [3000, 8000],
      sleep: async (delayMs) => sleeps.push(delayMs),
    },
  );
  assert.strictEqual(recovered, "recovered");
  assert.deepStrictEqual(attempts, [1, 2, 3]);
  assert.deepStrictEqual(sleeps, [3000, 8000]);

  const exportClickTimeout = new Error("locator.click: Timeout 10000ms exceeded.");
  assert.strictEqual(
    mod.isDouyinDetailNavigationExhausted(exportClickTimeout),
    false,
    "导出按钮超时不得误触发详情导航熔断",
  );
  const exhaustedNavigation = new Error("page.goto: net::ERR_CONNECTION_CLOSED");
  exhaustedNavigation.code = "douyin_detail_navigation_exhausted";
  assert.strictEqual(mod.isDouyinDetailNavigationExhausted(exhaustedNavigation), true);

  let nonTransientAttempts = 0;
  await assert.rejects(
    () => mod.retryTransientOperation(
      async () => {
        nonTransientAttempts += 1;
        throw new Error("missing_export_button");
      },
      { maxAttempts: 3, retryDelaysMs: [0, 0] },
    ),
    /missing_export_button/,
  );
  assert.strictEqual(nonTransientAttempts, 1);

  const mainSource = fs.readFileSync(path.resolve(__dirname, "douyin_export.mjs"), "utf8");
  const masterMergeIndex = mainSource.indexOf("await mergeAllVideos();", mainSource.indexOf("async function main()"));
  const partialAssertIndex = mainSource.indexOf("assertExportOutcome(metrics);", mainSource.indexOf("async function main()"));
  assert(masterMergeIndex > 0 && partialAssertIndex > masterMergeIndex,
    "部分成功的主表必须在最终 partial_failure 抛出前生成");
  const pendingSettleIndex = mainSource.indexOf("const settled = await settlePendingWorkListResponses()", mainSource.indexOf("async function exportRecentWorksFromList"));
  const stopDecisionIndex = mainSource.indexOf("if (shouldStopDouyinListScan", mainSource.indexOf("async function exportRecentWorksFromList"));
  assert(pendingSettleIndex > 0 && stopDecisionIndex > pendingSettleIndex,
    "任何停止扫描判断前必须等待已捕获的作品列表响应完成解析");
  assert(mainSource.includes("sequence >= latestAppliedWorkListResponseSequence"),
    "分页状态只能由最新作品列表响应更新，旧响应不得反向覆盖游标");
  assert(mainSource.includes("responseEventTarget.off('response', workListResponseListener)"),
    "作品列表监听器必须在 finally 中移除");
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
