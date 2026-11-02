import { expect, test } from '@playwright/test';
import { base, noOverflow } from './session';

test.use({ storageState: { cookies: [], origins: [] }, serviceWorkers: 'block' });
const pid = '11111111-1111-4111-8111-111111111111';

for (const width of [1440, 390]) test(`add and stop LinkedIn collection from profile at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 });
  let targets: { account_id: string; external_id: string; profile_url: string }[] = [];
  let conflict = true;
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const profile = { id: pid, display_name: '测试联系人', profile_type: 'person', fields: [], identities: [], significant_dates: [] };
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (path.endsWith('/linkedin-targets') && method === 'PUT') {
      expect(route.request().postDataJSON()).toEqual({ account: 'https://www.linkedin.com/in/jane-doe' });
      if (conflict) {
        conflict = false;
        return route.fulfill({ status: 409, json: { detail: '此 LinkedIn 账号已关联其他人物，请先在来源账号中核对归属' } });
      }
      targets = [{ account_id: 'target-1', external_id: 'jane-doe', profile_url: 'https://www.linkedin.com/in/jane-doe' }];
      return route.fulfill({ json: targets[0] });
    }
    if (path.endsWith('/linkedin-targets/target-1') && method === 'DELETE') {
      targets = [];
      return route.fulfill({ status: 204 });
    }
    if (path.endsWith('/linkedin-targets')) return route.fulfill({ json: targets });
    if (path === `/api/v1/profiles/${pid}/summary`) return route.fulfill({ json: { profile, overview: {}, has_posts: false } });
    if (path === `/api/v1/profiles/${pid}`) return route.fulfill({ json: profile });
    if (path === '/api/v1/auth/csrf') return route.fulfill({ json: { csrf_token: 'test' } });
    if (path === '/api/v1/me') return route.fulfill({ json: { email: 'test@example.test' } });
    if (path.endsWith('/overview') || path.endsWith('/locations') || path.endsWith('/metrics/social')) return route.fulfill({ status: 404, json: {} });
    if (path === '/api/v1/source-accounts') return route.fulfill({ json: [] });
    return route.fulfill({ status: 404, json: {} });
  });
  await page.goto(`${base}/profiles/${pid}`);
  await expect(page.getByRole('heading', { level: 1 })).toContainText('测试联系人');
  if (width === 390) {
    await page.getByRole('button', { name: '更多操作', exact: true }).click();
    await page.getByRole('menuitem', { name: '管理来源账号' }).click();
  } else await page.getByRole('button', { name: '来源账号', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '来源账号', exact: true });
  await expect(dialog.getByRole('region', { name: 'LinkedIn 持续采集' })).toBeVisible();
  const input = dialog.getByRole('textbox', { name: /LinkedIn 账号/ });
  await input.fill('https://www.linkedin.com/in/jane-doe');
  await dialog.getByRole('button', { name: '添加并持续采集' }).click();
  await expect(dialog).toContainText('已关联其他人物');
  await expect(input).toHaveValue('https://www.linkedin.com/in/jane-doe');
  await dialog.getByRole('button', { name: '添加并持续采集' }).click();
  await expect(dialog.getByRole('link', { name: 'jane-doe' })).toHaveAttribute('href', 'https://www.linkedin.com/in/jane-doe');
  await expect(dialog.getByRole('status')).toContainText('已加入采集名单');
  expect(await page.evaluate(noOverflow)).toBeTruthy();
  await page.screenshot({ path: `.local-validation/profile-linkedin-${width}.png` });
  await dialog.getByRole('button', { name: '停止手动采集' }).click();
  await expect(dialog.getByRole('link', { name: 'jane-doe' })).toHaveCount(0);
  await expect(dialog.getByRole('status')).toContainText('已移出手动采集名单');
  expect(errors).toEqual([]);
});
