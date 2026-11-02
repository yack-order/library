"""Anonymous public discovery. Only controlled status codes reach Actions logs."""
import json
import os
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from playwright.sync_api import sync_playwright
from runner import ArchiveError, Client

MAX_PAGES = 10000
MAX_EXPANSIONS = 2000
SHARE = re.compile(r'https://(?:yoto\.io/[A-Za-z0-9]{5}(?:\?[^\s<>"\']*)?|share\.yoto\.co/s/[A-Za-z0-9_-]+(?:\?[^\s<>"\']*)?)')


def page_url(value, base='https://yoto.space/'):
    p = urlsplit(urljoin(base, value))
    if p.scheme != 'https' or p.netloc != 'yoto.space' or p.username or p.password:
        return None
    if re.match(r'^/(member|search|login|signup|auth|api)(/|$)', p.path):
        return None
    if re.search(r'\.(png|jpg|jpeg|gif|svg|webp|mp3|mp4|pdf|zip)$', p.path, re.I):
        return None
    query=urlencode(sorted((key,value) for key,value in parse_qsl(p.query) if key in {'page','cursor','offset','before','after'}))
    return urlunsplit(('https', 'yoto.space', p.path.rstrip('/') or '/', query, ''))


class DiscoveryClient(Client):
    def __init__(self):
        super().__init__()
        self.base = self.base.replace('/archive/jobs/', '/space/jobs/')


def discover(page, client, saved=None, canary=False):
    state = saved or {'frontier': ['https://yoto.space/made-by-you/post/my-dragon-and-me-Vlt7BctgDUB6Gui' if canary else 'https://yoto.space/'],
                      'visited': [], 'shares': [], 'complete': False, 'canary': canary}
    visited = set(state['visited'])
    pairs = {(s['url'], s['provenance']) for s in state['shares']}
    frontier = list(dict.fromkeys(state['frontier']))

    def checkpoint(complete=False):
        state.update(frontier=frontier, visited=sorted(visited), shares=[{'url': u, 'provenance': p} for u, p in sorted(pairs)], complete=complete)
        client.call('checkpoint', state)

    def collect(current):
        # Only visible/public DOM is read. No account data or browser storage.
        data = page.evaluate("""() => ({text:document.body.innerText,
          links:Array.from(document.querySelectorAll('a[href],iframe[src]')).map(e=>e.href||e.src)})""")
        if re.search(r'just a moment|verify you are human|access denied|checking your browser', data['text'], re.I):
            raise ArchiveError('discovery_blocked')
        if len(data['text'].strip()) < 40:
            raise ArchiveError('discovery_parse_failed')
        for value in data['links'] + SHARE.findall(data['text']):
            match = SHARE.fullmatch(value.rstrip('.,;)'))
            if match:
                pairs.add((match.group(0), current))
            following = page_url(value, current)
            if not canary and following and following not in visited and following not in frontier:
                frontier.append(following)
        if len(frontier) + len(visited) > MAX_PAGES:
            raise ArchiveError('discovery_page_limit')
        return len(data['links']), len(data['text'])

    while frontier:
        current = frontier[0]
        response = page.goto(current, wait_until='networkidle', timeout=60000)
        if not response or response.status != 200 or page_url(page.url) != current:
            raise ArchiveError('discovery_http_failed')
        collect(current)
        # Public navigation menus are mounted lazily by the community frontend.
        # Open each menu explicitly before claiming navigation coverage.
        if not canary:
            for name in ['Yoto Daily', 'Free Audio', 'Create', 'Communities', 'Get Involved', 'Feed']:
                menu = page.get_by_role('button', name=name, exact=True)
                if menu.count() and menu.first.is_visible():
                    menu.first.click(timeout=10000)
                    collect(current)
                    page.keyboard.press('Escape')
        # Expand public pagination/replies, retaining every newly exposed link.
        # A control that never advances is a parse failure, never "end of list".
        count = 0
        while True:
            controls = page.get_by_role('button', name=re.compile(r'^(show more|load more|show (?:all |more )?replies|view (?:all |more )?replies|load (?:more )?comments)$', re.I))
            target = next((control for control in controls.all() if control.is_visible() and control.is_enabled()), None)
            if target is None:
                break
            if count >= MAX_EXPANSIONS:
                raise ArchiveError('discovery_pagination_limit')
            before = collect(current)
            target.click(timeout=30000)
            page.wait_for_load_state('networkidle', timeout=60000)
            after = collect(current)
            if before == after:
                raise ArchiveError('discovery_pagination_stalled')
            count += 1
            checkpoint()
        # Numeric/next page links are already in the frontier. Browser-only
        # next buttons require an advance and are followed until disabled/loop.
        seen_next = set()
        for count in range(MAX_EXPANSIONS):
            buttons = page.get_by_role('button', name=re.compile(r'^next(?: page)?$', re.I))
            candidates = [b for b in buttons.all() if b.is_visible() and b.is_enabled()]
            if not candidates:
                break
            signature = json.dumps(page.locator('a[href]').evaluate_all('(els)=>els.map(e=>e.href)'))
            if signature in seen_next:
                break
            seen_next.add(signature)
            candidates[0].click(timeout=30000)
            page.wait_for_load_state('networkidle', timeout=60000)
            collect(current)
            checkpoint()
        else:
            raise ArchiveError('discovery_pagination_limit')
        visited.add(current)
        frontier.remove(current)
        checkpoint()
        time.sleep(1)
    checkpoint(complete=True)


def main():
    client = None
    try:
        client = DiscoveryClient()
        claimed = client.call('claim')
        client.headers['X-Archive-Lease'] = claimed['leaseToken']
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                page = context.new_page()
                discover(page, client, claimed.get('checkpoint'), claimed.get('canary', False))
            finally:
                browser.close()
        print('Public discovery completed')
        return 0
    except Exception:
        if client and 'X-Archive-Lease' in client.headers:
            try:
                client.call('fail')
            except Exception:
                pass
        print('Public discovery incomplete; baseline preserved')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
