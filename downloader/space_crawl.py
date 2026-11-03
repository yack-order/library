"""Anonymous public discovery. Only controlled status codes reach Actions logs."""
import json
import hashlib
import os
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout, Error as BrowserError
from runner import ArchiveError, Client

MAX_PAGES = 10000
MAX_EXPANSIONS = 2000
PAGE_BATCH = 75
BATCH_SECONDS = 900
EXPANSION_BATCH = 20
PROGRESS_TIMEOUT_MS = 30000
# Loading button labels are not content progress. Keep the count so removing
# an exhausted pagination control is accepted even when no new posts arrive.
PROGRESS = """() => {
  let text=document.body.innerText;
  for (const node of document.querySelectorAll('button,[role=button],[role=progressbar]')) {
    const label=node.innerText?.trim(); if(label)text=text.split(label).join('');
  }
  return JSON.stringify({text,buttons:document.querySelectorAll('button,[role=button]').length,
    links:Array.from(document.querySelectorAll('a[href],iframe[src]')).map(e=>e.href||e.src),
    scroll:Array.from(document.querySelectorAll('div,section,ul')).filter(e=>e.clientWidth>0&&e.scrollWidth>e.clientWidth+1).map(e=>Math.round(e.scrollLeft))});
}"""


def advance(page, control):
    before = page.evaluate(PROGRESS)
    control.click(timeout=30000)
    try:
        page.wait_for_function('before => (' + PROGRESS + ')() !== before',
                               arg=before, timeout=PROGRESS_TIMEOUT_MS)
    except BrowserTimeout:
        raise ArchiveError('discovery_pagination_stalled') from None
    page.wait_for_load_state('networkidle', timeout=60000)

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


class PublicCursors:
    """Resume public read-query cursors through fresh anonymous browser requests.

    Never persist request headers, tokens, response nodes or application state.
    Scope each cursor to its page, endpoint, query and non-cursor variables.
    """
    def __init__(self, page, state, current):
        self.state, self.current, self.restored, self.pending = state, current, set(), {}
        page.route('**/*', self.route)
        page.on('response', self.response)
        page.on('requestfailed', lambda request: self.pending.pop(request, None))

    def route(self, route):
        request, override = route.request, None
        try:
            body = request.post_data_json if request.method == 'POST' else None
            if isinstance(body, dict) and re.match(r'^\s*query\b', body.get('query', '')):
                variables = body.get('variables') or {}
                if isinstance(variables.get('after'), str) and variables['after']:
                    endpoint = urlsplit(request.url)
                    shape = [endpoint.scheme, endpoint.netloc, endpoint.path, body['query'],
                             {key:value for key,value in variables.items() if key != 'after'}]
                    key = hashlib.sha256(json.dumps(shape,sort_keys=True).encode()).hexdigest()
                    owner = self.current()
                    self.pending[request] = (owner, key)
                    saved = self.state.setdefault('pagination', {}).get(owner, {}).get(key)
                    if saved and (owner,key) not in self.restored:
                        override = json.dumps({**body, 'variables': {**variables, 'after': saved}})
                        self.restored.add((owner,key))
        except (ValueError, TypeError, AttributeError, BrowserError):
            pass
        route.fallback(**({'post_data':override} if override else {}))

    def response(self, response):
        context = self.pending.pop(response.request, None)
        if not context or response.status != 200:
            return
        try:
            # Only a single read connection can establish this request's cursor.
            connections = [value['pageInfo'] for value in (response.json().get('data') or {}).values()
                           if isinstance(value,dict) and isinstance(value.get('pageInfo'),dict)]
            if len(connections) == 1:
                cursor = connections[0].get('endCursor')
                if isinstance(cursor,str) and 0 < len(cursor) <= 4096:
                    owner,key = context
                    self.state.setdefault('pagination', {}).setdefault(owner,{})[key] = cursor
        except (ValueError, TypeError, AttributeError, BrowserError):
            pass


def discover(page, client, saved=None, canary=False):
    state = saved or {'frontier': ['https://yoto.space/made-by-you/post/my-dragon-and-me-Vlt7BctgDUB6Gui' if canary else 'https://yoto.space/'],
                      'visited': [], 'shares': [], 'complete': False, 'canary': canary}
    visited = set(state['visited'])
    pairs = {(s['url'], s['provenance']) for s in state['shares']}
    frontier = list(dict.fromkeys(url for url in state['frontier'] if url not in visited))
    failures = state.setdefault('failures', {})
    current = None
    started = time.monotonic()
    cursors = PublicCursors(page, state, lambda: current)

    def checkpoint(exhausted=False):
        state.update(frontier=frontier, visited=sorted(visited), shares=[{'url': u, 'provenance': p} for u, p in sorted(pairs)],
                     complete=exhausted and not failures, exhausted=exhausted)
        client.call('checkpoint', state)

    def collect():
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
            if not canary and following and following not in visited and following not in frontier and failures.get(following,{}).get('attempts',0)<3:
                frontier.append(following)
        if len(frontier) + len(visited) > MAX_PAGES:
            raise ArchiveError('discovery_page_limit')

    for _ in range(PAGE_BATCH):
        if time.monotonic()-started >= BATCH_SECONDS:
            break
        if not frontier:
            checkpoint(exhausted=True)
            return state
        current = frontier[0]
        cursors.restored.clear()
        cursors.pending.clear()
        page_complete = True
        try:
            response = page.goto(current, wait_until='networkidle', timeout=60000)
            if not response or response.status != 200 or page_url(page.url) != current:
                raise ArchiveError('discovery_rate_limited' if response and response.status==429 else 'discovery_http_failed')
            collect()
            if not canary:
                for name in ['Yoto Daily', 'Free Audio', 'Create', 'Communities', 'Get Involved', 'Feed']:
                    menu = page.get_by_role('button', name=name, exact=True)
                    if menu.count() and menu.first.is_visible():
                        menu.first.click(timeout=10000)
                        collect()
                        page.keyboard.press('Escape')
                        page.get_by_role('menu').wait_for(state='hidden', timeout=10000)
            for count in range(MAX_EXPANSIONS):
                controls = page.get_by_role('button', name=re.compile(r'^(?:(?:show|load) more|(?:show|view|load|see|read)(?: (?:all|more|[0-9]+))* (?:repl(?:y|ies)|comments)|[0-9]+ (?:replies|comments))$', re.I))
                target = next((control for control in controls.all() if control.is_visible() and control.is_enabled()), None)
                if target is None:
                    break
                before_cursors = dict(state.get('pagination', {}).get(current, {}))
                advance(page, target)
                collect()
                checkpoint()
                if time.monotonic()-started >= BATCH_SECONDS and before_cursors == state.get('pagination', {}).get(current, {}):
                    raise ArchiveError('discovery_pagination_unresumable')
                if (count+1 >= EXPANSION_BATCH or time.monotonic()-started >= BATCH_SECONDS) and before_cursors != state.get('pagination', {}).get(current, {}) and any(c.is_visible() and c.is_enabled() for c in controls.all()):
                    page_complete = False
                    break
            else:
                raise ArchiveError('discovery_pagination_limit')
            if page_complete:
                seen_next = set()
                for count in range(MAX_EXPANSIONS):
                    candidates = [b for b in page.get_by_role('button', name=re.compile(r'^next(?: page)?$',re.I)).all() if b.is_visible() and b.is_enabled()]
                    if not candidates:
                        break
                    signature = page.evaluate(PROGRESS)
                    if signature in seen_next:
                        break
                    seen_next.add(signature)
                    before_cursors = dict(state.get('pagination', {}).get(current, {}))
                    advance(page, candidates[0])
                    collect()
                    checkpoint()
                    if time.monotonic()-started >= BATCH_SECONDS and before_cursors == state.get('pagination', {}).get(current, {}):
                        raise ArchiveError('discovery_pagination_unresumable')
                    if (count+1 >= EXPANSION_BATCH or time.monotonic()-started >= BATCH_SECONDS) and before_cursors != state.get('pagination', {}).get(current, {}):
                        page_complete = False
                        break
                else:
                    raise ArchiveError('discovery_pagination_limit')
            failures.pop(current,None)
            if page_complete:
                visited.add(current)
        except (ArchiveError, BrowserError) as error:
            code = error.code if isinstance(error,ArchiveError) else ('discovery_timeout' if isinstance(error,BrowserTimeout) else 'discovery_browser_failed')
            # API/storage failures belong to the run, never to a public page.
            if not code.startswith('discovery_') or code == 'discovery_page_limit':
                raise
            failure = failures.get(current, {})
            failures[current] = {'attempts': failure.get('attempts',0)+1, 'code':code}
            page_complete = False
        frontier.remove(current)
        if not page_complete and failures.get(current,{}).get('attempts',0)<3:
            frontier.append(current)
        checkpoint()
        time.sleep(1)
    checkpoint(exhausted=not frontier)
    return state


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
                result = discover(page, client, claimed.get('checkpoint'), claimed.get('canary', False))
                if not result['exhausted']:
                    client.call('yield')
            finally:
                browser.close()
        print('Public discovery completed' if result['complete'] else 'Public discovery checkpoint retained; coverage incomplete')
        return 0
    except Exception as error:
        code = error.code if isinstance(error,ArchiveError) and re.fullmatch(r'discovery_[a-z_]+',error.code) else 'discovery_runner_failed'
        if client and 'X-Archive-Lease' in client.headers:
            try:
                client.call('fail', {'code':code})
            except Exception:
                pass
        print('Public discovery incomplete; baseline preserved; '+code)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
