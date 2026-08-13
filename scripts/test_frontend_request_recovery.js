const assert = require("assert");
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.resolve(__dirname, "..", "frontend", "progress.html"),
  "utf8",
);

function loadApi(fetchImpl) {
  const start = html.indexOf("  const api = {");
  const end = html.indexOf("\n\n  const LICENSE_ACTIVATION_TRANSIENT_ERRORS", start);
  assert(start >= 0 && end > start, "unable to locate API helper");
  const source = html.slice(start, end);
  const factory = new Function(
    "fetch",
    "AbortController",
    "setTimeout",
    "clearTimeout",
    "console",
    `const state = { backendOnline: true };
     const SESSION_TOKEN = '';
     ${source}
     return { api, state };`,
  );
  return factory(fetchImpl, AbortController, setTimeout, clearTimeout, {
    error() {},
  });
}

async function testHungRequestIsAbortedAndNextRequestCanRecover() {
  const calls = [];
  let mode = "hang";
  const { api, state } = loadApi((_url, options) => {
    calls.push(options);
    if (mode === "ok") {
      return Promise.resolve({ json: async () => ({ ok: true }) });
    }
    return new Promise((_resolve, reject) => {
      options.signal?.addEventListener(
        "abort",
        () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })),
        { once: true },
      );
    });
  });

  const first = await Promise.race([
    api.request("GET", "/progress", null, { timeoutMs: 20 }),
    new Promise((resolve) => setTimeout(() => resolve("still_hung"), 80)),
  ]);
  assert.notStrictEqual(first, "still_hung", "hung request should settle after timeout");
  assert.strictEqual(first, null);
  assert.strictEqual(calls[0].signal.aborted, true);
  assert.strictEqual(state.backendOnline, false);

  mode = "ok";
  const second = await api.request("GET", "/progress", null, { timeoutMs: 20 });
  assert.deepStrictEqual(second, { ok: true });
  assert.strictEqual(state.backendOnline, true);
}

testHungRequestIsAbortedAndNextRequestCanRecover().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
