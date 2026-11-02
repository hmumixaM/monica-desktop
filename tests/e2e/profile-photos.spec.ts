import { expect, test } from '@playwright/test';
import { base, noOverflow } from './session';

const person = '11111111-1111-4111-8111-111111111111';
const other = '22222222-2222-4222-8222-222222222222';
const mid = (i: number) => `33333333-3333-4333-8333-${String(i).padStart(12, '0')}`;
const fid = (i: number) => `44444444-4444-4444-8444-${String(i).padStart(12, '0')}`;
const drawing = '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="360"><rect width="480" height="360" fill="#758b92"/><circle cx="170" cy="100" r="45" fill="#d8d3bc"/><path d="M80 300 Q100 150 170 150 Q240 150 260 300" fill="#263b48"/></svg>';
test.use({ serviceWorkers: 'block', reducedMotion: 'reduce' });

for (const width of [1440, 390]) test(`profile photo browsing and feedback at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  const requests: URL[] = [];
  const decisions = new Map<string, { status: string; owner: string | null }>();
  let lastDecision: { face_ids: string[]; profile_id: string } | null = null;
  const current = (id: string) => decisions.get(id) ?? { status: 'pending', owner: id === fid(202) ? other : person };
  const revision = () => JSON.stringify([...decisions]);
  let failSave = true, failNext = true;
  let nextCalls = 0;
  let releasePaging!: () => void;
  const pagingGate = new Promise<void>((resolve) => { releasePaging = resolve; });
  const record = (i: number) => {
    const oneFace = (index: number, box: number[], score: number) => {
      const value = current(fid(index));
      return { id: fid(index), media_id: mid(i), box, status: value.status, score,
        profile_id: value.owner, name: value.owner === other ? '另一个联系人' : '相册测试' };
    };
    const faces = [oneFace(i, [.25, .15, .22, .3], .82),
      ...(i === 2 ? [oneFace(102, [.6, .2, .2, .3], .61), oneFace(202, [.05, .6, .1, .2], .42)] : [])];
    return { media_id: mid(i), occurred_at: '2026-10-15T10:00:00Z', score: .82,
      pending: faces.some((face) => face.profile_id === person && face.status === 'pending'),
      confirmed: faces.some((face) => face.profile_id === person && face.status === 'confirmed'), width: 480, height: 360, faces,
      sources: [{ entity_type: 'social_story', entity_id: mid(200), provider: 'instagram', occurred_at: '2026-10-15T10:00:00Z',
        author: '照片发布者', conversation_type: null, conversation_name: null, href: `/timeline?event=${mid(200)}` }],
    };
  };
  const counts = () => {
    const records = Array.from({ length: 26 }, (_, i) => record(i + 1));
    return { all: records.filter((row) => row.pending || row.confirmed).length,
      pending: records.filter((row) => row.pending).length, confirmed: records.filter((row) => row.confirmed).length };
  };
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    if (url.pathname === `/api/v1/profiles/${person}`) return route.fulfill({ json: {
      id: person, profile_type: 'person', display_name: '相册测试', avatar_media_id: null, cover_media_id: null,
      fields: [], identities: [], structured_facts: [], tags: [],
    } });
    if (url.pathname === '/api/v1/profiles') return route.fulfill({ json: [{ id: other, display_name: '另一个联系人', avatar_media_id: null }] });
    if (url.pathname.endsWith('/photos/status')) return route.fulfill({ json: { counts: counts(), revision: revision() } });
    if (url.pathname === `/api/v1/faces/people/${person}/photos`) {
      if (url.searchParams.has('cursor')) {
        nextCalls++;
        await pagingGate;
        if (failNext) { failNext = false; return route.fulfill({ status: 503, json: { detail: 'Temporary failure' } }); }
      }
      const start = url.searchParams.has('cursor') ? 25 : 1;
      let items = Array.from({ length: start === 25 ? 2 : 24 }, (_, i) => record(start + i));
      if (url.searchParams.get('state') === 'confirmed') items = items.filter((row) => row.confirmed);
      if (url.searchParams.get('state') === 'pending') items = items.filter((r) => r.pending);
      return route.fulfill({ json: { items, counts: counts(), revision: revision(),
        next_cursor: start === 1 && url.searchParams.get('state') !== 'confirmed'
          && (url.searchParams.get('state') !== 'pending' || counts().pending > 24) ? 'page-2' : null } });
    }
    if (url.pathname === '/api/v1/faces/decide') {
      if (failSave) { failSave = false; return route.fulfill({ status: 500, json: { detail: 'Temporary save failure' } }); }
      const body = route.request().postDataJSON();
      lastDecision = body;
      const previous = body.face_ids.map((id: string) => {
        const value = current(id);
        return { id, status: value.status, profile_id: value.status === 'confirmed' ? value.owner : null,
          suggested_profile_id: value.owner, rejected_profile_id: null, decided_at: null };
      });
      for (const id of body.face_ids) decisions.set(id, {
        status: body.action === 'confirm' ? 'confirmed' : body.action === 'ignore' ? 'ignored' : 'pending',
        owner: body.action === 'confirm' ? body.profile_id : null,
      });
      return route.fulfill({ json: { previous } });
    }
    if (url.pathname === '/api/v1/faces/restore') {
      const previous = route.request().postDataJSON();
      for (const old of previous) decisions.set(old.id, { status: old.status, owner: old.profile_id || old.suggested_profile_id });
      return route.fulfill({ json: { restored: previous.length } });
    }
    if (url.pathname.endsWith('/reading')) return route.fulfill({ json: { readings: [{ kind: 'social_story', entity_id: mid(200), summary: '已保存的图片描述', items: [{ media_id: mid(1), text: '示例文字' }] }] } });
    if (url.pathname.startsWith('/api/v1/media/')) return route.fulfill({ contentType: 'image/svg+xml', body: drawing });
    if (url.pathname === '/api/v1/me') return route.fulfill({ json: { id: 'test' } });
    return route.fulfill({ status: 404, json: { detail: 'Not in fixture' } });
  });

  await page.goto(`${base}/profiles/${person}`);
  await expect(page.getByRole('heading', { level: 1 })).toContainText('相册测试');
  expect(requests.some((url) => url.pathname.startsWith('/api/v1/faces/people/'))).toBe(false);
  await page.getByRole('navigation', { name: '资料分区' }).getByRole('button', { name: '照片', exact: true }).click();
  const panel = page.locator('[data-tab-panel="photos"]');
  const views = panel.getByRole('group', { name: '浏览视图' });
  await expect(views.getByRole('button', { name: '人脸', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(panel.getByRole('button', { name: /^查看人脸/ })).toHaveCount(25);
  const facePreview = panel.getByAltText('人脸预览').nth(2);
  await expect(facePreview).toHaveAttribute('src', `/api/v1/media/${mid(2)}/preview?face_id=${fid(102)}&v=0.6%2C0.2%2C0.2%2C0.3`);
  expect(requests.filter((url) => url.pathname.startsWith('/api/v1/media/')).every((url) => url.pathname.endsWith('/preview'))).toBe(true);
  await views.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `.local-validation/profile-faces-${width}.png`, fullPage: false });
  await panel.getByRole('button', { name: /^查看人脸/ }).nth(2).click();
  const faceDetail = page.getByRole('dialog', { name: '照片详情' });
  await expect(faceDetail.getByRole('region', { name: '选中人脸的归属' })).toContainText('匹配分数 0.610');
  await expect(faceDetail.getByRole('link', { name: '打开原图' })).toHaveAttribute('href', `/api/v1/media/${mid(2)}`);
  await page.keyboard.press('Escape');
  const galleryCalls = requests.filter((url) => url.pathname === `/api/v1/faces/people/${person}/photos`).length;
  await views.getByRole('button', { name: '照片', exact: true }).click();
  await expect(panel.getByRole('button', { name: /^查看照片/ })).toHaveCount(24);
  expect(requests.filter((url) => url.pathname === `/api/v1/faces/people/${person}/photos`).length).toBe(galleryCalls);
  await panel.getByRole('button', { name: /^查看照片/ }).first().click();
  const detail = page.getByRole('dialog', { name: '照片详情' });
  await expect(detail.getByRole('link', { name: '查看原动态' })).toHaveAttribute('href', `/timeline?event=${mid(200)}`);
  await page.screenshot({ path: `.local-validation/photo-detail-${width}.png` });
  await detail.getByRole('button', { name: '是本人', exact: true }).click();
  await expect(detail.getByRole('alert')).toContainText('原归属已保留');
  await expect(detail.getByRole('button', { name: '是本人', exact: true })).toBeVisible();
  await detail.getByRole('button', { name: '重试', exact: true }).click();
  await expect(detail.getByRole('status')).toContainText('已保存');
  await expect(detail.getByRole('button', { name: '是本人', exact: true })).toHaveCount(0);
  await detail.getByRole('button', { name: '撤销上一步' }).click();
  await expect(detail.getByRole('button', { name: '是本人', exact: true })).toBeVisible();
  await detail.getByRole('button', { name: '改为其他联系人' }).click();
  await detail.getByRole('textbox', { name: '搜索联系人' }).fill('另一个');
  await detail.getByRole('button', { name: '另一个联系人', exact: true }).click();
  await expect(panel.getByRole('button', { name: /^查看照片/ })).toHaveCount(23);
  await detail.getByRole('button', { name: '撤销上一步' }).click();
  await expect(panel.getByRole('button', { name: /^查看照片/ })).toHaveCount(24);
  await page.keyboard.press('Escape');
  await expect(detail).toHaveCount(0);

  await panel.getByRole('button', { name: /加载更多|正在加载|重试加载/ }).scrollIntoViewIfNeeded();
  releasePaging();
  await expect(panel.getByRole('button', { name: '重试加载' })).toBeVisible();
  expect(nextCalls).toBe(1);
  await panel.getByRole('button', { name: '重试加载' }).click();
  await expect(panel.getByRole('button', { name: /^查看照片/ })).toHaveCount(26);
  await expect(panel.getByText('已显示全部照片')).toBeVisible();
  expect(nextCalls).toBe(2);
  await panel.getByRole('button', { name: /^查看照片/ }).last().scrollIntoViewIfNeeded();
  const scroll = await page.evaluate(() => window.scrollY);
  await panel.getByRole('button', { name: /^查看照片/ }).last().click();
  await page.keyboard.press('Escape');
  expect(Math.abs(await page.evaluate(() => window.scrollY) - scroll)).toBeLessThan(5);
  await page.getByRole('navigation', { name: '资料分区' }).getByRole('button', { name: '动态', exact: true }).click();
  await page.getByRole('navigation', { name: '资料分区' }).getByRole('button', { name: '照片', exact: true }).click();
  await expect(panel.getByRole('button', { name: /^查看照片/ })).toHaveCount(26);
  expect(nextCalls).toBe(2);
  await panel.getByRole('group', { name: '照片归属状态' }).getByRole('button', { name: /^已确认/ }).click();
  await expect(panel.getByText('还没有已确认照片')).toBeVisible();
  await panel.getByRole('group', { name: '照片归属状态' }).getByRole('button', { name: /^全部/ }).click();
  await expect(panel.getByRole('button', { name: /^查看照片/ })).toHaveCount(26);
  await views.getByRole('button', { name: '人脸', exact: true }).click();
  await expect(panel.getByRole('button', { name: /^查看人脸/ })).toHaveCount(27);
  await page.getByRole('navigation', { name: '资料分区' }).getByRole('button', { name: '动态', exact: true }).click();
  await page.getByRole('navigation', { name: '资料分区' }).getByRole('button', { name: '照片', exact: true }).click();
  await expect(views.getByRole('button', { name: '人脸', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(panel.getByRole('button', { name: /^查看人脸/ })).toHaveCount(27);
  expect(nextCalls).toBe(2);
  expect(await page.evaluate(noOverflow)).toBe(true);
  await panel.getByRole('group', { name: '照片归属状态' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: `.local-validation/photos-${width}.png`, fullPage: false });

  const bulk = panel.getByRole('region', { name: '批量确认', exact: true });
  const faceList = panel.getByRole('list', { name: '人脸缩略图' });
  await expect(bulk).toHaveCount(0);
  await expect(faceList.getByRole('checkbox')).toHaveCount(0);
  const faceBefore = await faceList.getByAltText('人脸预览').first().boundingBox();
  const faceSource = await faceList.getByAltText('人脸预览').first().getAttribute('src');
  await panel.getByRole('button', { name: '批量管理', exact: true }).click();
  await expect(views.getByRole('button', { name: '人脸', exact: true })).toHaveAttribute('aria-pressed', 'true');
  const faceAfter = await faceList.getByAltText('人脸预览').first().boundingBox();
  expect(faceAfter!.width).toBe(faceBefore!.width);
  expect(faceAfter!.height).toBe(faceBefore!.height);
  await expect(faceList.getByAltText('人脸预览').first()).toHaveAttribute('src', faceSource!);
  const choiceBox = await faceList.getByRole('checkbox').first().boundingBox();
  expect(choiceBox!.y).toBeGreaterThanOrEqual(faceAfter!.y + faceAfter!.height);
  expect(await bulk.evaluate((element) => getComputedStyle(element).position)).toBe('static');
  await panel.getByRole('group', { name: '照片归属状态' }).getByRole('button', { name: /^已确认/ }).click();
  await expect(panel.getByText('还没有已确认照片')).toBeVisible();
  await panel.getByRole('button', { name: '退出批量管理', exact: true }).click();
  await expect(panel.getByRole('button', { name: '批量管理', exact: true })).toHaveAttribute('aria-pressed', 'false');
  await panel.getByRole('group', { name: '照片归属状态' }).getByRole('button', { name: /^全部/ }).click();
  await panel.getByRole('button', { name: '批量管理', exact: true }).click();
  await faceList.getByRole('button', { name: /^查看人脸/ }).first().click();
  await expect(faceDetail).toBeVisible();
  await page.keyboard.press('Escape');
  await faceList.getByRole('checkbox').nth(0).check();
  await faceList.getByRole('checkbox').nth(1).check();
  await expect(bulk).toContainText('已选 2 张人脸');
  await views.getByRole('button', { name: '照片', exact: true }).click();
  const photoList = panel.getByRole('list', { name: '照片缩略图' });
  await expect(photoList.getByRole('checkbox').nth(0)).toBeChecked();
  await expect(photoList.getByRole('checkbox').nth(1)).toBeChecked({ indeterminate: true });
  await bulk.getByRole('button', { name: '选择已加载候选' }).click();
  await expect(bulk).toContainText('已选 27 张人脸');
  await expect(bulk).toContainText('来自 26 张照片');
  await page.screenshot({ path: `.local-validation/profile-batch-photos-${width}.png`, fullPage: false });
  failSave = true;
  await bulk.getByRole('button', { name: '确认选中', exact: true }).click();
  await expect(bulk.getByRole('alert')).toContainText('所选人脸已保留');
  await expect(bulk).toContainText('已选 27 张人脸');
  await bulk.getByRole('button', { name: '重试批量确认' }).click();
  await expect(bulk.getByRole('status')).toContainText('已确认 27 张人脸');
  expect(lastDecision!.face_ids).toHaveLength(27);
  expect(lastDecision!.face_ids).not.toContain(fid(202));
  expect(lastDecision!.profile_id).toBe(person);
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(panel.getByRole('group', { name: '照片归属状态' }).getByRole('button', { name: /^已确认/ })).toContainText('26');
  const photoBefore = await photoList.getByRole('button', { name: /^查看照片/ }).first().boundingBox();
  await panel.getByRole('button', { name: '退出批量管理', exact: true }).click();
  await expect(bulk).toHaveCount(0);
  await expect(photoList.getByRole('checkbox')).toHaveCount(0);
  await expect(views.getByRole('button', { name: '照片', exact: true })).toHaveAttribute('aria-pressed', 'true');
  const photoAfter = await photoList.getByRole('button', { name: /^查看照片/ }).first().boundingBox();
  expect(photoAfter!.width).toBe(photoBefore!.width);
  expect(photoAfter!.height).toBe(photoBefore!.height);
  await panel.getByRole('button', { name: '撤销批量确认' }).click();
  await expect(panel.getByRole('status')).toContainText('已撤销本次批量确认');
  await panel.getByRole('button', { name: '批量管理', exact: true }).click();
  await expect(bulk.getByRole('status')).toContainText('已撤销本次批量确认');
  await expect(photoList.getByRole('checkbox').nth(0)).not.toBeChecked();
  await views.getByRole('button', { name: '人脸', exact: true }).click();
  await faceList.getByRole('checkbox').nth(0).check();
  await faceList.getByRole('checkbox').nth(1).check();
  await page.screenshot({ path: `.local-validation/profile-batch-faces-${width}.png`, fullPage: false });
  await bulk.getByRole('button', { name: '确认选中', exact: true }).click();
  await expect(bulk.getByRole('status')).toContainText('已确认 2 张人脸');
  expect(lastDecision!.face_ids).toEqual([fid(1), fid(2)]);
  await bulk.getByRole('button', { name: '撤销批量确认' }).click();
  await views.getByRole('button', { name: '照片', exact: true }).click();
  await photoList.getByRole('checkbox').nth(1).check();
  await bulk.getByRole('button', { name: '确认选中', exact: true }).click();
  await expect(bulk.getByRole('status')).toContainText('已确认 2 张人脸');
  expect(lastDecision!.face_ids).toEqual([fid(2), fid(102)]);
  expect(lastDecision!.face_ids).not.toContain(fid(202));
  await bulk.getByRole('button', { name: '撤销批量确认' }).click();
  await expect(bulk.getByRole('status')).toContainText('已撤销本次批量确认');
  expect(await page.evaluate(noOverflow)).toBe(true);
  for (let i = 2; i <= 26; i++) decisions.set(fid(i), { status: 'confirmed', owner: person });
  decisions.set(fid(102), { status: 'confirmed', owner: person });
  await panel.getByRole('group', { name: '照片归属状态' }).getByRole('button', { name: /^候选/ }).click();
  await expect(photoList.getByRole('checkbox')).toHaveCount(1);
  await bulk.getByRole('button', { name: '选择已加载候选' }).click();
  await bulk.getByRole('button', { name: '确认选中', exact: true }).click();
  await expect(panel.getByText('还没有候选照片')).toBeVisible();
  await expect(bulk.getByRole('button', { name: '撤销批量确认' })).toBeVisible();
  await bulk.getByRole('button', { name: '撤销批量确认' }).click();
  await expect(photoList.getByRole('checkbox')).toHaveCount(1);
});
