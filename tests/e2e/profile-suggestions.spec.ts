import {test, expect, type Page} from '@playwright/test';
import {base, noOverflow} from './session';

const profileId = '55555555-5555-4555-8555-555555555555';
test.use({serviceWorkers: 'block'});

const suggestions = () => [
  {fingerprint: 'a'.repeat(64), kind: 'residence', label: '常驻地', value: '杭州', date: null, quote: '下个月正式搬去杭州了',
    confidence: 'high', evidence: [{kind: 'direct', id: 'd1', conversation_id: 'conv-1', at: '2026-09-20T10:00:00Z', speaker: 'TA', text: '下个月正式搬去杭州了'}]},
  {fingerprint: 'b'.repeat(64), kind: 'meetup', label: '见面', value: '周六一起吃火锅', date: '2026-10-17', quote: '那周六火锅见',
    confidence: 'medium', evidence: [{kind: 'group', id: 'g1', conversation_id: 'conv-2', at: '2026-10-10T08:00:00Z', group: '读书会', text: '那周六火锅见'}]},
];

/** Mocks the profile and the AI endpoints; returns the decisions the page sent. */
async function mock(page: Page) {
  const decided = new Set<string>();
  const sent: {method: string; path: string; body: unknown}[] = [];
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/v1/ai/suggestions/')) {
      const fingerprint = url.pathname.split('/').pop()!;
      sent.push({method: request.method(), path: url.pathname, body: request.postDataJSON()});
      if (request.method() === 'DELETE') decided.delete(fingerprint);
      else decided.add(fingerprint);
      return route.fulfill({json: {ok: true}});
    }
    const pending = suggestions().filter((item) => !decided.has(item.fingerprint));
    if (url.pathname === `/api/v1/ai/profiles/${profileId}`) return route.fulfill({json: {enabled: true, queued: false,
      memory: {summary: '大学同学，做产品经理，常在读书会群里聊天。', facts: [], gaps: ['生日', '常驻地'], generated_at: new Date().toISOString()},
      weekly_updates: [],
      suggestions: pending}});
    if (url.pathname === '/api/v1/ai/suggestions') return route.fulfill({json: {enabled: true, total: 1, items: pending.length,
      kinds: Object.fromEntries(pending.map((item) => [item.kind, 1])),
      people: [{profile_id: profileId, name: '建议验证', avatar_media_id: null, generated_at: new Date().toISOString(), items: pending}]}});
    if (url.pathname === `/api/v1/profiles/${profileId}`) return route.fulfill({json: {
      id: profileId, profile_type: 'person', display_name: '建议验证', fields: [], identities: [], structured_facts: []}});
    if (url.pathname === '/api/v1/me') return route.fulfill({json: {id: 'test'}});
    return route.fulfill({status: 404, json: {detail: 'Not included in fixture'}});
  });
  return sent;
}

for (const width of [1440, 390]) test(`profile card accepts, edits and ignores suggestions at ${width}px`, async ({page}) => {
  await page.setViewportSize({width, height: 900});
  const sent = await mock(page);
  await page.goto(`${base}/profiles/${profileId}`);
  const card = page.locator('section').filter({has: page.getByRole('heading', {name: '人物 Memory'})});
  await expect(card.getByText('大学同学，做产品经理')).toBeVisible();
  await expect(card.getByText('还不知道：生日、常驻地')).toBeVisible();
  await expect(card.getByRole('link', {name: /^微信私聊/})).toHaveAttribute('href', /\/messages\?conversation=conv-1&month=2026-09/);
  await expect(card.getByRole('link', {name: /^群「读书会」/})).toBeVisible();
  expect(await page.evaluate(noOverflow)).toBe(true);

  const residence = card.locator('li').filter({hasText: '杭州'});
  await residence.getByRole('button', {name: '忽略'}).click();
  await expect(residence).toHaveCount(0);
  await page.getByRole('button', {name: '撤销'}).click();
  await expect(card.locator('li').filter({hasText: '杭州'})).toBeVisible();
  expect(sent.map((item) => item.method)).toEqual(['POST', 'DELETE']);

  const meetup = card.locator('li').filter({hasText: '火锅'});
  await meetup.getByRole('button', {name: '修改'}).click();
  await meetup.getByRole('textbox', {name: '见面'}).fill('周六和读书会吃火锅');
  await meetup.getByLabel('日期').fill('2026-10-18');
  await meetup.getByRole('button', {name: '保存'}).click();
  await expect(page.getByText('已写入见面')).toBeVisible();
  expect(sent.at(-1)!.body).toEqual({action: 'accept', value: '周六和读书会吃火锅', date: '2026-10-18'});
});

for (const width of [1440, 390]) test(`suggestions page groups by person at ${width}px`, async ({page}) => {
  await page.setViewportSize({width, height: 900});
  const sent = await mock(page);
  await page.goto(`${base}/suggestions`);
  await expect(page.getByRole('heading', {level: 1})).toHaveText(/^资料建议/);
  await expect(page.getByRole('link', {name: /建议验证/})).toHaveAttribute('href', `/profiles/${profileId}`);
  await expect(page.getByRole('group', {name: '按类型筛选'})).toBeVisible();
  expect(await page.evaluate(noOverflow)).toBe(true);
  await page.locator('li li').filter({hasText: '杭州'}).getByRole('button', {name: '接受'}).click();
  await expect(page.getByText('已写入常驻地')).toBeVisible();
  expect(sent).toEqual([{method: 'POST', path: `/api/v1/ai/suggestions/${profileId}/${'a'.repeat(64)}`, body: {action: 'accept'}}]);
});
