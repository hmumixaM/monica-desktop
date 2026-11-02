#!/usr/bin/env python3
"""Inspect a running private smoke fixture; cookies arrive only through stdin."""
import json
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    request = json.load(sys.stdin)
    output = Path(request['screenshots'])
    output.mkdir(parents=True, mode=0o700)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 1000}, locale='zh-CN')
        context.add_cookies(request['cookies'])
        page = context.new_page()
        page.goto(request['web'] + '/imports')
        heading = page.get_by_role('heading', name='历史变化', exact=True)
        expect(heading).to_be_visible()
        history = page.locator('section').filter(has=heading)
        button = history.get_by_role('listitem').filter(has_text='资料字段').first.get_by_role('button', name='查看变化')
        expect(button).to_be_visible()
        history.scroll_into_view_if_needed()
        page.screenshot(path=str(output / 'history-desktop.png'), animations='disabled')
        button.click()
        dialog = page.get_by_role('dialog')
        expect(dialog).to_be_visible()
        expect(dialog.get_by_text('修改前', exact=True).first).to_be_visible()
        expect(dialog.get_by_text('修改后', exact=True).first).to_be_visible()
        expect(dialog.get_by_text('Local CDC verification', exact=True)).to_be_visible()
        expect(dialog.get_by_text('Local CDC verification updated', exact=True)).to_be_visible()
        page.screenshot(path=str(output / 'history-detail-desktop.png'), animations='disabled')
        page.set_viewport_size({'width': 590, 'height': 850})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert dialog.evaluate('(element) => element.scrollWidth <= element.clientWidth')
        page.screenshot(path=str(output / 'history-detail-narrow.png'), animations='disabled')
        dialog.get_by_role('button', name='关闭', exact=True).click()
        page.get_by_label('历史变化来源').select_option('instagram')
        expect(history.get_by_text('Instagram', exact=True).first).to_be_visible()
        expect(history.locator('li small').filter(has_text='Monica')).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        history.scroll_into_view_if_needed()
        page.screenshot(path=str(output / 'history-narrow.png'), animations='disabled')
        browser.close()
    print('Web history visual checks passed')


if __name__ == '__main__':
    main()
