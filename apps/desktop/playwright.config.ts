import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  use: {
    baseURL: "http://127.0.0.1:1420",
    viewport: { width: 820, height: 760 },
    ...(process.env.MONICA_TEST_BROWSER_CHANNEL
      ? { channel: process.env.MONICA_TEST_BROWSER_CHANNEL }
      : {}),
  },
  webServer: {
    command: "npm run dev",
    url: "http://127.0.0.1:1420",
    reuseExistingServer: !process.env.CI,
  },
});
