import { expect, test } from '@playwright/test';
import { base, noOverflow } from './session';

test.use({ serviceWorkers: 'block', reducedMotion: 'reduce' });

for (const width of [390, 1440]) test(`comment pictures render locally without joining post gallery at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 800 });
  const mediaId = '11111111-1111-4111-8111-111111111111';
  const post = { id: 'post', provider: 'wechat', title: '朋友圈', profile_name: '评论图片测试',
    summary: '一条纯文字朋友圈', cursor: 'one', media: [], occurred_at: '2026-10-18T04:00:00Z' };
  const externalRequests: string[] = [];
  page.on('request', request => { if (request.url().includes('qpic.cn')) externalRequests.push(request.url()); });
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === `/api/v1/media/${mediaId}`) return route.fulfill({ contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="1178" height="180"><rect width="1178" height="180" fill="#dce8e0"/><text x="30" y="100" font-size="40">Comment picture</text></svg>' });
    if (path === '/api/v1/timeline') return route.fulfill({ json: [post] });
    if (path === '/api/v1/timeline/post') return route.fulfill({ json: {
      content: { text: post.summary }, media: [], interactions: [
        { id: 'reply', interaction_type: 'reply', author_name: '作者', content: { text: '精选答案' },
          media: [{ id: mediaId, media_type: 'image/jpeg', width: 1178, height: 180 }] },
        { id: 'pending', interaction_type: 'comment', author_name: '朋友',
          media: [{ source_url: 'http://szmmsns.qpic.cn/mmcomment/pending/0', media_type: 'image' }] },
      ],
    } });
    return route.fulfill({ json: {} });
  });
  await page.goto(`${base}/timeline?provider=wechat`);
  await page.getByRole('button', { name: post.summary, exact: true }).click();
  const detail = page.getByRole('dialog', { name: '动态详情' });
  await expect(detail.getByText('精选答案')).toBeVisible();
  const image = detail.getByRole('img', { name: '评论图片 1' });
  await expect(image).toBeVisible();
  await expect(image).toHaveAttribute('src', `/api/v1/media/${mediaId}`);
  await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBeTruthy();
  await expect(detail.getByRole('link', { name: '查看评论原图 1' })).toHaveAttribute('href', `/api/v1/media/${mediaId}`);
  await expect(detail.getByRole('link', { name: '查看原图 1', exact: true })).toHaveCount(0);
  await expect(detail.getByText('评论图片待下载')).toBeVisible();
  expect(externalRequests).toEqual([]);
  expect(await page.evaluate(noOverflow)).toBeTruthy();
  expect(await image.evaluate(img => img.getBoundingClientRect().right <= innerWidth)).toBeTruthy();
});
