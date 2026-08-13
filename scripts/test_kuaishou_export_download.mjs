import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'ks-export-download-'));
process.env.KS_DETAIL_EXPORT_DIR = tempDir;

const {
  extractKuaishouExportTaskId,
  extractKuaishouListPage,
  extractKuaishouTitle,
  isExpectedKuaishouExportCreateResponse,
  saveCompletedExportTask,
  shouldStopKuaishouPagination,
  waitForUniqueNewCompletedExportTask,
} = await import('./kuaishou_export.mjs');

try {
  const calls = [];
  const page = {
    request: {
      async get(url, options) {
        calls.push({ url, options });
        return {
          ok: () => true,
          status: () => 200,
          body: async () => Buffer.from('real-export-bytes'),
        };
      },
    },
  };

  const outputPath = await saveCompletedExportTask(
    page,
    { taskId: 'task-123', filename: '作品数据分析明细表.xlsx' },
    { 作品ID: 'work-456' },
    'play',
    'api-ph-value',
  );

  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /\/analysis\/export\/download$/);
  assert.deepEqual(calls[0].options.params, {
    taskId: 'task-123',
    'kuaishou.web.cp.api_ph': 'api-ph-value',
  });
  assert.equal(await fs.readFile(outputPath, 'utf8'), 'real-export-bytes');
  assert.match(path.basename(outputPath), /^ks-detail-work-456-play-/);

  // 快手标题可能以 # 开头；正文前导 # 不能导致整段标题被清空。
  assert.equal(
    extractKuaishouTitle({ title: '#三分钟，教你一套万能AI PPT生成大法！【小白必会】#ai #ppt #教程' }),
    '三分钟，教你一套万能AI PPT生成大法！【小白必会】',
  );
  // 平台源只有话题时保留原值，不凭空制造标题，也不再同步空字符串。
  assert.equal(
    extractKuaishouTitle({ title: '#ai #AI新星计划 #教程' }),
    '#ai #AI新星计划 #教程',
  );
  assert.equal(
    extractKuaishouTitle({ title: '#ai #教程', name: '作者名' }),
    '#ai #教程',
  );
  assert.equal(
    extractKuaishouTitle({ title: '#AI（人工智能） #教程' }),
    '#AI（人工智能） #教程',
  );
  assert.equal(extractKuaishouTitle({ title: '#AI，未来 #教程' }), '#AI，未来 #教程');
  assert.equal(extractKuaishouTitle({ title: '#AI【人工智能】 #教程' }), '#AI【人工智能】 #教程');
  assert.equal(extractKuaishouTitle({ title: 'C#入门指南 #教程' }), 'C#入门指南');
  assert.equal(extractKuaishouTitle({ title: '普通正文 #ai #教程' }), '普通正文');
  assert.equal(extractKuaishouTitle({ title: '', caption: 'caption 正文 #ai' }), 'caption 正文');
  assert.equal(
    extractKuaishouTitle({ title: { text: '错误对象' }, name: '弱兜底', photo: { photoTitle: '嵌套标题 #ai' } }),
    '嵌套标题',
  );
  assert.equal(extractKuaishouTitle({ work: { caption: 'work 标题' } }), 'work 标题');
  assert.equal(extractKuaishouTitle({ item: { desc: 'item 标题' } }), 'item 标题');
  assert.equal(extractKuaishouTitle({ content: { description: 'content 标题' } }), 'content 标题');
  assert.equal(extractKuaishouTitle({ name: '最后兜底' }), '最后兜底');
  assert.equal(extractKuaishouTitle({ title: { text: '错误对象' } }), '');
  assert.equal(extractKuaishouTitle(null), '');

  const pollingPage = { waitForTimeout: async () => {} };
  const taskSnapshots = [
    [
      { taskId: 'old-task', status: 3, filename: '作品数据分析明细表.xlsx' },
      { taskId: 'new-task', status: 1, filename: '作品数据分析明细表.xlsx' },
    ],
    [
      { taskId: 'old-task', status: 3, filename: '作品数据分析明细表.xlsx' },
      { taskId: 'new-task', status: 3, filename: '作品数据分析明细表.xlsx' },
    ],
    [
      { taskId: 'old-task', status: 3, filename: '作品数据分析明细表.xlsx' },
      { taskId: 'new-task', status: 3, filename: '作品数据分析明细表.xlsx' },
    ],
    [
      { taskId: 'old-task', status: 3, filename: '作品数据分析明细表.xlsx' },
      { taskId: 'new-task', status: 3, filename: '作品数据分析明细表.xlsx' },
    ],
  ];
  const selectedTask = await waitForUniqueNewCompletedExportTask(
    pollingPage,
    'api-ph-value',
    new Set(['old-task']),
    '作品数据分析明细表',
    {
      fetchTasks: async () => taskSnapshots.shift() ?? [],
      timeoutMs: 100,
      pollMs: 0,
    },
  );
  assert.equal(String(selectedTask.taskId), 'new-task');

  await assert.rejects(
    () => waitForUniqueNewCompletedExportTask(
      pollingPage,
      'api-ph-value',
      new Set(['old-task']),
      '作品数据分析明细表',
      {
        fetchTasks: async () => [
          { taskId: 'new-a', status: 3, filename: '作品数据分析明细表.xlsx' },
          { taskId: 'new-b', status: 3, filename: '作品数据分析明细表.xlsx' },
        ],
        timeoutMs: 100,
        pollMs: 0,
      },
    ),
    /新增任务不唯一/,
  );

  const completedFirstSnapshots = [
    [
      { taskId: 'old-task', status: 3, filename: '作品数据分析明细表.xlsx' },
      { taskId: 'other-task', status: 3, filename: '作品数据分析明细表.xlsx' },
    ],
    [
      { taskId: 'other-task', status: 3, filename: '作品数据分析明细表.xlsx' },
      { taskId: 'our-task', status: 1, filename: '作品数据分析明细表.xlsx' },
    ],
  ];
  await assert.rejects(
    () => waitForUniqueNewCompletedExportTask(
      pollingPage,
      'api-ph-value',
      new Set(['old-task']),
      '作品数据分析明细表',
      {
        fetchTasks: async () => completedFirstSnapshots.shift() ?? [],
        timeoutMs: 100,
        pollMs: 0,
      },
    ),
    /新增任务不唯一/,
  );

  const exactTask = await waitForUniqueNewCompletedExportTask(
    pollingPage,
    'api-ph-value',
    new Set(['old-task']),
    '作品数据分析明细表',
    {
      expectedTaskId: 'our-task',
      fetchTasks: async () => [
        { taskId: 'other-task', status: 3, filename: '作品数据分析明细表.xlsx' },
        { taskId: 'our-task', status: 3, filename: '作品数据分析明细表.xlsx' },
      ],
      timeoutMs: 100,
      pollMs: 0,
    },
  );
  assert.equal(exactTask.taskId, 'our-task');
  assert.equal(extractKuaishouExportTaskId({ data: { taskId: 12345 } }), '12345');
  assert.equal(extractKuaishouExportTaskId({ data: { exportTaskId: 'task-x' } }), 'task-x');
  assert.equal(extractKuaishouExportTaskId({ data: {} }), '');

  const staggeredSnapshots = [
    [
      { taskId: 'old-task', status: 3, filename: '作品数据分析明细表.xlsx' },
      { taskId: 'other-task', status: 1, filename: '作品数据分析明细表.xlsx' },
    ],
    [
      { taskId: 'other-task', status: 3, filename: '作品数据分析明细表.xlsx' },
      { taskId: 'our-task', status: 3, filename: '作品数据分析明细表.xlsx' },
    ],
  ];
  await assert.rejects(
    () => waitForUniqueNewCompletedExportTask(
      pollingPage,
      'api-ph-value',
      new Set(['old-task']),
      '作品数据分析明细表',
      {
        fetchTasks: async () => staggeredSnapshots.shift() ?? [],
        timeoutMs: 100,
        pollMs: 0,
      },
    ),
    /新增任务不唯一/,
  );

  const responseFor = (workId) => ({
    url: () => 'https://cp.kuaishou.com/rest/cp/creator/pc/analysis/photo/data/export',
    request: () => ({
      method: () => 'POST',
      postDataJSON: () => ({ workId, timeType: 3, exportType: 2 }),
    }),
  });
  assert.equal(isExpectedKuaishouExportCreateResponse(responseFor('work-456'), 'work-456'), true);
  assert.equal(isExpectedKuaishouExportCreateResponse(responseFor('other-work'), 'work-456'), false);

  const firstListPage = extractKuaishouListPage({
    data: { photoList: { photoItems: Array.from({ length: 10 }, (_, index) => ({ photoId: index })), totalCount: 15 } },
  }, 0, 10);
  assert.equal(firstListPage.hasMore, true);
  assert.equal(firstListPage.total, 15);
  assert.equal(shouldStopKuaishouPagination({
    pageItems: 10,
    collected: 10,
    limit: 200,
    hasMore: true,
  }), false, '接口明确还有下一页时必须继续拉取');
  assert.equal(shouldStopKuaishouPagination({
    pageItems: 5,
    collected: 15,
    limit: 200,
    hasMore: false,
  }), true);
} finally {
  await fs.rm(tempDir, { recursive: true, force: true });
}
