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
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
