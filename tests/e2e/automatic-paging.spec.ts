import { expect, test } from '@playwright/test';
import { base, noOverflow } from './session';

test.use({ serviceWorkers: 'block', reducedMotion: 'reduce' });

for (const width of [390, 1440]) test(`timeline automatically loads at the bottom at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 800 });
  const calls: string[] = [];
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== '/api/v1/timeline') return route.fulfill({ json: {} });
    const cursor = url.searchParams.get('cursor') ?? '';
    calls.push(cursor);
    if (cursor === '12') await gate;
    const offset = Number(cursor || 0);
    return route.fulfill({ json: Array.from({ length: offset === 24 ? 2 : 12 }, (_, n) => ({
      id: `post-${offset + n + 1}`, provider: 'wechat', title: '朋友圈', profile_name: '分页测试',
      summary: `动态 ${offset + n + 1}：这是自动加载的动态内容。`.repeat(12),
      cursor: String(offset + n + 1), media: [], occurred_at: '2026-10-18T04:00:00Z',
    })) });
  });
  await page.goto(`${base}/timeline?provider=wechat`);
  const cards = page.locator('article[data-source="wechat"]');
  await expect(cards).toHaveCount(12);
  await expect(page.getByRole('button', { name: /再看|加载更多/ })).toHaveCount(0);
  await page.getByRole('status').filter({ hasText: /继续下滑加载更多|正在加载…/ }).scrollIntoViewIfNeeded();
  await expect.poll(() => calls).toEqual(['', '12']);
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  // The in-flight boundary cannot issue a second request for the same cursor.
  expect(calls.filter((cursor) => cursor === '12')).toHaveLength(1);
  release();
  await expect.poll(() => cards.count()).toBeGreaterThanOrEqual(24);
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await expect(cards).toHaveCount(26);
  await expect(page.getByText('继续下滑加载更多')).toHaveCount(0);
  expect(calls).toEqual(['', '12', '24']);
  expect(await page.evaluate(noOverflow)).toBeTruthy();
});

test('a short first page fills the viewport automatically and stops on a failed page', async ({ page }) => {
  let fail = true;
  let nextCalls = 0;
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/v1/faces/summary') return route.fulfill({ json: { scanned: 3, faces: 3, to_review: 3, confirmed: 0, people: 0 } });
    if (url.pathname !== '/api/v1/faces/groups') return route.fulfill({ json: {} });
    if (url.searchParams.get('offset') === '1') {
      nextCalls++;
      if (fail) return route.fulfill({ status: 503, json: {} });
      return route.fulfill({ json: { items: [], next_offset: null } });
    }
    return route.fulfill({ json: { items: [], next_offset: 1 } });
  });
  await page.goto(`${base}/faces`);
  await expect(page.getByRole('button', { name: '重试加载' })).toBeVisible();
  const attempts = nextCalls;
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  expect(nextCalls).toBe(attempts);
  fail = false;
  await page.getByRole('button', { name: '重试加载' }).click();
  await expect(page.getByRole('button', { name: '重试加载' })).toHaveCount(0);
  await expect(page.getByText('继续下滑加载更多')).toHaveCount(0);
});

test('weekly people continue automatically without the 12-person button', async ({ page }) => {
  const cursors: string[] = [];
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/v1/ai/digests') return route.fulfill({ json: { enabled: true, digests: [] } });
    if (url.pathname !== '/api/v1/ai/weekly-people') return route.fulfill({ json: {} });
    const cursor = url.searchParams.get('after') ?? '';
    cursors.push(cursor);
    return route.fulfill({ json: { enabled: true, week_start: '2026-10-12', weeks: ['2026-10-12'], total: 13,
      people: Array.from({ length: cursor ? 1 : 12 }, (_, n) => ({ profile_id: `weekly-${cursor ? 13 : n + 1}`,
        name: `每周联系人${cursor ? 13 : n + 1}`, summary: '最近过得不错。'.repeat(12), sources: [],
        week_start: '2026-10-12', generated_at: '2026-10-18T00:00:00Z' })), next_cursor: cursor ? null : 'older',
    } });
  });
  await page.goto(`${base}/digest?view=people`);
  await expect(page.getByText('每周联系人12', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '再看 12 人' })).toHaveCount(0);
  await page.getByText('继续下滑加载更多').scrollIntoViewIfNeeded();
  await expect(page.getByText('每周联系人13', { exact: true })).toBeVisible();
  expect(cursors).toEqual(['', 'older']);
});

test('suggestions automatically paginate past the former 100-person cap', async ({ page }) => {
  const offsets: number[] = [];
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== '/api/v1/ai/suggestions') return route.fulfill({ json: {} });
    const offset = Number(url.searchParams.get('offset') ?? 0);
    offsets.push(offset);
    return route.fulfill({ json: { enabled: true, total: 101, kinds: { company: 101 },
      people: Array.from({ length: Math.min(20, 101 - offset) }, (_, n) => ({
        profile_id: `suggested-${offset + n + 1}`, name: `建议联系人${offset + n + 1}`, generated_at: '2026-10-18T00:00:00Z',
        items: [{ fingerprint: `item-${offset + n + 1}`, kind: 'company', label: '公司', value: '测试公司', quote: '', evidence: [], confidence: 'high' }],
      })),
    } });
  });
  await page.goto(`${base}/suggestions`);
  await expect(page.getByText('建议联系人20', { exact: true })).toBeVisible();
  for (let loaded = 40; loaded <= 100; loaded += 20) {
    await page.getByText('继续下滑加载更多').scrollIntoViewIfNeeded();
    await expect(page.getByText(`建议联系人${loaded}`, { exact: true })).toBeVisible();
  }
  await page.getByText('继续下滑加载更多').scrollIntoViewIfNeeded();
  await expect(page.getByText('建议联系人101', { exact: true })).toBeVisible();
  await expect(page.getByText('继续下滑加载更多')).toHaveCount(0);
  expect(offsets).toEqual([0, 20, 40, 60, 80, 100]);
});

test('account linking lists append rows automatically instead of changing pages', async ({ page }) => {
  const requested: number[] = [];
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/v1/link-review/linked') return route.fulfill({ json: Array.from({ length: 51 }, (_, n) => ({
      profile_id: `linked-${n + 1}`, display_name: `关联联系人${n + 1}`, accounts: [], signals: [],
    })) });
    if (url.pathname !== '/api/v1/link-review') return route.fulfill({ json: {} });
    const current = Number(url.searchParams.get('page') || 1);
    requested.push(current);
    return route.fulfill({ json: { pairs: [], total: 51, unresolved: 51, page: current, page_size: 50,
      queue: Array.from({ length: current === 1 ? 50 : 1 }, (_, n) => ({
        account_id: `account-${current === 1 ? n + 1 : 51}`, display_name: `待关联账号${current === 1 ? n + 1 : 51}`,
        username: `handle-${current}-${n}`, provider: 'instagram', ready: true, profile_id: null,
        candidates: [], signals: [],
      })),
    } });
  });
  await page.goto(`${base}/linking`);
  await expect(page.getByText('关联联系人50', { exact: true })).toBeVisible();
  await page.getByRole('region', { name: '已关联', exact: true }).getByText('继续下滑加载更多').scrollIntoViewIfNeeded();
  await expect(page.getByText('关联联系人51', { exact: true })).toBeVisible();
  await expect(page.getByText('关联联系人1', { exact: true })).toHaveCount(1);
  expect(requested).toEqual([1]);
  await page.getByRole('button', { name: /^待关联/ }).click();
  await expect(page.getByText('待关联账号50', { exact: true })).toBeVisible();
  await page.getByRole('region', { name: '待关联', exact: true }).getByText('继续下滑加载更多').scrollIntoViewIfNeeded();
  await expect(page.getByText('待关联账号51', { exact: true })).toBeVisible();
  await expect(page.getByText('待关联账号1', { exact: true })).toHaveCount(1);
  expect(requested).toEqual([1, 2]);
  await expect(page.getByRole('button', { name: '下一页' })).toHaveCount(0);
});
