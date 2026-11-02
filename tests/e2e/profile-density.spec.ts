import {test, expect} from '@playwright/test';
import {base, storageState} from './session';

// A contact with years of chat, calls, posts and LinkedIn history.
const busy = process.env.CRM_BUSY_PROFILE || '3464f853-1002-420a-8941-dd012fe80dc1';
test.use({serviceWorkers: 'block', storageState});

for (const width of [1440, 390]) test(`profile renders a dense first paint at ${width}px`, async ({page}) => {
  await page.setViewportSize({width, height: 1000});
  const requests: string[] = [];
  page.on('request', (r) => { if (r.url().includes('/api/v1/')) requests.push(new URL(r.url()).pathname); });
  await page.goto(`${base}/profiles/${busy}`);
  await expect(page.getByRole('heading', {level: 1})).toBeVisible({timeout: 20000});
  const pulse = page.getByRole('region', {name: '往来概况'});
  await expect(pulse).toBeInViewport();
  await expect(pulse.getByRole('group', {name: '关系丝带：每月私聊'})).toBeVisible();
  await expect(page.getByRole('navigation', {name: '资料分区'})).toBeInViewport();
  await expect(page.locator('[data-tab-panel="overview"]').getByRole('region', {name: '近况'})).toBeVisible();
  if (width === 1440) await expect(page.getByRole('region', {name: '职业与教育'})).toBeVisible();

  const text = await page.locator('main').innerText();
  for (const noise of ['0 语音', '暂无已确认的关系边', '没有关联的微信群', '地址未填写', '待补充来源', '查看详情', '图片尚未缓存']) expect(text).not.toContain(noise);
  // Chat logs and field history stay behind their tabs instead of loading with the overview.
  const calls = requests.filter((path) => path.startsWith(`/api/v1/profiles/${busy}`));
  expect(calls).toContain(`/api/v1/profiles/${busy}/overview`);
  expect(calls.some((path) => /\/(messages|history|structured-history)$/.test(path))).toBeFalsy();
  await page.screenshot({path: `.local-validation/profile-dossier-${width}.png`});
});

test('tabs keep deep sections one click away', async ({page}) => {
  await page.goto(`${base}/profiles/${busy}`);
  const tabs = page.getByRole('navigation', {name: '资料分区'});
  await tabs.getByRole('button', {name: '动态与照片'}).click();
  await expect(page.locator('[data-tab-panel="timeline"] article').first()).toBeVisible();
  await expect(tabs.getByRole('button', {name: '动态与照片'})).toHaveAttribute('aria-current', /.+/);
  await tabs.getByRole('button', {name: '聊天'}).click();
  await expect(page.getByRole('region', {name: '聊天记录'})).toBeVisible();
  await tabs.getByRole('button', {name: '历史'}).click();
  await expect(page.locator('[data-tab-panel="history"]').getByRole('heading', {name: '字段变更记录'})).toBeVisible();
  await expect(page).toHaveURL(/tab=history/);
});
