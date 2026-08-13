import { describe, expect, it } from 'vitest';

describe('desktop test harness', () => {
  it('provides jsdom and DOM matchers', () => {
    const status = document.createElement('p');
    status.textContent = '数据科学家 Community 正在启动';
    document.body.append(status);

    expect(status).toBeInTheDocument();
    expect(status).toHaveTextContent('数据科学家 Community 正在启动');
  });
});
