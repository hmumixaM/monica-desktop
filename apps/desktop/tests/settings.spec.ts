import { expect, test } from "@playwright/test";

test("distinguishes dependency waits, queued work and failed projections", async ({ page }) => {
  await page.evaluate(() => {
    (window as any).__desktopTest.state.account_created = true;
    (window as any).__desktopTest.state.pipeline = {
      consumers: [{provider: "instagram", state: "ready", heartbeat_age_seconds: 1, retained_wal_bytes: 0}],
      projection: {count: 4, oldest_seconds: 12},
      media_waiting: [{reason: "source_pending", count: 3}],
      lanes: [{lane: "projection", status: "failed", count: 1, oldest_seconds: 30, retry_waiting: 0},
        {lane: "ai", status: "queued", count: 2, oldest_seconds: 5, retry_waiting: 1}],
    };
  });
  const panel = page.getByRole("region", {name: "任务通道"});
  await expect(panel.getByText("等待对应内容入库 · 3 项")).toBeVisible();
  await expect(panel.getByText("需要重试 · 1 项")).toBeVisible();
  await expect(panel.getByText("AI 分析", {exact: true})).toBeVisible();
  await page.setViewportSize({width: 590, height: 780});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({path: test.info().outputPath("pipeline-status.png"), fullPage: true});
});

test("shows committed import progress, real totals, paused checkpoints and stale phases", async ({ page }) => {
  await page.evaluate(() => {
    const state = (window as any).__desktopTest.state;
    Object.assign(state, {account_created: true, configured: true, profiles: 1234, posts_imported: 4567, messages_imported: 8900,
      import_phases: [
        {source: "archives", phase: "messages", state: "running", progress: {messages_inserted: 500}},
        {source: "archives", phase: "moments", state: "running", stage: "projecting", progress: {processed: 240, total: 1000}},
      ]});
  });
  await expect(page.getByRole("heading", {name: "导入进度"})).toBeVisible();
  await expect(page.getByText("8,900", {exact:true})).toBeVisible();
  await expect(page.getByText("本轮已保存 500 条消息")).toBeVisible();
  const messages = page.getByRole("listitem").filter({hasText: "微信聊天记录"});
  await expect(messages.getByText("正在导入")).toBeVisible();
  await expect(messages.locator("progress")).toHaveCount(0);
  await expect(page.getByRole("progressbar", {name:"微信朋友圈处理进度"})).toHaveAttribute("max", "1000");
  await page.screenshot({path: test.info().outputPath("import-progress.png")});
  await page.evaluate(() => {
    const state = (window as any).__desktopTest.state;
    state.state = "paused";
    state.import_phases[0].state = "pending";
    state.import_phases[0].progress.messages_inserted = 750;
  });
  await expect(messages.getByText("已暂停", {exact:true})).toBeVisible();
  await expect(messages.getByText("本轮已保存 750 条消息")).toBeVisible();
  await expect(messages.getByText("恢复后从已保存的进度继续。")).toBeVisible();
  await page.evaluate(() => {
    const state = (window as any).__desktopTest.state;
    state.state = "running";
    state.import_phases[0].state = "interrupted";
  });
  await expect(messages.getByText("等待恢复", {exact:true})).toBeVisible();
  await page.setViewportSize({width:590, height:780});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const config = {
      wechat: { data_dir: "", config_file: "" },
      ai: {
        enabled: true,
        base_url: "https://api.example.com/v1",
        api_key_set: true,
        batch_model: "text-model",
        vision_model: "vision-model",
      },
      sources: {
        instagram: { enabled: true, username: "example_user" },
        linkedin: { enabled: true },
        faces: { enabled: false },
      },
      autostart: false,
    };
    const state = {
      state: "running",
      operation: "",
      error: "",
      account_created: false,
      configured: false,
      web_url: "http://127.0.0.1:3210",
      config,
      services: [{ name: "postgres", state: "running" }],
      sources: [
        { name: "wechat", state: "not_configured" },
        { name: "instagram", state: "needs_login" },
      ],
    };
    const target = window as unknown as Record<string, unknown>;
    const calls: { command: string; args?: Record<string, unknown> }[] = [];
    target.__desktopTest = { state, calls, failNext: false };
    target.__TAURI_INTERNALS__ = {
      invoke: async (command: string, args?: Record<string, unknown>) => {
        calls.push({ command, args });
        if (command === "get_status") return structuredClone(state);
        if (command === "control_command") {
          state.operation = String(args?.command);
          state.error = "";
          window.setTimeout(() => {
            if ((target.__desktopTest as { failNext: boolean }).failNext)
              state.error = "模型服务拒绝了此密钥";
            else if (args?.command === "connect_wechat") {
              const payload = args.payload as { data_dir: string };
              state.config.wechat.data_dir = payload.data_dir.replace(
                /\/db_storage\/?$/,
                "",
              );
            }
            state.operation = "";
          }, 1100);
          return { accepted: true };
        }
        return null;
      },
    };
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "运行概览", exact: true }),
  ).toBeVisible();
});

test("waits for async completion, clears saved secret, and does not send it again", async ({
  page,
}) => {
  await page.getByRole("button", { name: "模型服务", exact: true }).click();
  await page.getByLabel("API Key").fill("test-only-key");
  await page.getByRole("button", { name: "保存设置", exact: true }).click();
  await expect(page.getByText("保存设置…", { exact: true })).toBeVisible();
  await expect(
    page.getByText("设置已保存。", { exact: true }),
  ).not.toBeVisible();
  await expect(page.getByText("设置已保存。", { exact: true })).toBeVisible();
  await expect(page.getByLabel("API Key")).toHaveValue("");
  await page.getByLabel("文本模型", { exact: true }).fill("another-model");
  await page.getByRole("button", { name: "保存设置", exact: true }).click();
  await expect(page.getByText("设置已保存。", { exact: true })).toBeVisible();
  const payloads = await page.evaluate(() => {
    const testState = (
      window as unknown as {
        __desktopTest: {
          calls: {
            command: string;
            args?: {
              command?: string;
              payload: { config: { ai: Record<string, unknown> } };
            };
          }[];
        };
      }
    ).__desktopTest;
    return testState.calls
      .filter(
        (call) =>
          call.command === "control_command" &&
          call.args?.command === "save_config",
      )
      .map((call) => call.args!.payload.config.ai);
  });
  expect(payloads[0].api_key).toBe("test-only-key");
  expect(payloads[1]).not.toHaveProperty("api_key");
  expect(payloads[1]).not.toHaveProperty("api_key_set");
});

test("shows a background failure instead of reporting a successful model connection", async ({
  page,
}) => {
  await page.evaluate(() => {
    (
      window as unknown as { __desktopTest: { failNext: boolean } }
    ).__desktopTest.failNext = true;
  });
  await page.getByRole("button", { name: "模型服务", exact: true }).click();
  await page
    .getByRole("button", { name: "保存并测试连接", exact: true })
    .click();
  await expect(
    page.getByText("模型服务拒绝了此密钥", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("模型服务连接成功。", { exact: true }),
  ).not.toBeVisible();
});

test("status polling preserves unsaved edits and narrow settings do not overflow", async ({
  page,
}) => {
  await page.getByRole("button", { name: "模型服务", exact: true }).click();
  await page.getByLabel("文本模型", { exact: true }).fill("unsaved-model");
  await page.waitForTimeout(2800);
  await expect(page.getByLabel("文本模型", { exact: true })).toHaveValue(
    "unsaved-model",
  );
  await page.setViewportSize({ width: 590, height: 520 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("source login is requested through the native bridge without a password field", async ({
  page,
}) => {
  await page.getByRole("button", { name: "数据源", exact: true }).click();
  await page
    .getByRole("button", { name: "登录 Instagram", exact: true })
    .click();
  await expect(
    page.getByText("Instagram 登录完成。", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("登录密码", { exact: true })).not.toBeVisible();
  const login = await page.evaluate(() => {
    const testState = (
      window as unknown as {
        __desktopTest: {
          calls: {
            command: string;
            args?: { command?: string; payload: unknown };
          }[];
        };
      }
    ).__desktopTest;
    return testState.calls.find((call) => call.args?.command === "source_login")
      ?.args?.payload;
  });
  expect(login).toEqual({ source: "instagram", username: "example_user" });
});

test("later settings saves preserve the account path normalized by WeChat connection", async ({
  page,
}) => {
  await page.getByRole("button", { name: "数据源", exact: true }).click();
  await page
    .getByLabel("微信数据目录", { exact: true })
    .fill("/example/account/db_storage");
  await page.getByRole("button", { name: "连接微信", exact: true }).click();
  await expect(
    page.getByText("微信连接设置已保存。同步状态会自动更新。", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("微信数据目录", { exact: true })).toHaveValue(
    "/example/account",
  );
  await page.getByRole("button", { name: "模型服务", exact: true }).click();
  await page.getByLabel("文本模型", { exact: true }).fill("updated-model");
  await page.getByRole("button", { name: "保存设置", exact: true }).click();
  await expect(page.getByText("设置已保存。", { exact: true })).toBeVisible();
  const savedPath = await page.evaluate(() => {
    const fixture = (
      window as unknown as {
        __desktopTest: {
          calls: {
            args?: {
              command?: string;
              payload?: { config?: { wechat?: { data_dir?: string } } };
            };
          }[];
        };
      }
    ).__desktopTest;
    return fixture.calls.find((call) => call.args?.command === "save_config")
      ?.args?.payload?.config?.wechat?.data_dir;
  });
  expect(savedPath).toBe("/example/account");
});
