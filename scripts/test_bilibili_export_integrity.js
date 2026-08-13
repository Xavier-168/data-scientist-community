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
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
