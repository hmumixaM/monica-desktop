import type {BrowserContextOptions} from '@playwright/test';

export const base = process.env.CRM_BASE_URL || 'http://127.0.0.1:3001';

/** A session cookie from CRM_SESSION_TOKEN, so it never has to be saved to disk; otherwise a saved browser state. */
export const storageState: BrowserContextOptions['storageState'] = process.env.CRM_SESSION_TOKEN
  ? {cookies: [{name: 'crm_session', value: process.env.CRM_SESSION_TOKEN, domain: new URL(base).hostname, path: '/',
    httpOnly: true, secure: false, sameSite: 'Lax', expires: -1}], origins: []}
  : process.env.CRM_BROWSER_STATE || '.local-validation/browser-state.json';

export const noOverflow = () => document.documentElement.scrollWidth <= innerWidth;
