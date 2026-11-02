"""Read current SDUI activity cards without relying on hashed CSS classes.

The control menu identifies the outer post. Its copy-link URL resolves share
identities to canonical activities, preserving deduplication with older imports.
Clipboard writes are intercepted inside the page and restored immediately.
"""
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .browser import PageUnavailable

CARD_DETAILS = r"""element => {
  const text = e => (e?.innerText || e?.textContent || '').trim();
  const firstActor=element.querySelector('a[href*="/in/"],a[href*="/company/"]');
  let headerNode=element.querySelector('button[aria-label^="Open control menu for post"]');
  while (headerNode && firstActor && !headerNode.contains(firstActor)) headerNode=headerNode.parentElement;
  const paragraphs = [...element.querySelectorAll('p[componentkey]')].filter(e => {
    const anchor=e.closest('a');
    return !headerNode?.contains(e) && !e.closest('button') && (!anchor || /\/feed\/update\//.test(anchor.href));
  });
  const dateLabel = /^(?:\d+\s*(?:s|min|h|hr|d|w|wk|mo|m|yr|y)(?:\s*[•·].*)?|[A-Z][a-z]{2} \d{1,2}(?:, \d{4})?)$/i;
  const body = paragraphs.find(e => text(e) && !dateLabel.test(text(e)));
  const commentary = text(body);
  const actors=[...element.querySelectorAll('a[href*="/in/"],a[href*="/company/"]')];
  // A quoted post can have a longer body and another actor. The primary
  // commentary precedes it; retain the actor header before that commentary.
  const actor = body ? actors.filter(a => a.compareDocumentPosition(body) & Node.DOCUMENT_POSITION_FOLLOWING).at(-1) : null;
  const header = text(element).slice(0, 180);
  const images = [...element.querySelectorAll('img')].map(e => e.currentSrc || e.src)
    .filter(url => /^https:\/\/[^/]*licdn\.com\//i.test(url)
      && !/profile-displayphoto|company-logo|school-logo/i.test(url));
  const videos = [...element.querySelectorAll('video')];
  return {text: commentary, actor_profile_url: actor?.href || null,
    repost_header: /\b(?:reposted|shared)\s+this\b|转发了|分享了/i.test(header) ? header : null,
    image_urls: [...new Set([...images,...videos.map(e=>e.poster).filter(Boolean)])],
    video_url: videos.map(e=>e.currentSrc||e.src).find(url=>/^https:\/\/[^/]*licdn\.com\//i.test(url)) || null};
}"""


def canonical_activity(url):
    parts = urlsplit(url or '')
    if parts.scheme != 'https' or parts.hostname not in {'www.linkedin.com', 'linkedin.com'}:
        return None
    highlighted = parse_qs(parts.query).get('highlightedUpdateUrn', [''])[0]
    if re.fullmatch(r'urn:li:activity:[0-9]+', highlighted):
        return highlighted
    match = re.search(r'(?:/feed/update/urn:li:activity:|-activity-)([0-9]+)', parts.path)
    return 'urn:li:activity:' + match.group(1) if match else None


async def copied_activity(page, menu):
    # No OS clipboard access: the site's explicit copy action delivers its URL
    # to this temporary page-local sink. Never call the original write method.
    await page.evaluate('''() => {
      if (!navigator.clipboard) throw new Error('Clipboard URL capture unavailable');
      const state={text:null,original:{}};
      for (const name of ['writeText','write']) state.original[name]=Object.getOwnPropertyDescriptor(navigator.clipboard,name);
      window.__monicaPostLinkCapture=state;
      Object.defineProperty(navigator.clipboard,'writeText',{configurable:true,value:async text=>{state.text=text;}});
      Object.defineProperty(navigator.clipboard,'write',{configurable:true,value:async()=>{throw new Error('Unexpected clipboard format');}});
    }''')
    try:
        await menu.get_by_role('menuitem', name='Copy link to post', exact=True).click(timeout=5000)
        await page.wait_for_function('() => window.__monicaPostLinkCapture.text !== null', timeout=5000)
        url = await page.evaluate('() => window.__monicaPostLinkCapture.text')
    finally:
        await page.evaluate('''() => {
          const state=window.__monicaPostLinkCapture;
          if (!state) return;
          for (const name of ['writeText','write']) {
            if (state.original[name]) Object.defineProperty(navigator.clipboard,name,state.original[name]);
            else delete navigator.clipboard[name];
          }
          delete window.__monicaPostLinkCapture;
        }''')
    for _ in range(5):
        urn = canonical_activity(url)
        if urn:
            return urn
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.hostname != 'lnkd.in':
            break
        response = await page.request.get(url, max_redirects=0, timeout=10000)
        location = response.headers.get('location')
        await response.dispose()
        if not location:
            break
        url = urljoin(url, location)
    raise PageUnavailable('Post copy URL did not resolve to a canonical LinkedIn activity')


async def extract_activity_cards(page, *, identities=None):
    identities = {} if identities is None else identities
    cards = page.locator('[role="listitem"][componentkey^="update-card-focus"]')
    count = await cards.count()
    if not count and await page.locator('main a[href*="/feed/update/"]').count():
        raise PageUnavailable('Post links are visible but the activity card layout is unrecognized')
    result = []
    for index in range(min(count, 20)):
        card = cards.nth(index)
        details = await card.evaluate(CARD_DETAILS)
        if not details['text']:
            continue
        key = await card.get_attribute('componentkey')
        if key in identities:
            urn = identities[key]
            result.append({**details, 'urn': urn,
                           'linkedin_url': f'https://www.linkedin.com/feed/update/{urn}/'})
            continue
        # Scrolling to a menu can virtualize preceding cards. Keep this card's
        # identity rather than resolving its old list position after scrolling.
        card = page.locator('[componentkey=' + json.dumps(key) + ']')
        menu_button = card.get_by_role('button', name=re.compile(r'^Open control menu for post'))
        if not await menu_button.count():
            raise PageUnavailable('Activity card has content but no readable post control menu')
        try:
            await menu_button.first.click(timeout=5000)
            menu = page.get_by_role('menu')
            try:
                await menu.wait_for(state='visible', timeout=1500)
            except PlaywrightTimeoutError:
                # Lazy image layout can consume the first pointer click while
                # scrolling an offscreen card into view. Keyboard activation
                # targets the same named control without screen coordinates.
                await menu_button.first.press('Enter', timeout=5000)
                await menu.wait_for(state='visible', timeout=5000)
            urn = await copied_activity(page, menu)
            identities[key] = urn
            result.append({**details, 'urn': urn,
                           'linkedin_url': f'https://www.linkedin.com/feed/update/{urn}/'})
        finally:
            await page.keyboard.press('Escape')
            menu = page.get_by_role('menu')
            try:
                await menu.wait_for(state='hidden', timeout=1000)
            except PlaywrightTimeoutError:
                await menu_button.first.click(timeout=5000)
                await menu.wait_for(state='hidden', timeout=5000)
    if count and not result:
        raise PageUnavailable('Activity cards are visible but their post content could not be read')
    return result
