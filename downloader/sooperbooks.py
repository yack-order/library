"""Byte-preserving public Sooper Books archives, with publisher attribution."""
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urlsplit, quote, unquote

import requests
from archive_errors import ArchiveError
from archive_safety import safe_filename, create_verified_archive
from runner import digest
from restoration import image_info

LICENSE = 'https://creativecommons.org/licenses/by-nc-nd/4.0/'
MAX_TOTAL = 10 * 1024**3


def media_url(value):
    try:
        u = urlsplit(value)
        if (u.scheme != 'https' or u.netloc != 'media.sooperbooks.com' or u.query or u.fragment
                or re.search(r'[\x00-\x1f\x7f\\]', value) or '..' in unquote(u.path).split('/')):
            raise ValueError()
        return u._replace(path=quote(unquote(u.path), safe="/!$&'()*+,-.:;=@_")).geturl()
    except (ValueError, TypeError):
        raise ArchiveError('source_unavailable') from None


def download(url, path, image=False):
    limit = 16 * 1024**2 if image else 512 * 1024**2
    started = time.monotonic()
    try:
        with requests.get(media_url(url), stream=True, allow_redirects=False, timeout=(20, 60),
                          headers={'Referer': 'https://sooperbooks.com/', 'Accept-Encoding': 'identity'}) as response:
            mime = response.headers.get('Content-Type', '').split(';')[0].lower()
            if response.status_code != 200 or mime not in ({'image/png', 'image/jpeg', 'image/webp', 'image/gif'} if image else {'audio/mpeg', 'audio/mp3'}):
                raise ArchiveError('media_format_unsupported')
            declared = int(response.headers.get('Content-Length', '0'))
            if declared > limit:
                raise ArchiveError('resource_limit')
            count = 0
            with path.open('xb') as output:
                for chunk in response.iter_content(65536):
                    count += len(chunk)
                    if count > limit or time.monotonic() - started > 600:
                        raise ArchiveError('resource_limit')
                    output.write(chunk)
            if not count or declared and declared != count:
                raise ArchiveError('media_failed')
    except ArchiveError:
        path.unlink(missing_ok=True)
        raise
    except Exception:
        path.unlink(missing_ok=True)
        raise ArchiveError('media_failed') from None
    if image:
        extension, _ = image_info(path)
        renamed = path.with_suffix(extension)
        path.rename(renamed)
        return renamed
    return path


def build_sooperbooks_archive(source, work, fetch=download):
    manifest = source['sooperbooks']
    identity = source['publicCard']['cardId']
    if (not re.fullmatch(r'sooperbooks:[A-Za-z0-9_-]{1,184}', identity)
            or identity != 'sooperbooks:' + manifest['sourceId'] or manifest.get('license') != LICENSE):
        raise ArchiveError('identity_conflict')
    tracks = manifest['tracks']
    if not 1 <= len(tracks) <= 2000 or len({t['sourceId'] for t in tracks}) != len(tracks):
        raise ArchiveError('source_unavailable')
    with tempfile.TemporaryDirectory(prefix='sooperbooks-', dir=work) as temporary:
        folder = Path(temporary) / 'Sooper Books'
        audio = folder / 'audio_files'; audio.mkdir(parents=True)
        images = folder / 'images'; images.mkdir()
        total, duration = 0, 0
        saved_tracks, image_cache = [], {}

        def save_image(url):
            nonlocal total
            if not url:
                return None
            media_url(url)
            if url not in image_cache:
                dest = images / ('image-%03d' % (len(image_cache) + 1))
                path = fetch(url, dest, image=True)
                image_info(path)
                total += path.stat().st_size
                image_cache[url] = str(path.relative_to(folder))
            return image_cache[url]

        cover = save_image(manifest.get('cover'))
        for number, track in enumerate(tracks, 1):
            path = audio / ('%03d - %s.mp3' % (number, safe_filename(track['title'], limit=100)))
            fetch(track['url'], path)
            total += path.stat().st_size
            if total > MAX_TOTAL:
                raise ArchiveError('resource_limit')
            probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)],
                                   capture_output=True, text=True, timeout=30, check=True)
            seconds = float(json.loads(probe.stdout)['format']['duration'])
            if not math.isfinite(seconds) or seconds <= 0:
                raise ArchiveError('media_format_unsupported')
            duration += seconds
            saved_tracks.append({'sourceId': track['sourceId'], 'title': track['title'], 'order': track['order'],
                                 'file': str(path.relative_to(folder)), 'durationSeconds': seconds,
                                 'sourceUrl': track.get('sourceUrl'), 'cover': save_image(track.get('cover')),
                                 'yotoArtwork': save_image(track.get('yoto'))})
        # The provider namespace occupies the generic storage identity field. It is not a Yoto CardID.
        metadata = {'cardId': identity, 'source': 'sooperbooks', 'title': manifest['title'], 'sourceUrl': manifest['sourceUrl'],
                    'license': LICENSE, 'attribution': 'Sooper Books', 'cover': cover,
                    'content': {'chapters': [{'tracks': saved_tracks}]}}
        (folder / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        (folder / 'tracks.txt').write_text('\n'.join('%03d :: %s' % (n, t['title']) for n, t in enumerate(tracks, 1)), encoding='utf-8')
        (folder / 'ATTRIBUTION.txt').write_text('Stories, recordings and artwork by Sooper Books.\n' + manifest['sourceUrl'] + '\n'
            + 'Shared under Creative Commons Attribution-NonCommercial-NoDerivatives 4.0 International.\n'
            + LICENSE + '\nOriginal audio and artwork are preserved without modification. No endorsement is implied.\n', encoding='utf-8')
        files = {str(p.relative_to(folder)): {'bytes': p.stat().st_size, 'sha256': digest(p)}
                 for p in sorted(folder.rglob('*')) if p.is_file()}
        (folder / 'manifest.json').write_text(json.dumps({'version': 1, 'cardId': identity, 'files': files}, indent=2), encoding='utf-8')
        from datetime import datetime, timezone
        # Portable filename uses the provider ID; metadata retains its full namespace.
        filename = 'Sooper Books - %s [%s] (%s).7z' % (safe_filename(manifest['title'], limit=100), manifest['sourceId'], datetime.now(timezone.utc).strftime('%Y-%m-%d'))
        return Path(create_verified_archive(folder, Path(work) / filename, reproducible=True))
