import assert from 'node:assert/strict';

const { isLoggedIn, waitForLogin } = await import('./xiaohongshu_export.mjs');

function makePage(visibilitySequence) {
  const sequence = [...visibilitySequence];
  let waits = 0;
  return {
    url: () => 'https://creator.xiaohongshu.com/statistics/data-analysis',
    locator(selector) {
      const isPrimaryMarker = selector === 'text=笔记管理';
      return {
        first() { return this; },
        async count() { return isPrimaryMarker ? 1 : 0; },
        async isVisible() { return isPrimaryMarker ? Boolean(sequence.shift()) : false; },
      };
    },
    async waitForTimeout() { waits += 1; },
    get waits() { return waits; },
  };
}

await (async function hiddenLoginMarkersDoNotCountAsAuthorized() {
  const page = makePage([false]);
  assert.equal(await isLoggedIn(page), false);
})();

await (async function loginMustRemainVisibleForTwoConsecutiveChecks() {
  const page = makePage([true, false, true, true]);
  const ready = await waitForLogin(page, {
    timeoutMs: 1000,
    announce: false,
    stableChecks: 2,
  });
  assert.equal(ready, true);
  assert.equal(page.waits, 3);
})();
