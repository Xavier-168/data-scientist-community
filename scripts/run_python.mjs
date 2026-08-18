#!/usr/bin/env node
/**
 * 跨平台 Python 启动代理。
 *
 * 优先使用仓库内虚拟环境解释器（Windows 为 .venv\Scripts\python.exe，
 * macOS/Linux 为 .venv/bin/python），不存在时回退到 PATH 中的
 * python3/python。用法：
 *
 *   node scripts/run_python.mjs <任意参数...>
 *
 * 例如：
 *   node scripts/run_python.mjs -m unittest discover -s scripts -p "test_*.py"
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function resolvePythonBin() {
  const venvCandidates =
    process.platform === "win32"
      ? [path.join(projectRoot, ".venv", "Scripts", "python.exe")]
      : [path.join(projectRoot, ".venv", "bin", "python")];
  for (const candidate of venvCandidates) {
    if (existsSync(candidate)) {
      return candidate;
    }
  }
  const pathCandidates = process.platform === "win32" ? ["python.exe", "python"] : ["python3", "python"];
  for (const name of pathCandidates) {
    const probe = spawnSync(name, ["--version"], { stdio: "ignore", shell: process.platform === "win32" });
    if (probe.status === 0) {
      return name;
    }
  }
  return null;
}

const pythonBin = resolvePythonBin();
if (!pythonBin) {
  console.error("[run_python] 未找到可用的 Python（先创建 .venv 或把 python3/python 加入 PATH）。");
  process.exit(127);
}

const result = spawnSync(pythonBin, process.argv.slice(2), {
  cwd: process.cwd(),
  stdio: "inherit",
  env: process.env,
});
process.exit(result.status ?? 1);
