import {defineConfig} from '@playwright/test';
// These run against real NAS data, where a cold query can take several seconds.
export default defineConfig({testDir:'tests/e2e',workers:1,timeout:90000,expect:{timeout:15000},use:{channel:process.env.CRM_TEST_BROWSER === 'webkit' ? undefined : 'chrome',headless:true},reporter:'list',outputDir:'.local-validation/playwright-results'});
