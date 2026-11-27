"""One-shot website interoperability experiment; never bypasses browser challenges."""
import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from urllib.parse import urlsplit
import zipfile

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

DEFAULT_PLAYLIST = 'https://open.spotify.com/playlist/7jlh29r2ASMKfqZKgEAuHz'
MAX_BYTES = 20 * 1024**3
PROVIDER_SECONDS = 120 * 60
AUDIO = {'.mp3', '.m4a', '.aac', '.flac', '.wav', '.ogg', '.opus'}


class POCError(Exception):
    pass


def playlist_url(value):
    p = urlsplit(value.strip())
    match = re.fullmatch(r'/(?:intl-[a-z-]+/)?playlist/([A-Za-z0-9]{22})/?', p.path)
    if p.scheme != 'https' or p.netloc != 'open.spotify.com' or not match:
        raise POCError('invalid_playlist_url')
    return 'https://open.spotify.com/playlist/' + match[1]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def digest(path, algorithm='sha256'):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, algorithm).hexdigest()


def title_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def filename(value):
    return re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', value).strip(' .')[:140] or 'track'


def audio_info(path):
    if path.stat().st_size <= 0 or path.stat().st_size > MAX_BYTES:
        raise POCError('invalid_audio_size')
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
                             '-of', 'json', str(path)], capture_output=True, timeout=60)
    try:
        data = json.loads(result.stdout)
        streams = [s for s in data['streams'] if s.get('codec_type') == 'audio']
        duration = float(data['format']['duration'])
        if result.returncode or not streams or duration <= 0:
            raise ValueError()
        tags = data['format'].get('tags', {})
        return {'bytes': path.stat().st_size, 'sha256': digest(path),
                'durationSeconds': duration, 'codec': streams[0].get('codec_name'),
                'tags': {k: str(v)[:500] for k, v in tags.items()
                         if k.lower() in {'title', 'artist', 'album', 'track'}}}
    except (KeyError, ValueError, TypeError):
        raise POCError('invalid_audio') from None


def inspect_zip(path):
    if not zipfile.is_zipfile(path):
        raise POCError('invalid_archive')
    entries, reports = [], []
    with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory() as tmp:
        members = archive.infolist()
        if len(members) > 10000 or sum(m.file_size for m in members) > MAX_BYTES:
            raise POCError('archive_resource_limit')
        for member in members:
            p = PurePosixPath(member.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in member.filename or member.flag_bits & 1:
                raise POCError('unsafe_archive')
            if member.is_dir():
                continue
            if p.suffix.lower() in AUDIO:
                target = Path(tmp) / ('audio' + p.suffix.lower())
                with archive.open(member) as source, target.open('wb') as dest:
                    shutil.copyfileobj(source, dest, 1024 * 1024)
                entries.append({'filename': member.filename, **audio_info(target)})
                target.unlink()
            elif p.suffix.lower() in {'.txt', '.json', '.csv'} and member.file_size < 1024**2:
                # Keep the provider's skipped-track report in the native archive;
                # do not copy arbitrary text or potential URLs into public logs.
                reports.append(member.filename)
        if not entries:
            raise POCError('archive_contains_no_audio')
    return {'audio': entries, 'reportFiles': reports}


def package_spotdown(tracks, downloads, destination):
    """Map completion-order downloads back to playlist order, never guessing collisions."""
    groups = {}
    for download in downloads:
        key = title_key(re.sub(r'_spotdown\.org$', '', Path(download['suggested']).stem))
        groups.setdefault(key, []).append(download)
    identities = {}
    for track in tracks:
        identities.setdefault(title_key(track['title']), set()).add(track['url'])
    manifest, used, unassigned = [], set(), 0
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_STORED) as archive:
        for i, track in enumerate(tracks, 1):
            entry = {'position': i, **track}
            candidates = groups.get(title_key(track['title']), [])
            if len(identities[title_key(track['title'])]) > 1:
                entry['failure'] = 'ambiguous_same_title'
            elif not candidates:
                entry['failure'] = 'too_long' if track.get('tooLong') else 'not_downloaded'
            else:
                downloaded = candidates[0]
                try:
                    info = audio_info(downloaded['path'])
                    name = f"{i:04d} - {filename(track['title'])}.mp3"
                    archive.write(downloaded['path'], name)
                    used.add(downloaded['path'])
                    entry.update(filename=name, **info)
                except POCError as exc:
                    entry['failure'] = str(exc)
            manifest.append(entry)
        for i, download in enumerate(downloads, 1):
            if download['path'] not in used:
                try:
                    audio_info(download['path'])
                    archive.write(download['path'], f"unassigned/{i:04d}.mp3")
                    unassigned += 1
                except POCError:
                    pass
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    count = sum('filename' in item for item in manifest)
    return {'downloadedTracks': count, 'expectedTracks': len(tracks),
            'unassignedFiles': unassigned,
            'status': 'complete' if tracks and count == len(tracks) else 'partial',
            'tracks': manifest}


def upload_gofile(path, token):
    if not token:
        raise POCError('gofile_token_missing')
    handle = path.open('rb')
    body = MultipartEncoder(fields={'file': (path.name, handle, 'application/zip')})
    try:
        # Never retry an ambiguous upload: it may already have been accepted.
        with requests.post('https://upload.gofile.io/uploadfile', data=body,
                           headers={'Authorization': 'Bearer ' + token,
                                    'Content-Type': body.content_type},
                           timeout=(15, 600), allow_redirects=False) as response:
            data = response.json()
            if response.status_code != 200 or data.get('status') != 'ok':
                raise POCError('gofile_upload_rejected')
            data = data['data']
        page = urlsplit(data['downloadPage'])
        if page.scheme != 'https' or page.netloc != 'gofile.io' or not re.fullmatch(r'/d/[A-Za-z0-9_-]+', page.path) or page.query or page.fragment:
            raise POCError('gofile_invalid_receipt')
        if data.get('size') != path.stat().st_size or data.get('md5', '').lower() != digest(path, 'md5'):
            raise POCError('gofile_receipt_mismatch')
        return {'pageUrl': data['downloadPage'], 'contentId': data['id'],
                'bytes': data['size'], 'sha256': digest(path), 'verification': 'upload-receipt'}
    except POCError:
        raise
    except Exception:
        raise POCError('gofile_upload_uncertain') from None
    finally:
        handle.close()


async def verify_gofile(browser, receipt, original, directory):
    context = await browser.new_context(accept_downloads=True)
    try:
        page = await context.new_page()
        await page.goto(receipt['pageUrl'], wait_until='domcontentloaded', timeout=60000)
        await page.get_by_text(original.name, exact=True).wait_for(timeout=60000)
        button = page.get_by_role('button', name='Download', exact=True)
        if await button.count() != 1:
            raise POCError('gofile_readback_ambiguous')
        async with page.expect_download(timeout=600000) as pending:
            await button.click()
        download = await pending.value
        if download.suggested_filename != original.name:
            raise POCError('gofile_readback_filename_mismatch')
        copy = directory / 'gofile-readback.zip'
        await download.save_as(copy)
        if copy.stat().st_size != receipt['bytes'] or digest(copy) != receipt['sha256']:
            raise POCError('gofile_readback_mismatch')
        copy.unlink()
        receipt['verification'] = 'sha256-readback'
    finally:
        await context.close()


def clean_tracks(data, provider):
    items = data.get('songs' if provider == 'spotdown' else 'tracks', [])
    tracks = []
    if not isinstance(items, list):
        raise POCError('invalid_metadata')
    for item in items:
        url = item.get('url', '') if provider == 'spotdown' else 'https://open.spotify.com/track/' + str(item.get('id', ''))
        if not re.fullmatch(r'https://open\.spotify\.com/track/[A-Za-z0-9]{22}', url):
            raise POCError('unsupported_track_metadata')
        tracks.append({'title': str(item.get('title' if provider == 'spotdown' else 'name', ''))[:500],
                       'artist': str(item.get('artist', ''))[:500], 'url': url,
                       'tooLong': bool(item.get('tooLong', False))})
    return tracks


class WebsiteRun:
    def __init__(self, provider, directory):
        self.provider, self.directory = provider, directory
        self.tracks, self.downloads, self.events, self.tasks = [], [], [], set()
        self.metadata_seen = False
        self.counter = 0
        self.rate_limited = False
        self.batch_finished = False
        self.failure = None

    def schedule(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def response(self, response):
        parsed = urlsplit(response.url)
        # Only retain named application routes. Never log arbitrary signed paths.
        routes = {'/apinew/song-details', '/apinew/download', '/v1/resolve', '/v1/download'}
        if parsed.path not in routes or parsed.hostname not in {'spotdown.org', 'api.spotidown.cc'}:
            return
        self.events.append({'host': parsed.hostname, 'route': parsed.path, 'status': response.status})
        if response.status == 429:
            self.rate_limited = True
        if response.ok and parsed.path in {'/apinew/song-details', '/v1/resolve'}:
            try:
                self.tracks = clean_tracks(await response.json(), self.provider)
                self.metadata_seen = bool(self.tracks)
            except Exception:
                self.failure = 'invalid_metadata'

    def request_failed(self, request):
        parsed = urlsplit(request.url)
        if parsed.hostname in {'spotdown.org', 'api.spotidown.cc'}:
            failure = request.failure or ''
            code = 'backend_dns_failure' if 'NAME_NOT_RESOLVED' in failure else 'network_failure'
            self.events.append({'host': parsed.hostname, 'status': code})
            if parsed.hostname == 'api.spotidown.cc' and code == 'backend_dns_failure':
                self.failure = code

    async def save(self, download):
        self.counter += 1
        index = self.counter
        suffix = Path(download.suggested_filename).suffix.lower()
        if suffix not in AUDIO | {'.zip'}:
            await download.cancel()
            self.events.append({'download': index, 'status': 'unsupported_file_type'})
            return
        target = self.directory / f'download-{index:04d}{suffix}'
        try:
            await download.save_as(target)
            if target.stat().st_size > MAX_BYTES:
                target.unlink()
                raise POCError('download_resource_limit')
            self.downloads.append({'path': target, 'suggested': download.suggested_filename})
            self.events.append({'download': index, 'status': 'saved', 'bytes': target.stat().st_size})
        except Exception:
            self.events.append({'download': index, 'status': 'failed'})

    async def execute(self, page, url):
        site = 'https://spotdown.org/en3/playlist' if self.provider == 'spotdown' else 'https://spotidown.cc/spotify-playlist-downloader/'
        await page.goto(site, wait_until='domcontentloaded', timeout=60000)
        selector = '#search-form-input' if self.provider == 'spotdown' else '[data-tool-input]'
        try:
            await page.locator(selector).wait_for(timeout=60000)
        except Exception:
            raise POCError('challenge_or_page_unavailable') from None
        await page.locator(selector).fill(url)
        # Spotdown filling triggers a debounced lookup already. Avoid duplicate submissions.
        if self.provider != 'spotdown':
            await page.locator('[data-tool-submit]').click()
        deadline = time.monotonic() + 120
        while not self.metadata_seen:
            if self.rate_limited:
                raise POCError('rate_limited')
            if self.failure:
                raise POCError(self.failure)
            if time.monotonic() > deadline:
                text = (await page.locator('body').inner_text()).lower()
                if re.search(r'verify you are human|captcha|access denied|just a moment', text):
                    raise POCError('challenge_blocked')
                raise POCError('metadata_unavailable')
            await asyncio.sleep(1)
        if self.provider == 'spotdown':
            await page.locator('#download-now-all-button').click()
            await page.locator('#download-all-button').click()
            cancel = page.locator('#cancel-download-button')
            # Batch ends when the real cancel control is hidden.
            while await cancel.is_visible():
                if self.rate_limited:
                    raise POCError('rate_limited')
                await asyncio.sleep(1)
            self.batch_finished = True
        else:
            await page.locator('[data-format="mp3"]').click()
            button = page.locator('[data-zip]')
            if not await button.count() and len(self.tracks) == 1:
                button = page.locator('[data-one="0"]')
            await button.click()
            deadline = time.monotonic() + PROVIDER_SECONDS
            while not self.downloads:
                if self.rate_limited:
                    raise POCError('rate_limited')
                if await page.locator('[data-tool-status][data-state="error"]').count():
                    raise POCError('provider_download_failed')
                if time.monotonic() > deadline:
                    raise POCError('download_timeout')
                await asyncio.sleep(1)
            self.batch_finished = True


async def run_provider(browser, provider, url, root, token):
    directory = root / provider
    directory.mkdir(parents=True, exist_ok=True)
    work = root / 'private' / provider
    work.mkdir(parents=True, exist_ok=True)
    state = WebsiteRun(provider, work)
    context = await browser.new_context(accept_downloads=True)
    page = await context.new_page()
    page.on('popup', lambda popup: state.schedule(popup.close()))
    page.on('response', lambda response: state.schedule(state.response(response)))
    page.on('requestfailed', state.request_failed)
    page.on('download', lambda download: state.schedule(state.save(download)))
    result = {'provider': provider, 'status': 'failed', 'expectedTracks': 0, 'downloadedTracks': 0}
    try:
        print(f'{provider}: starting website test', flush=True)
        try:
            await asyncio.wait_for(state.execute(page, url), PROVIDER_SECONDS)
        except POCError as exc:
            state.failure = str(exc)
        except asyncio.TimeoutError:
            state.failure = 'provider_timeout'
        except Exception:
            state.failure = 'browser_operation_failed'
        if state.tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*list(state.tasks), return_exceptions=True), 60)
            except asyncio.TimeoutError:
                state.failure = state.failure or 'download_save_timeout'
        try:
            await page.screenshot(path=str(directory / 'website.png'), mask=[page.locator('input')], timeout=10000)
        except Exception:
            pass
        result['expectedTracks'] = len(state.tracks)
        if state.failure:
            result['reason'] = state.failure
            result['status'] = 'blocked' if state.failure in {'challenge_blocked', 'challenge_or_page_unavailable', 'rate_limited', 'metadata_unavailable', 'backend_dns_failure'} else 'failed'
        archive = work / f'{provider}-{url.rsplit("/", 1)[1]}.zip'
        if state.downloads and (provider == 'spotdown' or
                               (len(state.tracks) == 1 and state.downloads[0]['path'].suffix in AUDIO)):
            packaged = await asyncio.to_thread(package_spotdown, state.tracks, state.downloads, archive)
            result.update({key: value for key, value in packaged.items() if key != 'tracks'})
            write_json(directory / 'tracks.json', packaged['tracks'])
        elif provider == 'spotidown' and state.downloads:
            if len(state.downloads) != 1 or state.downloads[0]['path'].suffix != '.zip':
                raise POCError('unexpected_native_archive')
            shutil.copyfile(state.downloads[0]['path'], archive)
            info = await asyncio.to_thread(inspect_zip, archive)
            result.update(downloadedTracks=len(info['audio']), reportFiles=info['reportFiles'])
            result['status'] = 'complete' if len(info['audio']) == len(state.tracks) and not info['reportFiles'] else 'partial'
            write_json(directory / 'audio.json', info)
        if archive.exists() and (result['downloadedTracks'] or result.get('unassignedFiles')):
            if state.failure:
                result['status'] = 'partial'
            result.update(bytes=archive.stat().st_size, sha256=digest(archive))
            receipt = await asyncio.to_thread(upload_gofile, archive, token)
            result['gofile'] = receipt
            try:
                await asyncio.wait_for(verify_gofile(browser, receipt, archive, work), 720)
            except Exception:
                result['deliveryWarning'] = 'gofile_readback_not_verified'
        elif not state.failure:
            result.update(status='failed', reason='no_valid_audio_downloaded')
    except POCError as exc:
        result.update(status='failed', reason=str(exc))
    except Exception:
        result.update(status='failed', reason='packaging_or_delivery_failed')
    finally:
        await context.close()
        write_json(directory / 'metadata.json', {'tracks': state.tracks})
        write_json(directory / 'events.json', state.events)
        write_json(directory / 'result.json', result)
        shutil.rmtree(work, ignore_errors=True)
    print(f"{provider}: {result['status']}; {result['downloadedTracks']}/{result['expectedTracks']} tracks", flush=True)
    return result


async def main():
    from playwright.async_api import async_playwright
    url = playlist_url(os.environ.get('PLAYLIST_URL', DEFAULT_PLAYLIST))
    selected = os.environ.get('PROVIDER', 'both')
    if selected not in {'both', 'spotdown', 'spotidown'}:
        raise POCError('invalid_provider')
    token = os.environ.get('GOFILE_TOKEN', '')
    if not token:
        raise POCError('gofile_token_missing')
    root = Path(os.environ.get('POC_OUTPUT', 'spotify-poc-results')).resolve()
    root.mkdir(parents=True, exist_ok=True)
    providers = ['spotdown', 'spotidown'] if selected == 'both' else [selected]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            results = []
            for provider in providers:
                results.append(await run_provider(browser, provider, url, root, token))
        finally:
            await browser.close()
    write_json(root / 'results.json', results)
    summary = ['# Spotify website proof of concept', '', '| Provider | Outcome | Tracks | Delivery |', '|---|---|---|---|']
    for item in results:
        receipt = item.get('gofile', {})
        delivery = f"[Gofile]({receipt['pageUrl']}) ({receipt['verification']})" if receipt else 'No archive uploaded'
        summary.append(f"| {item['provider']} | {item['status']} | {item['downloadedTracks']}/{item['expectedTracks']} | {delivery} |")
        if item.get('reason'):
            summary.append(f"\n{item['provider']}: `{item['reason']}`\n")
        if item.get('deliveryWarning'):
            summary.append(f"\n{item['provider']}: `{item['deliveryWarning']}`\n")
    summary.append('\nTrack identity and source quality are not established by metadata or filenames.\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as handle:
            handle.write('\n'.join(summary))
    return 0 if all(r['status'] == 'complete' and r.get('gofile', {}).get('verification') == 'sha256-readback' for r in results) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(asyncio.run(main()))
    except POCError as exc:
        print(str(exc), flush=True)
        raise SystemExit(1)
