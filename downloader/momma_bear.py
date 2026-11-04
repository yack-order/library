"""Paid Momma Bear discovery and MYO preparation. Logs contain status only."""
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit
import requests
from runner import ArchiveError, Client, public_url, digest
from momma_resources import fetch_resource, ordered_mp3s

ORIGIN = 'https://mommabearaudio.com'
DASHBOARD = ORIGIN + '/subscription/member-dashboard/'


class Node:
    def __init__(self, tag='', attrs=None, parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs or []), parent, []

    def text(self):
        return ' '.join(child.text() if isinstance(child, Node) else child for child in self.children).strip()

    def find(self, tag):
        return [node for child in self.children if isinstance(child, Node)
                for node in ([child] if child.tag == tag else []) + child.find(tag)]


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.current = node

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                break
            node = node.parent

    def handle_data(self, text):
        if self.current.tag not in {'script', 'style'}:
            self.current.children.append(text)


def member_url(value, base=ORIGIN):
    url = urljoin(base, value)
    p = urlsplit(url)
    if p.scheme != 'https' or p.netloc != 'mommabearaudio.com' or p.query or p.fragment or p.username or p.password:
        raise ArchiveError('member_link_invalid')
    return url


class Membership:
    def __init__(self):
        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        self.browser = self.runtime.chromium.launch(headless=True)
        self.context = self.browser.new_context()
        self.browser_page = self.context.new_page()

    def page(self, url):
        current = member_url(url)
        from playwright.sync_api import TimeoutError as BrowserTimeout
        try:
            response = self.browser_page.goto(current, wait_until='domcontentloaded', timeout=60000)
        except BrowserTimeout:
            raise ArchiveError('membership_http_failed') from None
        if re.search(r'just a moment|verify you are human|checking your browser', self.browser_page.title(), re.I):
            raise ArchiveError('membership_blocked')
        if not response or response.status != 200:
            raise ArchiveError('membership_http_failed')
        member_url(self.browser_page.url)
        text = self.browser_page.content()
        if len(text.encode()) > 8 * 1024**2:
            raise ArchiveError('resource_limit')
        return Document(text).root

    def login(self, credentials):
        print('Momma Bear: opening membership login')
        self.page(ORIGIN + '/login/')
        username = self.browser_page.get_by_label('Username', exact=True)
        password = self.browser_page.get_by_label('Password', exact=True)
        if username.count() != 1 or password.count() != 1:
            raise ArchiveError('membership_login_changed')
        print('Momma Bear: submitting membership login')
        username.fill(credentials['username'])
        password.fill(credentials['password'])
        self.browser_page.get_by_role('button', name='Log In', exact=True).click()
        from playwright.sync_api import TimeoutError as BrowserTimeout
        try:
            self.browser_page.wait_for_load_state('domcontentloaded', timeout=60000)
        except BrowserTimeout:
            raise ArchiveError('membership_http_failed') from None
        print('Momma Bear: verifying paid dashboard')
        return self.dashboard()

    def dashboard(self, url=DASHBOARD):
        page = self.page(url)
        if any(i.attrs.get('type') == 'password' for i in page.find('input')):
            raise ArchiveError('membership_login_required')
        if not any('all audio' in h.text().lower() for h in page.find('h2')):
            raise ArchiveError('membership_library_changed')
        return page

    def close(self):
        self.context.close()
        self.browser.close()
        self.runtime.stop()


def listing(page):
    # Content-card headings exclude navigation, account and support links.
    urls = sorted({member_url(a.attrs['href']) for h in page.find('h1') for a in h.find('a') if a.attrs.get('href')})
    if not urls:
        raise ArchiveError('membership_library_empty')
    pages = sorted({member_url(a.attrs['href']) for a in page.find('a')
                    if a.attrs.get('href') and (a.attrs.get('rel') == 'next' or re.fullmatch(r'Next(?: page)?|[0-9]+', a.text(), re.I))})
    return urls, pages


def parse_entry(page, url):
    headings = page.find('h1')
    if len(headings) != 1 or not headings[0].text():
        raise ArchiveError('entry_parse_failed')
    paragraphs = [p.text() for p in page.find('p') if p.text()]
    runtime = next((match.group(0).strip() for p in paragraphs
                    if (match := re.match(r'run\s*time(?:\s*total)?\s*:?\s*(?:\d+(?:[.:]\d+)?\s*(?:hours?|hrs?|minutes?|mins?|seconds?|secs?|h|m|s)\s*)+', p, re.I))), None)
    images = page.find('img')
    artwork = next((i.attrs.get('src') for i in images if i.attrs.get('src', '').startswith(ORIGIN + '/')), None)
    links = [(a.text(), urljoin(url, a.attrs.get('href', ''))) for a in page.find('a')]
    yoto = [href for label, href in links if re.search(r'yoto.*link', label, re.I) and urlsplit(href).hostname in {'share.yoto.co', 'yoto.io'}]
    mp3 = [href for label, href in links if re.search(r'mp3.*download', label, re.I)]
    supplements = [{'label': label, 'url': href} for label, href in links
                   if re.search(r'transcript|label|printable|document|\bmap\b|activity.*(?:book|sheet)', label, re.I)
                   and urlsplit(href).hostname in {'www.dropbox.com', 'dropbox.com', 'drive.google.com', 'mommabearaudio.com'}]
    no_yoto = (any(re.fullmatch('No Yoto Link', n.text(), re.I) for n in page.find('button') + page.find('a'))
               or bool(re.search(r'\bthere is no yoto link for this playlist\b', ' '.join(paragraphs), re.I)))
    printable_only = bool(re.search(r'not audio|digital download only', ' '.join(paragraphs), re.I)) and not yoto and not mp3
    if len(yoto) > 1 or len(mp3) > 1 or (not printable_only and not yoto and not no_yoto):
        raise ArchiveError('entry_links_changed')
    if any(i.attrs.get('type') == 'password' for i in page.find('input')):
        raise ArchiveError('membership_login_required')
    return {'url': url, 'title': headings[0].text(), 'description': '\n\n'.join(paragraphs),
            'runtime': runtime, 'artwork': artwork, 'yoto': yoto[0] if yoto else None,
            'noYoto': no_yoto, 'printableOnly': printable_only, 'mp3': mp3[0] if mp3 else None,
            'supplements': list({r['url']: r for r in supplements}.values())}


class MommaClient(Client):
    def __init__(self):
        super().__init__()
        self.base = self.base.replace('/archive/jobs/', '/momma/jobs/')

    def heartbeats(self):
        # A single transient heartbeat failure must not abandon lease renewal
        # during a long download or Yoto transcode. The API still rejects an
        # expired/replaced lease at every mutation boundary.
        while not self.stopped.wait(60):
            try:
                self.call('heartbeat', {'stage': self.stage})
            except ArchiveError as error:
                if error.code == 'job_cancelled':
                    self.lost.set()
                    return
            except requests.RequestException:
                pass


def yoto_json(client, entry, path):
    token = client.call('yoto-token', {'url': entry['url']})['token']
    with requests.get('https://api.yotoplay.com' + path, headers={'Authorization': 'Bearer ' + token},
                      timeout=(10, 45), allow_redirects=False, stream=True) as response:
        if response.status_code in {202, 404} and path.startswith('/media/upload/'):
            return {'transcode': {}}
        if not response.ok:
            raise ArchiveError('yoto_upload_authorization_required')
        raw = bytearray()
        for chunk in response.iter_content(65536):
            raw.extend(chunk)
            if len(raw) > 4 * 1024**2:
                raise ArchiveError('resource_limit')
        return json.loads(raw)


def create_playlist(client, entry, work):
    files = ordered_mp3s(fetch_resource(entry['mp3'], work / 'audio', audio=True))
    chapters = []
    for number, path in enumerate(files, 1):
        sha = digest(path)
        data = yoto_json(client, entry, '/media/transcode/audio/uploadUrl?sha256=' + sha)['upload']
        if data.get('uploadUrl'):
            target = public_url(data['uploadUrl'])
            with path.open('rb') as audio, requests.put(target, data=audio, headers={'Content-Type': 'audio/mpeg'},
                                                      timeout=(10, 180), allow_redirects=False) as uploaded:
                if not uploaded.ok:
                    raise ArchiveError('yoto_audio_upload_failed')
        upload_id = str(data['uploadId'])
        if not re.fullmatch(r'[A-Za-z0-9_-]+', upload_id):
            raise ArchiveError('yoto_audio_upload_failed')
        transcode = None
        for _ in range(180):
            result = yoto_json(client, entry, '/media/upload/' + upload_id + '/transcoded?loudnorm=false').get('transcode', {})
            if result.get('transcodedSha256'):
                transcode = result
                break
            time.sleep(2)
        if not transcode:
            raise ArchiveError('yoto_transcode_timeout')
        info = transcode['transcodedInfo']
        title = (info.get('metadata') or {}).get('title') or re.sub(r'^\d+[\s._-]*', '', path.stem)
        key = f'{number:02d}'
        track = {'key': key, 'title': title, 'trackUrl': 'yoto:#' + transcode['transcodedSha256'],
                 'duration': info['duration'], 'fileSize': info['fileSize'], 'channels': info.get('channels', 2),
                 'format': info.get('format', 'mp3'), 'type': 'audio', 'overlayLabel': str(number)}
        chapters.append({'key': key, 'title': title, 'overlayLabel': str(number), 'tracks': [track], 'display': {'icon16x16': None}})
    marker = hashlib.sha256(entry['url'].encode()).hexdigest()
    content = {'title': entry['title'], 'content': {'chapters': chapters},
               'metadata': {'author': 'Momma Bear Audio', 'description': entry['description'] + '\nToyo Momma Bear source: ' + marker}}
    if entry.get('artwork'):
        content['metadata']['cover'] = {'imageL': entry['artwork']}
    return client.call('create', {'url': entry['url'], 'content': content})


def discover(membership, client, login):
    first = membership.login(login)
    print('Momma Bear: membership verified; discovering library')
    urls, following = listing(first)
    frontier, visited = list(following), {DASHBOARD}
    while frontier:
        url = frontier.pop(0)
        if url in visited:
            continue
        if len(visited) >= 100 or len(urls) > 2000:
            raise ArchiveError('resource_limit')
        found, more = listing(membership.dashboard(url))
        urls = sorted(set(urls) | set(found))
        visited.add(url)
        frontier.extend(p for p in more if p not in visited)
        client.call('heartbeat')
        time.sleep(1)
    entries = []
    for url in urls:
        entries.append(parse_entry(membership.page(url), url))
        client.call('heartbeat')
        time.sleep(1)
    # Detect changes during the scan before replacing a comparison baseline.
    if listing(membership.dashboard())[0] != listing(first)[0]:
        raise ArchiveError('membership_library_changed')
    client.call('discovered', entries)
    return entries


def main():
    client = None
    try:
        client = MommaClient()
        claimed = client.call('claim')
        client.headers['X-Archive-Lease'] = claimed['leaseToken']
        threading.Thread(target=client.heartbeats, daemon=True).start()
        if 'entries' in claimed:
            entries = claimed.pop('entries')
            client.call('discovered', entries)
            print('Momma Bear: processing signed-in browser snapshot')
        else:
            membership = Membership()
            try:
                entries = discover(membership, client, claimed.pop('login'))
            finally:
                membership.close()
        mode = claimed['mode']
        if mode != 'full':
            entries = [e for e in entries if e['yoto']] if mode == 'existing' else [e for e in entries if not e['yoto'] and not e['printableOnly']]
            entries = entries[:1]
        for entry in entries:
            for attempt in range(3):
                try:
                    result = client.call('entry', {'url': entry['url']})
                    if result['state'] == 'needs_playlist':
                        with tempfile.TemporaryDirectory(prefix='toyo-momma-') as directory:
                            create_playlist(client, entry, Path(directory))
                        client.call('entry', {'url': entry['url']})
                    break
                except Exception as error:
                    if attempt == 2:
                        client.call('entry-failed', {'url': entry['url'], 'code': error.code if isinstance(error, ArchiveError) else 'crawl_incomplete'})
                    else:
                        time.sleep(2 ** attempt)
        client.call('finish')
        print('Momma Bear discovery and import preparation completed')
        return 0
    except Exception as error:
        if client and 'X-Archive-Lease' in client.headers:
            try:
                client.call('fail', {'code': error.code if isinstance(error, ArchiveError) else 'crawl_incomplete'})
            except Exception:
                pass
        print('Momma Bear run incomplete; inspect private run status')
        return 1
    finally:
        if client:
            client.stopped.set()


if __name__ == '__main__':
    raise SystemExit(main())
