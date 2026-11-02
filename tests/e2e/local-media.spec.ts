import {test, expect} from '@playwright/test';
import {base, noOverflow} from './session';

const profileId = '11111111-1111-4111-8111-111111111111';
const assetId = '22222222-2222-4222-8222-222222222222';
const videoId = '33333333-3333-4333-8333-333333333333';
const external = 'https://media.licdn.com/test-not-cached.jpg';
const local = `/api/v1/media/${assetId}`;
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jL1sAAAAASUVORK5CYII=', 'base64');
test.use({serviceWorkers: 'block'});

for (const width of [1440, 390]) test(`provider images load only after local caching at ${width}px`, async ({page}) => {
  await page.setViewportSize({width, height: 1000});
  let cached = false;
  const outside: string[] = [];
  const reads: string[] = [];
  page.on('request', (request) => {
    if (!['image', 'media'].includes(request.resourceType())) return;
    (new URL(request.url()).origin === new URL(base).origin ? reads : outside).push(request.url());
  });
  await page.route('https://media.licdn.com/**', (route) => route.abort());
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    const media = [
      {...(cached ? {id: assetId} : {}), ready: cached, source_url: external, thumbnail_url: external, media_type: 'image/jpeg'},
      {...(cached ? {id: videoId, thumbnail_id: assetId} : {}), ready: cached, source_url: 'https://media.licdn.com/test-not-cached.mp4', media_type: 'video/mp4'},
    ];
    if (url.searchParams.get('provider') === 'instagram') media.reverse();
    if (url.pathname === `/api/v1/media/${videoId}`) return route.fulfill({status: 204});
    if (url.pathname === local) return route.fulfill({contentType: 'image/png', body: png});
    if (url.pathname === `/api/v1/profiles/${profileId}`) return route.fulfill({json: {
      id: profileId, profile_type: 'person', display_name: '缓存验证', avatar_url: external, cover_url: external,
      avatar_media_id: cached ? assetId : null, cover_media_id: cached ? assetId : null, fields: [], identities: [], structured_facts: [],
    }});
    if (url.pathname === '/api/v1/timeline') return route.fulfill({json: [{
      id: 'local-cache-event', title: '图片缓存验证', provider: 'linkedin', profile_id: profileId, profile_name: '缓存验证',
      occurred_at: '2026-01-02T03:04:05Z', media, cursor: 'one', summary: '验证缓存后展示',
    }]});
    if (url.pathname === '/api/v1/timeline/local-cache-event') return route.fulfill({json: {title: '图片缓存验证', media, content: {text: '验证缓存后展示'}, interactions: []}});
    if (url.pathname === '/api/v1/me') return route.fulfill({json: {id: 'test'}});
    return route.fulfill({status: 404, json: {detail: 'Not included in cache fixture'}});
  });

  await page.goto(`${base}/profiles/${profileId}?tab=timeline`);
  await expect(page.getByRole('heading', {level: 1})).toHaveText(/^缓存验证/);
  const avatar = page.getByRole('button', {name: '查看头像原图', exact: true}).locator('img');
  const cover = page.getByRole('button', {name: '查看朋友圈封面原图', exact: true}).locator('img');
  await expect(avatar).toHaveCount(0);
  await expect(cover).toHaveCount(0);

  // Uncached provider media is left out of the card; the card still opens its detail in place.
  const feed = page.locator('[data-tab-panel="timeline"]');
  const card = feed.locator('article').filter({hasText: '验证缓存后展示'});
  await expect(card).toBeVisible();
  await expect(card.locator('button[data-thumb]')).toHaveCount(0);
  await card.getByRole('button', {name: '验证缓存后展示'}).click();
  const detail = page.getByRole('dialog', {name: '动态详情'});
  await expect(detail).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/profiles/${profileId}\\?tab=timeline$`));
  await expect(detail.getByRole('link', {name: /^查看原图/})).toHaveCount(0);
  await expect(detail.locator('video')).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(detail).toHaveCount(0);

  cached = true;
  await page.reload();
  await expect(avatar).toHaveAttribute('src', new RegExp(`^${local}`));
  await expect(cover).toHaveAttribute('src', new RegExp(`^${local}`));
  const thumb = card.locator('button[data-thumb]');
  const image = thumb.locator('img');
  await image.scrollIntoViewIfNeeded();
  await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
  await thumb.click();
  await expect(detail.getByRole('link', {name: '查看原图 1', exact: true})).toHaveAttribute('href', local);
  await expect(detail.locator('video')).toHaveAttribute('src', `/api/v1/media/${videoId}`);
  await expect(detail.locator('video')).toHaveAttribute('poster', local);
  expect(await page.evaluate(noOverflow)).toBe(true);
  await page.keyboard.press('Escape');
  await expect(detail).toHaveCount(0);

  await page.getByRole('button', {name: '查看头像原图', exact: true}).click();
  await expect(page.locator('dialog[open] img[data-zoom-target]')).toHaveAttribute('src', local);
  await expect(page.getByRole('link', {name: '打开原图', exact: true})).toHaveAttribute('href', local);
  await page.getByRole('button', {name: '关闭原图', exact: true}).click();

  await feed.getByRole('group', {name: '动态来源'}).getByRole('button', {name: 'Instagram', exact: true}).click();
  await expect(card.locator('button[data-thumb] img')).toHaveAttribute('src', new RegExp(`^${local}`));
  expect(reads.some((url) => url.includes(local))).toBeTruthy();
  expect(outside).toEqual([]);
});

const tall = (label: string) => `<svg xmlns="http://www.w3.org/2000/svg" width="600" height="1800"><rect width="600" height="1800" fill="#486"/><text x="40" y="900" font-size="120">${label}</text></svg>`;

for (const count of [1, 2]) test(`detail shows each of ${count} tall photos whole`, async ({page}) => {
  await page.setViewportSize({width: 1440, height: 900});
  const ids = ['44444444-4444-4444-8444-444444444441', '44444444-4444-4444-8444-444444444442'].slice(0, count);
  const media = ids.map((id) => ({id, thumbnail_id: id, ready: true, media_type: 'image/svg+xml', width: 600, height: 1800}));
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    const asset = ids.find((id) => url.pathname === `/api/v1/media/${id}`);
    if (asset) return route.fulfill({contentType: 'image/svg+xml', body: tall(asset.slice(-1))});
    if (url.pathname === `/api/v1/profiles/${profileId}`) return route.fulfill({json: {
      id: profileId, profile_type: 'person', display_name: '长图验证', fields: [], identities: [], structured_facts: []}});
    if (url.pathname === '/api/v1/timeline') return route.fulfill({json: [{id: 'tall', title: '长图', provider: 'instagram',
      profile_id: profileId, profile_name: '长图验证', occurred_at: '2026-01-02T03:04:05Z', media, summary: '一张很高的图'}]});
    if (url.pathname === '/api/v1/timeline/tall') return route.fulfill({json: {title: '长图', media, content: {text: '一张很高的图'}, interactions: []}});
    if (url.pathname === '/api/v1/me') return route.fulfill({json: {id: 'test'}});
    return route.fulfill({status: 404, json: {detail: 'Not included in fixture'}});
  });
  await page.goto(`${base}/profiles/${profileId}?tab=timeline`);
  await page.locator('[data-tab-panel="timeline"] article button[data-thumb]').first().click();
  const detail = page.getByRole('dialog', {name: '动态详情'});
  const photos = detail.getByRole('link', {name: /^查看原图/});
  await expect(photos).toHaveCount(count);
  await expect.poll(() => photos.first().locator('img').evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
  const fit = await detail.evaluate((dialog) => {
    const panel = dialog.querySelector('img')!.closest('div')!;
    const box = panel.getBoundingClientRect();
    return {scrolls: panel.scrollHeight > panel.clientHeight + 1,
      images: [...panel.querySelectorAll('img')].map((img) => img.getBoundingClientRect().height <= box.height)};
  });
  expect(fit.images).toEqual(ids.map(() => true));
  expect(fit.scrolls).toBe(count > 1);
});
