import {test, expect} from '@playwright/test';
import {base, noOverflow} from './session';

const profileId = '77777777-7777-4777-8777-777777777777';
test.use({serviceWorkers: 'block'});

const longNote = [
  '## 见面要点', '', '聊了**换工作**的打算，', '下个月去杭州。', '',
  '- 喜欢徒步', '- 不吃香菜', '- [x] 寄了生日礼物', '- [ ] 介绍给老王', '',
  '> 她说最近压力很大', '', '<b>raw html</b>', '', '![合照](https://example.invalid/photo.jpg)', '',
  '[她的博客](https://example.invalid/blog)', '',
  ...Array.from({length: 30}, (_, index) => `第 ${index + 1} 段：后来又聊了很多别的事情。\n`),
].join('\n');

const records = [
  {id: 'r1', activity_type: 'note', occurred_at: '2026-10-10T12:00:00Z', title: '咖啡', body: longNote, source_type: 'manual', state: 'open'},
  {id: 'r2', activity_type: 'wechat_call', occurred_at: '2026-10-09T12:00:00Z', title: '微信通话', source_type: 'wechat', state: 'open',
    body: JSON.stringify({duration_seconds: 125, direction: 'outgoing', text: '**not markdown**'})},
];

for (const width of [1440, 390]) test(`records render written notes as Markdown at ${width}px`, async ({page}) => {
  await page.setViewportSize({width, height: 900});
  const saved: unknown[] = [];
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/v1/activities') return route.fulfill({json: records});
    if (url.pathname === '/api/v1/activities/r1' && route.request().method() === 'PUT') {
      saved.push(route.request().postDataJSON());
      return route.fulfill({json: records[0]});
    }
    if (url.pathname === `/api/v1/profiles/${profileId}`) return route.fulfill({json: {
      id: profileId, profile_type: 'person', display_name: '笔记验证', identities: [], structured_facts: [],
      fields: [{field_key: 'biography', value: '大学同学\n\n- 爱好：**摄影**', source_type: 'manual'}]}});
    if (url.pathname === '/api/v1/me') return route.fulfill({json: {id: 'test'}});
    return route.fulfill({status: 404, json: {detail: 'Not included in fixture'}});
  });
  await page.goto(`${base}/profiles/${profileId}?tab=records`);
  const note = page.locator('[data-tab-panel="records"] li[data-kind="note"]');
  await expect(note.getByRole('heading', {name: '见面要点'})).toBeVisible();
  await expect(note.locator('strong', {hasText: '换工作'})).toBeVisible();
  await expect(note.getByText('下个月去杭州。')).toBeVisible();
  await expect(note.getByRole('listitem').filter({hasText: '不吃香菜'})).toBeVisible();
  await expect(note.getByRole('checkbox')).toHaveCount(2);
  await expect(note.locator('blockquote')).toHaveText('她说最近压力很大');
  await expect(note.locator('b')).toHaveCount(0);
  await expect(note.locator('img')).toHaveCount(0);
  await expect(note.getByRole('link', {name: '合照'})).toHaveAttribute('href', 'https://example.invalid/photo.jpg');
  await expect(note.getByRole('link', {name: '她的博客'})).toHaveAttribute('target', '_blank');

  const expand = note.getByRole('button', {name: '展开全文'});
  await expect(expand).toBeVisible();
  await expect(note.getByText('第 30 段')).not.toBeInViewport();
  await expand.click();
  await note.getByText('第 30 段').scrollIntoViewIfNeeded();
  await expect(note.getByText('第 30 段')).toBeInViewport();
  await expect(note.getByRole('button', {name: '收起'})).toBeVisible();

  const call = page.locator('[data-tab-panel="records"] li[data-kind="wechat_call"]');
  await expect(call.locator('p').first()).toContainText('**not markdown**');
  await expect(call.locator('strong')).toHaveCount(0);
  expect(await page.evaluate(noOverflow)).toBe(true);

  await note.getByRole('button', {name: '编辑'}).click();
  const form = page.locator('form');
  await form.getByRole('textbox', {name: '内容'}).fill('# 新标题\n- 一\n- 二');
  await form.getByRole('button', {name: '预览'}).click();
  await expect(form.getByRole('region', {name: '内容'}).getByRole('heading', {name: '新标题'})).toBeVisible();
  await form.getByRole('button', {name: '保存记录'}).click();
  await expect.poll(() => saved.length).toBe(1);
  expect((saved[0] as {body: string}).body).toBe('# 新标题\n- 一\n- 二');

  await page.goto(`${base}/profiles/${profileId}`);
  const notes = page.locator('section').filter({has: page.getByRole('heading', {name: '笔记与资料'})});
  await expect(notes.locator('strong', {hasText: '摄影'})).toBeVisible();
  expect(await page.evaluate(noOverflow)).toBe(true);
});
