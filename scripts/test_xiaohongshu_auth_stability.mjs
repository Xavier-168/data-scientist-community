import assert from 'node:assert/strict';

const {
  extractXiaohongshuListPage,
  isLoggedIn,
  shouldStopXiaohongshuListScan,
  waitForLogin,
} = await import('./xiaohongshu_export.mjs');

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

await (async function listApiPreventsPrematureDomStop() {
  const firstPage = extractXiaohongshuListPage(
    { data: { note_infos: [{ id: 'note-1' }], total: 36 } },
    'https://creator.xiaohongshu.com/api/galaxy/creator/datacenter/note/analyze/list?page_size=10&page_num=1',
  );
  assert.equal(firstPage.hasMore, true);
  assert.equal(firstPage.pageNum, 1);
  assert.equal(firstPage.pageSize, 10);
  assert.equal(shouldStopXiaohongshuListScan({
    discovered: 10,
    stableRounds: 20,
    staleRoundsLimit: 6,
    expectedTotal: 36,
    apiHasMore: firstPage.hasMore,
  }), false, '接口明确还有下一页时，页面短暂不变不得早停');
  assert.equal(shouldStopXiaohongshuListScan({
    discovered: 36,
    stableRounds: 2,
    expectedTotal: 36,
    apiHasMore: false,
  }), true);
})();
