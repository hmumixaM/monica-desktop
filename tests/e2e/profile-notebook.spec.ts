import {test, expect, type Page} from '@playwright/test';
import {base, noOverflow, storageState} from './session';

const vincent = 'c8795960-e80a-48ff-8e5c-6f65d69aae6b';
const target = 'f977df28-a35b-49a8-b7ab-8825eeafd960';
const group = '96c7c93f-3b71-4bf4-92fb-d21ed1b6bdf9';
test.use({storageState});

const map = (page: Page) => page.locator('[data-map-ready]');
const section = (page: Page, heading: string) => page.locator('section', {has: page.getByRole('heading', {name: heading, exact: true})});

test('footprint map survives initialization on a slow connection', async ({page}) => {
  let release = () => {};
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route('https://tiles.openfreemap.org/styles/**', async (route) => { await gate; await route.continue(); });
  try {
    await page.setViewportSize({width: 390, height: 844});
    await page.goto(`${base}/profiles/${vincent}`);
    const places = page.getByRole('region', {name: '足迹与地点'});
    await places.getByRole('button', {name: '全球'}).click();
    release();
    await expect(map(page).locator('canvas')).toBeVisible({timeout: 20000});
  } finally {
    release();
  }
});

for (const width of [1440, 390]) test.describe(`${width}px notebook`, () => {
  test.use({viewport: {width, height: 1000}});

  test('real cover, platform bios, map and every profile tab', async ({page}) => {
    const errors: string[] = [];
    const failed: string[] = [];
    const requests: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    page.on('request', (r) => { if (r.url().includes('/api/')) requests.push(r.url()); });
    page.on('response', (r) => { if (r.url().includes('/api/') && r.status() >= 500) failed.push(r.url()); });

    await page.goto(`${base}/profiles/${vincent}`);
    await expect(page.getByRole('heading', {level: 1})).toHaveText(/^Vincent 鲍涵/, {timeout: 20000});
    await expect(page.locator('header span[data-provider="linkedin"]:has(> b)')).toHaveCount(1);
    await expect(page.getByRole('region', {name: '职业与教育'})).toContainText('Carnegie Mellon');
    await expect(section(page, '微信资料')).toContainText('Vincent12526');

    await page.getByRole('button', {name: '查看朋友圈封面原图'}).click();
    const original = page.locator('dialog[open] img[data-zoom-target]');
    await expect.poll(() => original.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
    expect(await original.getAttribute('src')).not.toContain('variant=thumb');
    await page.getByRole('button', {name: '原尺寸放大'}).click();
    await expect(page.locator('dialog[open] [data-zoomed]')).toHaveCount(1);
    await page.keyboard.press('Escape');

    await expect(page.getByRole('region', {name: '互动'})).toBeVisible();
    await expect(map(page).locator('canvas')).toBeVisible({timeout: 20000});
    await page.getByRole('region', {name: '足迹与地点'}).getByRole('button', {name: '全球'}).click();
    const location = await (await page.request.get(`${base}/api/v1/profiles/${vincent}/locations`)).json();
    expect(location.points[0].first_seen.slice(0, 10)).toBe('2026-04-25');
    expect(location.points[0].latitude).toBeCloseTo(39.8752, 3);
    expect(requests.some((url) => url.includes('/messages?') || url.includes('/history'))).toBeFalsy();

    const panels: [string, (page: Page) => Promise<void>][] = [
      ['relationships', async () => {
        await expect(page.getByRole('heading', {name: '朋友圈互动', exact: true})).toBeVisible();
        const links = page.locator('[data-moments-graph-svg] a');
        await expect(links.nth(1)).toBeVisible({timeout: 20000});
        expect(await links.nth(1).getAttribute('href')).toMatch(/^\/profiles\/[0-9a-f-]+$/);
        await page.getByRole('button', {name: '缩小', exact: true}).click();
        await page.getByRole('button', {name: '暂停运动', exact: true}).click();
      }],
      ['records', () => expect(page.getByRole('heading', {name: '笔记与记录', exact: true})).toBeVisible()],
      ['history', () => expect(page.getByRole('heading', {name: '字段变更记录', exact: true})).toBeVisible()],
      ['messages', () => expect(page.getByRole('region', {name: '聊天记录'})).toBeVisible()],
      ['timeline', () => expect(page.locator('[data-tab-panel="timeline"] article').first()).toBeVisible()],
    ];
    for (const [tab, check] of panels) {
      await page.goto(`${base}/profiles/${vincent}?tab=${tab}`);
      await expect(page.locator(`[data-tab-panel="${tab}"]`)).toBeVisible({timeout: 20000});
      await check(page);
      expect(await page.evaluate(noOverflow)).toBeTruthy();
      await page.screenshot({path: `.local-validation/notebook-${tab}-${width}.png`});
    }

    await page.goto(`${base}/profiles/${target}`);
    await expect(page.locator('figure[data-source="instagram"]')).toContainText(/shanghai/i, {timeout: 20000});
    await page.screenshot({path: `.local-validation/notebook-target-${width}.png`, fullPage: true});
    expect(errors).toEqual([]);
    expect(failed).toEqual([]);
  });

  test('all three platforms have real interactive statistics', async ({page}) => {
    await page.goto(`${base}/insights`);
    const stats = page.getByRole('region', {name: '社交统计'});
    await expect(stats).toBeVisible({timeout: 30000});
    const bars = stats.getByRole('region', {name: /^每月动态/}).getByRole('link');
    for (const provider of ['微信', 'Instagram', 'LinkedIn']) {
      await stats.getByRole('group', {name: '统计平台'}).getByRole('button', {name: provider}).click();
      await expect(bars.last()).toBeAttached();
      expect(await stats.innerText()).not.toContain('NaN');
      expect(await page.evaluate(noOverflow)).toBeTruthy();
    }
    const month = stats.locator('[data-month]:not([data-empty])').last();
    const value = await month.getAttribute('data-month');
    await month.scrollIntoViewIfNeeded();
    await month.click();
    await expect(page).toHaveURL(new RegExp(`/timeline\\?.*month=${value}`));
    await expect(page.getByLabel('只看某个月')).toHaveValue(value!);
    await expect(page.locator('article[data-source="linkedin"]').first()).toBeVisible();
    await page.screenshot({path: `.local-validation/notebook-insights-${width}.png`, fullPage: true});
  });

  test('group shows source-proven members and never personal Moments', async ({page}) => {
    const requests: string[] = [];
    page.on('request', (r) => { if (r.url().includes('/api/')) requests.push(r.url()); });
    await page.goto(`${base}/profiles/${group}`);
    await expect(page.getByRole('heading', {level: 1})).toHaveText(/^sunstone 租房/, {timeout: 20000});
    const members = section(page, '群成员');
    const people = members.locator('a[href^="/profiles/"]:not([href*="?"])');
    await expect(people).toHaveCount(4);
    await expect(members).toContainText('455');
    await expect(section(page, '群主')).toBeVisible();
    expect(requests.some((url) => /\/locations|\/moments-circle|\/messages\?|\/timeline\?/.test(url))).toBeFalsy();
    await expect(page.locator('[data-tab-panel] article, [data-moments-graph-svg], [data-map-ready]')).toHaveCount(0);
    const locations = await (await page.request.get(`${base}/api/v1/profiles/${group}/locations`)).json();
    expect(locations.points).toEqual([]);
    const relationships = await (await page.request.get(`${base}/api/v1/profiles/${group}/relationships`)).json();
    expect(relationships).toHaveLength(4);
    expect(relationships.every((r: any) => r.id.startsWith('membership:'))).toBeTruthy();
    await page.getByRole('navigation', {name: '资料分区'}).getByRole('button', {name: '成员', exact: true}).click();
    await expect(page.locator('[data-tab-panel="relationships"] a[href^="/profiles/"]:not([href*="?"])')).toHaveCount(4);
    await page.screenshot({path: `.local-validation/group-boundary-${width}.png`, fullPage: true});
  });

  test('scrolling loads more posts and contacts; tags open filtered people', async ({page}) => {
    await page.goto(`${base}/profiles/${target}?tab=timeline`);
    const posts = page.locator('[data-tab-panel="timeline"] article');
    await expect(posts).toHaveCount(18, {timeout: 20000});
    const next = page.waitForResponse((r) => r.url().includes('/api/v1/timeline?') && r.url().includes('cursor='));
    await page.getByRole('button', {name: '加载更多'}).scrollIntoViewIfNeeded();
    expect((await next).ok()).toBeTruthy();
    await expect.poll(() => posts.count()).toBeGreaterThan(18);

    // The contact list is a virtualized view of the local directory: scrolling renders later rows without paging requests.
    await page.goto(`${base}/profiles`);
    const rows = page.locator('a[role="row"]');
    await expect(rows.first()).toBeVisible({timeout: 20000});
    const firstIndex = Number(await rows.first().getAttribute('aria-rowindex'));
    await rows.first().evaluate((row) => {
      let el: HTMLElement | null = row.parentElement;
      while (el && el.scrollHeight <= el.clientHeight + 1) el = el.parentElement;
      (el ?? document.scrollingElement!).scrollBy(0, 6000);
    });
    await expect.poll(async () => Number(await rows.last().getAttribute('aria-rowindex'))).toBeGreaterThan(firstIndex + 48);
    await page.getByRole('button', {name: '标签', exact: true}).click();
    await expect(page.getByRole('region', {name: '按标签查找'}).getByRole('button').first()).toBeVisible();

    await page.goto(`${base}/profiles/${vincent}`);
    const tag = section(page, '标签').getByRole('link', {name: 'UNC', exact: true});
    await expect(tag).toHaveCount(1, {timeout: 20000});
    await tag.click();
    await expect(page).toHaveURL(/\/profiles\?tag=/);
    await expect(page.locator('a[role="row"]').first()).toBeVisible();
    await expect(page.locator('[aria-label="当前筛选"]')).toContainText('UNC');
  });
});
