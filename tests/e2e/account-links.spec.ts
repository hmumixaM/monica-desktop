import {test, expect, type Page} from '@playwright/test';
import {base, noOverflow, storageState} from './session';

const vincent = 'c8795960-e80a-48ff-8e5c-6f65d69aae6b';
const handle = 'han-bao-94344a21a';
test.use({storageState});

test.afterEach(async ({page}) => {
  const request = page.request;
  const current = await (await request.get(`${base}/api/v1/source-accounts?provider=linkedin&q=${handle}`)).json();
  const row = current.find((entry: any) => entry.external_id === handle);
  if (row?.profile_id === vincent) return;
  const csrf = await (await request.get(`${base}/api/v1/auth/csrf`)).json();
  const restored = await request.put(`${base}/api/v1/source-accounts/${row.id}/link`, {
    headers: {'X-CSRF-Token': csrf.csrf_token}, data: {profile_id: vincent, expected_profile_id: row.profile_id},
  });
  expect(restored.ok()).toBe(true);
});

async function openAccounts(page: Page, width: number) {
  if (width > 700) await page.getByRole('button', {name: '来源账号', exact: true}).click();
  else {
    await page.getByRole('button', {name: '更多操作'}).click();
    await page.getByRole('menuitem', {name: '管理来源账号'}).click();
  }
  return page.getByRole('dialog', {name: '来源账号'});
}

for (const width of [1440, 390]) test(`source account unlink and relink at ${width}px`, async ({page}) => {
  await page.setViewportSize({width, height: 950});
  await page.goto(`${base}/profiles/${vincent}`);
  const career = page.getByRole('region', {name: '职业与教育'});
  await expect(career).toContainText('Carnegie Mellon', {timeout: 20000});
  const dialog = await openAccounts(page, width);
  const account = dialog.locator('li').filter({hasText: handle});
  await expect(account).toHaveCount(1);
  const initial = await (await page.request.get(`${base}/api/v1/source-accounts?profile_id=${vincent}`)).json();
  const source = initial.find((row: any) => row.provider === 'linkedin' && row.external_id === handle);
  expect(source.ready).toBe(true);

  await account.getByRole('button', {name: '解除关联'}).click();
  await expect(dialog).toContainText('手工资料和记录会保留');
  const unlinked = page.waitForResponse((r) => r.request().method() === 'PUT' && r.url().includes(`/source-accounts/${source.id}/link`));
  await dialog.getByRole('button', {name: '确认', exact: true}).click();
  expect((await unlinked).ok()).toBe(true);
  await expect(account).toHaveCount(0);
  const profile = await (await page.request.get(`${base}/api/v1/profiles/${vincent}`)).json();
  expect(profile.identities.filter((row: any) => row.provider === 'linkedin')).toEqual([]);
  expect(profile.presentation.sources.linkedin).toEqual({});

  await dialog.getByLabel('平台').selectOption('linkedin');
  await dialog.getByLabel('寻找已有账号').fill(handle);
  const result = dialog.locator('li').filter({hasText: handle});
  await result.getByRole('button', {name: '关联', exact: true}).click();
  const linked = page.waitForResponse((r) => r.request().method() === 'PUT' && r.url().includes(`/source-accounts/${source.id}/link`));
  await dialog.getByRole('button', {name: '确认', exact: true}).click();
  expect((await linked).ok()).toBe(true);
  await expect(dialog.getByRole('button', {name: '解除关联'}).last()).toBeVisible();
  await page.screenshot({path: `.local-validation/account-links-${width}.png`});
  expect(await page.evaluate(noOverflow)).toBe(true);

  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(career).toContainText('Carnegie Mellon');
});
