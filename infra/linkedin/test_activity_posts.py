"""Run with the configured collector on PYTHONPATH, alongside its own tests."""
import os

import pytest
from linkedin_enrichment.activity_posts import canonical_activity, extract_activity_cards
from linkedin_enrichment.scrapers import PersonalPostsScraper, classify_post_authorship
from linkedin_enrichment.storage import activity_urn_posted_at
from playwright.async_api import async_playwright


def test_post_identity_accepts_only_explicit_linkedin_activity_urns():
    assert canonical_activity('https://www.linkedin.com/feed/update/urn:li:activity:456/') == 'urn:li:activity:456'
    assert canonical_activity('https://attacker.invalid/?targetUrn=urn:li:share:123') is None
    assert activity_urn_posted_at('urn:li:share:7406426662684471297').startswith('2025-12-')
    assert canonical_activity('https://www.linkedin.com/posts/alice_job-share-123/?highlightedUpdateUrn=urn%3Ali%3Aactivity%3A456') == 'urn:li:activity:456'
    assert canonical_activity('https://other.invalid/?highlightedUpdateUrn=urn:li:activity:456') is None
    assert classify_post_authorship('https://www.linkedin.com/in/alice/',actor_profile_url='https://www.linkedin.com/company/other/',repost_header=None) == (False,'other_actor')


async def test_sdui_reads_outer_post_identity_instead_of_a_quoted_post(tmp_path):
    executable = os.environ.get('LINKEDIN_CHROMIUM_EXECUTABLE')
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))
        try:
            page = await browser.new_page()
            html = '''
                <div role="listitem" componentkey="update-card-focus-example">
                  <div>
                    <a href="https://www.linkedin.com/in/alice/">Alice</a>
                    <p>A deliberately long professional headline that is not the post body</p>
                    <p componentkey="custom-link">Visit my website</p>
                    <p componentkey="date-label">1mo • Edited</p>
                    <button aria-label="Open control menu for post by Alice"
                      onclick="document.querySelector('[role=menu]').hidden=!document.querySelector('[role=menu]').hidden">Menu</button>
                  </div>
                  <p componentkey="post-body">I started a new job today.</p>
                  <a href="https://www.linkedin.com/company/quoted/">Quoted company</a>
                  <p componentkey="quoted-body">A much longer quoted company announcement that must not replace Alice's own status update or become her employer.</p>
                  <a href="https://www.linkedin.com/feed/update/urn:li:share:111/">Quoted post</a>
                </div>
                <div role="menu" hidden>
                  <a href="https://www.linkedin.com/preload/embed-modal/?targetUrn=urn%3Ali%3Ashare%3A7406426662684471297">Embed this post</a>
                  <button role="menuitem" onclick="navigator.clipboard.writeText('https://www.linkedin.com/feed/update/urn:li:activity:7406426663296708612/')">Copy link to post</button>
                </div>'''
            await page.route('**/*', lambda route: route.fulfill(status=200,content_type='text/html',body=html))
            await page.goto('https://www.linkedin.com/test')
            await page.evaluate('''() => {window.actualClipboardWrites=0;Object.defineProperty(navigator.clipboard,'writeText',{configurable:true,value:async()=>{window.actualClipboardWrites++;}});}''')
            rows = await extract_activity_cards(page)
            assert len(rows) == 1
            assert rows[0]['urn'] == 'urn:li:activity:7406426663296708612'
            assert rows[0]['text'] == 'I started a new job today.'
            assert rows[0]['actor_profile_url'] == 'https://www.linkedin.com/in/alice/'
            assert await page.evaluate('() => window.actualClipboardWrites') == 0
            await page.evaluate("() => navigator.clipboard.writeText('restoration test')")
            assert await page.evaluate('() => window.actualClipboardWrites') == 1
        finally:
            await browser.close()


async def test_visible_card_without_identifier_is_not_an_empty_success():
    from unittest.mock import AsyncMock, MagicMock

    from linkedin_enrichment.browser import PageUnavailable

    card = MagicMock()
    card.evaluate = AsyncMock(return_value={'text': 'A visible status update'})
    card.get_attribute = AsyncMock(return_value='update-card-focus-example')
    card.get_by_role.return_value.count = AsyncMock(return_value=0)
    cards = MagicMock()
    cards.count = AsyncMock(return_value=1)
    cards.nth.return_value = card
    page = MagicMock()
    page.locator.side_effect = [cards, card]
    with pytest.raises(PageUnavailable, match='no readable post control menu'):
        await extract_activity_cards(page)


async def test_pinned_known_post_does_not_hide_newer_unseen_posts():
    from unittest.mock import AsyncMock

    from linkedin_scraper import Post

    scraper = PersonalPostsScraper(object())
    for method in ('navigate_and_wait', 'check_rate_limit', '_wait_for_posts_to_load', '_scroll_for_more_posts'):
        setattr(scraper, method, AsyncMock())
    scraper._extract_posts_from_page = AsyncMock(return_value=[Post(urn='known'), Post(urn='new')])
    posts = await scraper.scrape('https://www.linkedin.com/in/alice/', max_scrolls=0, known_urns={'known'})
    assert [post.urn for post in posts] == ['new']
