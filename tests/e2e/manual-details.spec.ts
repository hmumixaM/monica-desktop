import {test, expect, type Page} from '@playwright/test';
import {base, noOverflow} from './session';

const profileId = '88888888-8888-4888-8888-888888888888';
const pdf = {id: '99999999-9999-4999-8999-999999999999', media_type: 'application/pdf', byte_length: 245760, file_name: '简历 2026.pdf'};
test.use({serviceWorkers: 'block'});

const at = '2026-10-01T00:00:00Z';
const fields = [
  {field_key: 'monica.custom_fields', current_source_type: 'monica', updated_at: at,
    value: {Phone: ['5550100123'], 籍贯: ['祖籍杭州'], 'YouTube Channel': ['https://youtube.example/@a'], 微信号: ['wx-hidden']}},
  {field_key: '籍贯', current_source_type: 'manual', updated_at: '2026-10-05T00:00:00Z', value: '祖籍杭州，老家绍兴'},
  {field_key: 'monica.phone', current_source_type: 'manual', updated_at: '2026-10-15T00:00:00Z', value: '5550100123'},
  {field_key: 'youtube channel ', current_source_type: 'manual', updated_at: at, value: 'https://youtube.example/@a'},
  {field_key: 'Linkedin', current_source_type: 'manual', updated_at: at, value: 'https://linkedin.example/in/a'},
  {field_key: 'wechat.alias', current_source_type: 'wechat', updated_at: at, value: 'wx'},
];

async function fixture(page: Page) {
  const calls: {method: string; path: string; body: unknown}[] = [];
  const photos: unknown[] = [];
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() !== 'GET') calls.push({method: request.method(), path: url.pathname, body: request.postData()?.startsWith('{') ? request.postDataJSON() : null});
    if (url.pathname === `/api/v1/profiles/${profileId}`) return route.fulfill({json: {
      id: profileId, profile_type: 'person', display_name: '资料验证', identities: [], structured_facts: [], fields}});
    if (url.pathname === `/api/v1/profiles/${profileId}/details`) return route.fulfill(request.method() === 'DELETE' ? {status: 204} : {json: {}});
    if (url.pathname === `/api/v1/profiles/${profileId}/photos`) {
      if (request.method() === 'POST') photos.push(pdf);
      return route.fulfill({json: request.method() === 'POST' ? {status: 'attached'} : photos});
    }
    if (url.pathname === '/api/v1/media' && request.method() === 'POST') return route.fulfill({json: pdf});
    if (url.pathname === '/api/v1/activities') return route.fulfill(request.method() === 'POST' ? {json: {id: 'new'}} : {json: []});
    if (url.pathname === '/api/v1/me') return route.fulfill({json: {id: 'test'}});
    return route.fulfill({status: 404, json: {detail: 'Not included in fixture'}});
  });
  return calls;
}

const notes = (page: Page) => page.locator('section').filter({has: page.getByRole('heading', {name: '笔记与资料'})});
const row = (page: Page, label: string) => notes(page).locator('dl > div').filter({has: page.getByRole('term').filter({hasText: new RegExp(`^${label}$`)})});

for (const width of [1440, 390]) test(`hand-kept details edit in place at ${width}px`, async ({page}) => {
  await page.setViewportSize({width, height: 900});
  const calls = await fixture(page);
  await page.goto(`${base}/profiles/${profileId}`);

  await expect(notes(page).getByRole('term')).toHaveText(['电话', '籍贯', 'Linkedin', 'youtube channel']);
  await expect(row(page, '电话').getByRole('definition')).toHaveText('5550100123');
  await expect(row(page, '籍贯').getByText('另有 1 个不同的值')).toBeVisible();
  await expect(notes(page).getByText('wx-hidden')).toHaveCount(0);

  await row(page, '籍贯').getByRole('button', {name: '编辑籍贯'}).click();
  const form = notes(page).getByRole('form', {name: '编辑籍贯'});
  await expect(form.getByRole('textbox', {name: '籍贯'})).toHaveValue('祖籍杭州，老家绍兴');
  await expect(form.getByText('下面 1 个旧值移入历史')).toBeVisible();
  await form.getByRole('textbox', {name: '籍贯'}).fill('祖籍杭州，老家浙江绍兴');
  await form.getByRole('textbox', {name: '籍贯'}).press('Enter');
  await expect.poll(() => calls.length).toBe(1);
  expect(calls[0]).toEqual({method: 'PUT', path: `/api/v1/profiles/${profileId}/details`, body: {
    field_key: '籍贯', value: '祖籍杭州，老家浙江绍兴', replaces: [{field_key: '籍贯'}, {field_key: 'monica.custom_fields', item: '籍贯'}]}});

  await row(page, '电话').hover();
  await row(page, '电话').getByRole('button', {name: '删除电话'}).click();
  await row(page, '电话').getByRole('alert').getByRole('button', {name: '删除'}).click();
  await expect.poll(() => calls.length).toBe(2);
  expect(calls[1]).toEqual({method: 'DELETE', path: `/api/v1/profiles/${profileId}/details`,
    body: {refs: [{field_key: 'monica.phone'}, {field_key: 'monica.custom_fields', item: 'Phone'}]}});

  await notes(page).getByRole('button', {name: '添加'}).click();
  const add = notes(page).getByRole('form', {name: '添加资料'});
  await add.getByRole('button', {name: '饮食偏好'}).click();
  await expect(add.getByRole('combobox', {name: '名称'})).toHaveValue('饮食偏好');
  await add.getByRole('combobox', {name: '名称'}).fill('YouTube Channel');
  await expect(add.getByText('已有「youtube channel」，保存后合并为一条。')).toBeVisible();
  await add.getByRole('textbox', {name: 'YouTube Channel'}).fill('https://youtube.example/@b');
  await add.getByRole('button', {name: '保存'}).click();
  await expect.poll(() => calls.length).toBe(3);
  expect(calls[2].body).toEqual({field_key: 'youtube channel ', value: 'https://youtube.example/@b',
    replaces: [{field_key: 'youtube channel '}, {field_key: 'monica.custom_fields', item: 'YouTube Channel'}]});
  expect(await page.evaluate(noOverflow)).toBe(true);
});

test('photos and records take any file', async ({page}) => {
  const calls = await fixture(page);
  await page.goto(`${base}/profiles/${profileId}`);
  const files = page.locator('section').filter({has: page.getByRole('heading', {name: '照片与文件'})});
  await files.getByRole('button', {name: '上传'}).click();
  await files.locator('input[type=file]').setInputFiles({name: '简历 2026.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.7')});
  await expect(files.getByRole('status')).toHaveText('上传完成，请保存。');
  await files.getByRole('button', {name: '保存'}).click();
  await expect(files.getByRole('button', {name: '上传'})).toBeVisible();
  expect(calls.find((call) => call.path.endsWith('/photos'))?.body).toEqual({media_ids: [pdf.id]});
  await expect(files.getByRole('link', {name: /简历 2026\.pdf/}).first()).toHaveAttribute('href', `/api/v1/media/${pdf.id}`);
  await expect(files.getByText('240.0 KB')).toBeVisible();

  await page.goto(`${base}/profiles/${profileId}?tab=records`);
  await page.getByRole('button', {name: '添加记录'}).click();
  const form = page.locator('form');
  await form.getByRole('textbox', {name: '标题'}).fill('签合同');
  await form.locator('input[type=file]').setInputFiles({name: '简历 2026.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.7')});
  await expect(form.getByRole('link', {name: /简历 2026\.pdf/}).first()).toBeVisible();
  await form.getByRole('button', {name: '保存记录'}).click();
  await expect.poll(() => (calls.find((call) => call.path === '/api/v1/activities')?.body as {media_ids?: string[]})?.media_ids).toEqual([pdf.id]);
});
