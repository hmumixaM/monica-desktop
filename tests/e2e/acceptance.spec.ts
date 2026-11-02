import {test, expect} from '@playwright/test';
import {base, noOverflow, storageState} from './session';

const target = process.env.CRM_TARGET_PROFILE || 'f977df28-a35b-49a8-b7ab-8825eeafd960';
const group = process.env.CRM_GROUP_PROFILE || '96c7c93f-3b71-4bf4-92fb-d21ed1b6bdf9';
const linkedin = process.env.CRM_LINKEDIN_PROFILE || 'a1fc2a78-6323-444b-8689-f7f44c45c9bc';
test.use({storageState});

for (const viewport of [{width: 1440, height: 1000}, {width: 390, height: 844}]) test.describe(`${viewport.width}px`, () => {
  test.use({viewport});

  test('real profile, post detail, conversation search and graph', async ({page}) => {
    const requests: string[] = [];
    const errors: string[] = [];
    page.on('request', (r) => { if (r.url().includes('/api/')) requests.push(r.url()); });
    page.on('pageerror', (e) => errors.push(e.message));
    await page.goto(`${base}/profiles/${target}`);
    await expect(page.getByRole('heading', {level: 1})).toHaveText(/^刘鹤尧/, {timeout: 20000});
    const avatar = page.getByRole('button', {name: '查看头像原图'}).locator('img');
    await expect.poll(() => avatar.evaluate((e: HTMLImageElement) => e.naturalWidth)).toBeGreaterThan(0);
    // The overview never loads the chat log or field history.
    expect(requests.some((r) => /\/(messages|history)(\?|$)/.test(new URL(r).pathname + '?'))).toBeFalsy();
    expect(await page.evaluate(noOverflow)).toBeTruthy();

    await page.getByRole('navigation', {name: '资料分区'}).getByRole('button', {name: '动态与照片'}).click();
    const photo = page.locator('[data-tab-panel="timeline"] button[data-thumb]').first();
    await expect(photo.locator('img')).toBeVisible();
    await photo.click();
    const detail = page.getByRole('dialog', {name: '动态详情'});
    await expect(detail).toBeVisible();
    await expect(detail.getByRole('heading', {name: '赞与评论'})).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(detail).toHaveCount(0);

    await page.getByRole('navigation', {name: '资料分区'}).getByRole('button', {name: '聊天', exact: true}).click();
    const chat = page.getByRole('region', {name: '聊天记录'});
    await chat.getByRole('button', {name: '在会话中搜索'}).click();
    await page.getByLabel('会话内搜索').fill('上海');
    await page.getByRole('button', {name: '搜索', exact: true}).click();
    await expect(chat.getByText('包含「')).toBeVisible();
    await expect(chat.locator('article').first()).toBeVisible();

    await page.goto(`${base}/relationships?view=graph&focus=${target}`);
    await expect(page.locator('[data-graph-status]')).toContainText('已显示', {timeout: 30000});
    await page.getByLabel('查找节点').fill('刘鹤尧');
    await page.getByRole('button', {name: '定位', exact: true}).click();
    const open = page.locator('[data-graph-details] a');
    await expect(open).toHaveAttribute('href', `/profiles/${target}`);
    await open.click();
    await expect(page).toHaveURL(`${base}/profiles/${target}`);
    expect(errors).toEqual([]);
  });

  test('group members and LinkedIn images served from Monica', async ({page}) => {
    await page.goto(`${base}/profiles/${group}`);
    await expect(page.getByRole('heading', {name: '群成员'})).toBeVisible({timeout: 20000});
    const members = await (await page.request.get(`${base}/api/v1/profiles/${group}`)).json();
    expect(members.profile_type).toBe('group');
    expect(members.group_members.every((m: {profile_type: string; person_profile_id: string}) =>
      m.profile_type === 'person' && m.person_profile_id !== group)).toBeTruthy();

    await page.goto(`${base}/profiles/${linkedin}?tab=timeline`);
    const feed = page.waitForResponse((r) => r.url().includes('/api/v1/timeline?') && r.url().includes('provider=linkedin'));
    await page.getByRole('group', {name: '动态来源'}).getByRole('button', {name: 'LinkedIn'}).click();
    await feed;
    const image = page.locator('[data-tab-panel="timeline"] button[data-thumb] img').first();
    await image.scrollIntoViewIfNeeded();
    await expect.poll(() => image.evaluate((e: HTMLImageElement) => e.naturalWidth)).toBeGreaterThan(0);
    expect(await image.getAttribute('src')).toMatch(/^\/api\/v1\/media\//);
  });
});
