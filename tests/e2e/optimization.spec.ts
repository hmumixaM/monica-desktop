import { test, expect } from '@playwright/test';

const base = process.env.CRM_OPTIMIZATION_URL || 'http://127.0.0.1:3017';
const pid = '11111111-1111-4111-8111-111111111111';
const columns = ['id','type','name','summary','avatar_media_id','tag_ids','company_ids','school_ids','aliases','providers','headline','city','birthday_md','last_contact_at','direct_message_count','member_count','conversation_id','group_names'];
const people = Array.from({length: 4040}, (_, i) => [i === 0 ? pid : `person-${i}`, 'person', i === 0 ? '张小明' : `王同学 ${i}`, null, null, [], [], [], ['朋友'], ['wechat'], '一份很长的工作描述，验证省略与布局', 'Chicago', null, '2026-10-01', 123456, null, null, ['研究生同学群']]);
const posts = Array.from({length: 54217}, (_, i) => [`post-${i}`, pid, 'wechat', '2026-10-01', `周末去散步，见到了老朋友。工作、读书、旅行和生活记录 ${i}，一点日常想法，还有照片和最近的活动。`]);

test.use({ browserName: process.env.CRM_TEST_BROWSER === 'webkit' ? 'webkit' : 'chromium', channel: process.env.CRM_TEST_BROWSER === 'webkit' ? undefined : 'chrome', serviceWorkers: 'block', storageState: {cookies: [], origins: []} });
test.beforeEach(async ({context}) => {
  await context.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/directory') return route.fulfill({json: {columns, profiles: people, tags: [], companies: [], schools: []}});
    if (path === '/api/v1/directory/posts') return route.fulfill({json: {posts}});
    if (path.endsWith('/summary')) return route.fulfill({json: {profile: {id: pid, profile_type: 'person', display_name: '张小明', identities: [], significant_dates: [], linked_group_count: 0}, overview: {messages: null, post_stats: {}}}});
    if (path === `/api/v1/profiles/${pid}`) return route.fulfill({status: 503, json: {detail: 'delayed dossier'}});
    return route.fulfill({json: []});
  });
});

for (const width of [390, 768, 1024, 1440]) test(`contacts fit ${width}px`, async ({page}) => {
  await page.setViewportSize({width, height: 844});
  await page.goto(`${base}/profiles`);
  await expect(page.getByRole('table', {name: '联系人'})).toBeVisible();
  await expect(page.getByRole('row').filter({hasText: '王同学'}).first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  const overlapping = await page.evaluate(() => {
    const index = document.querySelector('[aria-label="按首字母跳转"]');
    if (!index) return false;
    const box = index.getBoundingClientRect();
    return [...document.querySelectorAll('[data-row] [role="cell"]')].some(cell => {
      const rect = cell.getBoundingClientRect();
      return rect.width > 0 && rect.right > box.left && rect.left < box.right;
    });
  });
  expect(overlapping).toBeFalsy();
  const index = page.getByRole('navigation', {name: '按首字母跳转'});
  const fitsViewport = () => index.evaluate(element => {
    const bottom = element.getBoundingClientRect().bottom;
    const reserve = innerWidth <= 720 ? parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--shell-tabbar')) : 0;
    return bottom <= innerHeight - reserve;
  });
  await expect.poll(fitsViewport).toBeTruthy();
  await page.evaluate(() => window.scrollTo(0, 500));
  await expect.poll(fitsViewport).toBeTruthy();
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect.poll(fitsViewport).toBeTruthy();
  await page.screenshot({path: `.local-validation/contacts-optimized-${width}.png`});
});

test('large cold search keeps input and closing responsive, then reuses the index', async ({page}) => {
  await page.goto(`${base}/profiles`);
  await expect(page.getByRole('row').filter({hasText: '王同学'}).first()).toBeVisible();
  await page.keyboard.press('ControlOrMeta+k');
  const input = page.getByRole('combobox', {name: '命令面板'});
  await expect(input).toBeVisible();
  await input.fill('周');
  const started = Date.now();
  await input.fill('周末');
  await expect(input).toHaveValue('周末', {timeout: 1500});
  await page.keyboard.press('Escape');
  await expect(page.locator("[cmdk-input]")).toHaveCount(0, {timeout: 1500});
  expect(Date.now() - started).toBeLessThan(2000);
  await page.keyboard.press('ControlOrMeta+k');
  await input.fill('周末');
  await expect(page.locator('[cmdk-group-heading]').filter({hasText: '动态'})).toBeVisible({timeout: 30000});
  await input.fill('张');
  await expect(page.locator('[cmdk-item]').filter({hasText: '张小明'})).toBeVisible();
  await input.fill('zxm');
  await expect(page.locator('[cmdk-item]').filter({hasText: '张小明'})).toBeVisible();
});

test('summary paints when the full dossier fails and exposes a retry', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  await page.goto(`${base}/profiles/${pid}`);
  await expect(page.getByRole('heading', {level: 1, name: '张小明'})).toBeVisible();
  await expect(page.getByText('资料未能加载')).toBeVisible({timeout: 15000});
  await expect(page.getByRole('button', {name: '重试', exact: true})).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});
