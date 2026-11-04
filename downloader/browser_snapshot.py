"""Convert browser-observed publisher metadata into a bounded crawler snapshot.

The capture contains only content headings, paragraphs, images, and download
controls. Cookies, login fields, raw account HTML, and browser profiles are never
inputs. Browser navigation belongs to the operator's existing signed-in session.
"""
import argparse
from html import escape
import json
from pathlib import Path
import time
from momma_bear import Document, member_url, parse_entry
from runner import ArchiveError


def browser_snapshot(capture):
    initial = sorted(member_url(url) for url in capture['initialUrls'])
    final = sorted(member_url(url) for url in capture['finalUrls'])
    pages = capture['pages']
    urls = sorted(member_url(page['url']) for page in pages)
    if not initial or len(initial) > 2000 or len(set(urls)) != len(urls) or initial != final or initial != urls:
        raise ArchiveError('membership_library_changed')
    entries = []
    for page in sorted(pages, key=lambda page: page['url']):
        if page.get('password') or any(value in page.get('title', '').lower() for value in ('just a moment', 'verify you are human', 'checking your browser')):
            raise ArchiveError('membership_login_required')
        html = ''.join('<h1>' + escape(value) + '</h1>' for value in page['headings'])
        html += ''.join('<p>' + escape(value) + '</p>' for value in page['paragraphs'])
        html += ''.join('<img src="' + escape(value, quote=True) + '">' for value in page['images'])
        for link in page['links']:
            tag = 'button' if link['tag'] == 'button' else 'a'
            href = ' href="' + escape(link['url'], quote=True) + '"' if link['url'] else ''
            html += '<' + tag + href + '>' + escape(link['label']) + '</' + tag + '>'
        entries.append(parse_entry(Document(html).root, page['url']))
    return {'source': 'signed-in-browser', 'capturedAt': capture['capturedAt'],
            'initialUrls': initial, 'finalUrls': final, 'entries': entries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.capture.stat().st_size > 4 * 1024**2:
        raise ArchiveError('resource_limit')
    snapshot = browser_snapshot(json.loads(args.capture.read_text()))
    if not time.time() * 1000 - 86400000 <= snapshot['capturedAt'] <= time.time() * 1000 + 60000:
        raise ArchiveError('membership_library_changed')
    raw = json.dumps(snapshot)
    if len(raw.encode()) > 3.9 * 1024**2:
        raise ArchiveError('resource_limit')
    args.output.write_text(raw)
    args.output.chmod(0o600)
    print('Browser snapshot prepared: ' + str(len(snapshot['entries'])) + ' entries')


if __name__ == '__main__':
    main()
