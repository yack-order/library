"""Headless archive worker adapted from YOTO-json-extractor-v2 (MIT).

Only opaque job IDs belong in public logs. All private inputs remain in memory
or the runner's owned temporary directory, never Actions artifacts or caches.
"""
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit
from datetime import datetime, timezone

import requests
from requests_toolbelt import MultipartEncoder
from PIL import Image
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TCON, TRCK, APIC, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.aac import AAC
from archive_safety import safe_filename, create_verified_archive
from privacy import register_sensitive_values, scrub_json, redact_text


class ArchiveError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(path):
    with open(path, 'rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def public_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.port not in (None, 443) or p.username or p.password:
        raise ArchiveError('media_failed')
    addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0].split('%')[0]).is_global for a in addresses):
        raise ArchiveError('media_failed')
    return url


def stream_file(url, destination, *, maximum=1024**3, allowed=None, attempts=3, bearer=None):
    """Bounded streaming with redirect validation; never forward credentials."""
    for attempt in range(attempts):
        try:
            current = public_url(url)
            started = time.monotonic()
            with requests.Session() as session:
                session.trust_env = False
                for redirects in range(4):
                    headers = {'Accept-Encoding': 'identity'}
                    if bearer:
                        host = urlsplit(current).hostname
                        if host != 'gofile.io' and not host.endswith('.gofile.io'):
                            raise ArchiveError('verification_failed')
                        headers['Authorization'] = f'Bearer {bearer}'
                    with session.get(current, stream=True, allow_redirects=False,
                                     timeout=(10, 30), headers=headers) as response:
                        if response.is_redirect:
                            if redirects == 3:
                                raise ArchiveError('media_failed')
                            current = public_url(urljoin(current, response.headers['Location']))
                            continue
                        if response.status_code != 200:
                            raise ArchiveError('media_failed')
                        mime = response.headers.get('Content-Type', '').split(';')[0].lower()
                        if allowed and mime not in allowed:
                            raise ArchiveError('media_failed')
                        # Radio streams have no finite source and must not be archived.
                        if 'icy-name' in response.headers or 'icy-metaint' in response.headers:
                            raise ArchiveError('media_failed')
                        declared = int(response.headers.get('Content-Length', 0))
                        if declared > maximum:
                            raise ArchiveError('media_failed')
                        size = 0
                        with open(destination, 'wb') as handle:
                            for chunk in response.iter_content(65536):
                                size += len(chunk)
                                if size > maximum or time.monotonic() - started > 600:
                                    raise ArchiveError('media_failed')
                                handle.write(chunk)
                        if not size or (declared and declared != size):
                            raise ArchiveError('media_failed')
                        return mime
                raise ArchiveError('media_failed')
        except Exception:
            Path(destination).unlink(missing_ok=True)
            if attempt == attempts - 1:
                raise ArchiveError('media_failed') from None
            time.sleep(2 ** attempt)


def strip_source_tags(path):
    # Remux compressed audio without transcoding; drop all provider metadata,
    # chapters, attachments, and embedded artwork before adding scrubbed tags.
    clean = path.with_name('clean-' + path.name)
    try:
        subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(path),'-map','0:a:0',
                        '-map_metadata','-1','-map_chapters','-1','-c:a','copy',str(clean)],
                       check=True,timeout=180,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        clean.replace(path)
    except Exception:
        raise ArchiveError('media_failed') from None


def tag_audio(path, title, author, album, category, number, total, cover):
    """Preserve published credits, chapter order, and cover without source URLs."""
    if path.suffix == '.mp3':
        try:
            tags = ID3(path)
            tags.clear()
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(TIT2(encoding=3, text=title)); tags.add(TPE1(encoding=3, text=author))
        tags.add(TALB(encoding=3, text=album)); tags.add(TCON(encoding=3, text=category))
        tags.add(TRCK(encoding=3, text=f'{number}/{total}'))
        if cover:
            tags.add(APIC(encoding=3, mime='image/png', type=3, desc='Cover', data=cover))
        tags.save(path)
    elif path.suffix == '.m4a':
        audio = MP4(path)
        audio.delete()
        audio['\xa9nam'] = [title]; audio['\xa9ART'] = [author]; audio['\xa9alb'] = [album]
        audio['\xa9gen'] = [category]; audio['trkn'] = [(number, total)]
        if cover:
            audio['covr'] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_PNG)]
        audio.save()
    elif path.suffix == '.aac':
        # Raw ADTS AAC cannot embed MP4 tags. Keep native audio plus tracks.txt.
        AAC(path)


def normalized_image(url, path):
    temp = path.with_suffix('.download')
    stream_file(url, temp, maximum=16*1024**2,
                allowed={'image/png', 'image/jpeg', 'image/webp', 'image/gif'})
    try:
        with Image.open(temp) as image:
            clean=image.convert('RGBA');clean.info.clear();clean.save(path, format='PNG')
    finally:
        temp.unlink(missing_ok=True)


def build_archive(source, work):
    raw = source['card']
    register_sensitive_values(raw)
    card = scrub_json(source['publicCard'])
    card_id = card['cardId']
    if not re.fullmatch(r'[A-Za-z0-9]{5}', card_id):
        raise ArchiveError('source_unavailable')
    title = safe_filename(card.get('title'), limit=120)
    metadata = card.get('metadata') or {}
    category = safe_filename(metadata.get('category'), fallback='MYO', limit=40)
    author = str(metadata.get('author') or 'MYO')
    folder = work / title; folder.mkdir()
    audio_dir = folder / 'audio_files'; audio_dir.mkdir()
    images = folder / 'images'; images.mkdir()
    raw_chapters = (raw.get('content') or {}).get('chapters') or []
    chapters = (card.get('content') or {}).get('chapters') or []
    raw_tracks = [(c, t) for c in raw_chapters for t in c.get('tracks', [])]
    tracks = [t for c in chapters for t in c.get('tracks', [])]
    if not tracks or len(tracks) != len(raw_tracks) or len(tracks) > 2000:
        raise ArchiveError('media_failed')
    if str((raw.get('content') or {}).get('playbackType', '')).lower() in {'radio', 'live', 'stream'}:
        raise ArchiveError('media_failed')
    cover_path = images / 'cover.png'
    cover_url = ((raw.get('metadata') or {}).get('cover') or {}).get('imageL')
    if cover_url:
        normalized_image(cover_url, cover_path)
    cover = cover_path.read_bytes() if cover_path.exists() else None
    (folder / f'{title}.json').write_text(json.dumps(card, indent=2), encoding='utf-8')
    details = {'Title': card.get('title'), 'Author': author, 'CardID': card_id,
               'Description': metadata.get('description'), 'Category': metadata.get('category'),
               'Languages': metadata.get('languages'), 'Version': card.get('content', {}).get('version'),
               'CreatedAt': card.get('createdAt'), 'UpdatedAt': card.get('updatedAt')}
    (folder / 'card.txt').write_text('\n'.join(f'{k}:: {redact_text(v)}' for k,v in details.items() if v is not None)+'\n', encoding='utf-8')
    lines = []; padding = len(str(len(tracks)))
    formats = {'audio/mpeg': '.mp3', 'audio/mp3': '.mp3', 'audio/aac': '.aac',
               'audio/aacp': '.aac', 'audio/mp4': '.m4a', 'audio/x-m4a': '.m4a'}
    for number, (track, (chapter, private)) in enumerate(zip(tracks, raw_tracks), 1):
        if private.get('type') in {'radio', 'live'} or not private.get('trackUrl'):
            raise ArchiveError('media_failed')
        track_title = str(track.get('title') or f'Track {number}')
        prefix = f'{number:0{padding}d}'
        temporary = audio_dir / (prefix + '.download')
        mime = stream_file(private['trackUrl'], temporary, allowed=set(formats))
        audio = audio_dir / safe_filename(f'{prefix} - {track_title}', limit=160)
        audio = audio.with_name(audio.name + formats[mime]); temporary.rename(audio)
        strip_source_tags(audio)
        tag_audio(audio, track_title, author, str(card.get('title',title)), category, number, len(tracks), cover)
        display = private.get('display') or chapter.get('display') or {}
        if display.get('icon16x16'):
            normalized_image(display['icon16x16'], images / f'{prefix}.png')
        lines.append('\n'.join(f'{k}:: {redact_text(v)}' for k,v in {
            'TrackNumber':prefix,'Title':track_title,'Duration':track.get('duration'),
            'FileSize':track.get('fileSize'),'Channels':track.get('channels'),'Type':track.get('type')}.items() if v is not None))
        if sum(p.stat().st_size for p in folder.rglob('*') if p.is_file()) > 10*1024**3:
            raise ArchiveError('media_failed')
    (folder / 'tracks.txt').write_text('\n\n'.join(lines)+'\n', encoding='utf-8')
    manifest = {str(p.relative_to(folder)): {'sha256':digest(p),'bytes':p.stat().st_size}
                for p in sorted(folder.rglob('*')) if p.is_file()}
    (folder / 'manifest.json').write_text(json.dumps({'version':1,'cardId':card_id,'files':manifest},indent=2),encoding='utf-8')
    demo = '[DEMO]' if re.search(r'Discover:\s*\d+\s+tracks?', str(metadata.get('description',''))+' '+str(card.get('title',''))) else ''
    filename = f'{category} - {title}{demo}[{card_id}] ({datetime.now(timezone.utc):%Y-%m-%d}).7z'
    return Path(create_verified_archive(folder, work / filename))


class Client:
    def __init__(self):
        base = os.environ['TOYO_ARCHIVE_API'].rstrip('/')
        if base != 'https://ui.yackorder.org':
            raise ArchiveError('configuration_failed')
        job = os.environ['TOYO_ARCHIVE_JOB']
        if not re.fullmatch(r'[a-f0-9-]{36}',job):
            raise ArchiveError('configuration_failed')
        token = os.environ['TOYO_ARCHIVE_RUNNER_TOKEN']
        if not token:
            raise ArchiveError('configuration_failed')
        self.base = f'{base}/api/internal/archive/jobs/{job}'
        self.headers = {'Authorization':f'Bearer {token}'}
        self.stage = 'fetching'
        self.stopped = threading.Event()
        self.lost = threading.Event()

    def call(self, action, data=None):
        with requests.post(f'{self.base}/{action}', json=data or {}, headers=self.headers,
                           timeout=(10,120), allow_redirects=False) as response:
            if response.status_code != 200:
                raise ArchiveError('reconnect_required' if action=='source' and response.status_code==409 else 'source_unavailable')
            return response.json()

    def heartbeats(self):
        while not self.stopped.wait(60):
            try:
                self.call('heartbeat', {'stage':self.stage})
            except Exception:
                self.lost.set(); return


def provider_error(status):
    # Only log controlled codes, never provider bodies, links or credentials.
    return {'error-token': 'upload_auth_required', 'error-notPremium': 'provider_plan_required',
            'error-limits': 'provider_quota_exceeded', 'error-rateLimit': 'provider_rate_limited'}.get(status, 'upload_failed')


def verify_upload(receipt, token=None):
    """Validate durable upload evidence without Premium listing/direct-link APIs.

    SHA-256 describes the locally extraction-verified archive. Matching upload
    size/MD5 is provider acknowledgement, NOT independent download verification.
    Legacy receipts missing provider checksums cannot be upgraded automatically.
    """
    md5 = receipt.get('md5', '')
    if (receipt.get('verification') != 'upload-receipt' or
            not isinstance(md5, str) or not re.fullmatch(r'[a-f0-9]{32}', md5) or
            not re.fullmatch(r'[a-f0-9]{32}', receipt.get('providerMd5', '')) or
            type(receipt.get('providerBytes')) is not int or
            type(receipt.get('uploadedAt')) is not int or receipt['uploadedAt'] <= 0):
        raise ArchiveError('verification_unavailable')
    if receipt['providerBytes'] != receipt.get('bytes') or receipt['providerMd5'] != md5:
        raise ArchiveError('verification_failed')
    return {key: receipt[key] for key in ['filename', 'bytes', 'sha256', 'contentId', 'pageUrl',
            'parentFolder', 'md5', 'providerMd5', 'providerBytes', 'uploadedAt', 'verification']}


def upload(path, token=None, on_uploaded=None):
    token = token or os.environ.get('GOFILE_TOKEN')
    if not token:
        raise ArchiveError('upload_failed')
    headers = {'Authorization': f'Bearer {token}'}
    try:
        size = path.stat().st_size
        sha256 = digest(path)
        with open(path, 'rb') as handle:
            md5 = hashlib.file_digest(handle, 'md5').hexdigest()
        with open(path, 'rb') as handle:
            # A separate folder per archive prevents a shared page from exposing
            # other cards. Do not create guest accounts or retry ambiguous POSTs.
            multipart = MultipartEncoder(fields={'file': (path.name, handle, 'application/x-7z-compressed')})
            with requests.post('https://upload.gofile.io/uploadfile', data=multipart,
                               headers={**headers, 'Content-Type': multipart.content_type},
                               timeout=(10, 600), allow_redirects=False) as response:
                result = response.json()
                if response.status_code != 200 or result.get('status') != 'ok':
                    raise ArchiveError(provider_error(result.get('status')))
        data = result['data']; page = data['downloadPage']; content_id = data['id']
        p = urlsplit(page)
        if p.scheme != 'https' or p.netloc != 'gofile.io' or not re.fullmatch(r'/d/[A-Za-z0-9_-]+', p.path) or p.query or p.fragment:
            raise ArchiveError('upload_failed')
        provider_md5 = data.get('md5', '')
        provider_size = data.get('size')
        receipt = {'filename': path.name, 'bytes': size, 'sha256': sha256,
                   'contentId': content_id, 'pageUrl': page, 'parentFolder': data['parentFolder'],
                   'md5': md5,
                   'uploadedAt': int(time.time()*1000), 'verification': 'upload-receipt'}
        if isinstance(provider_md5, str) and re.fullmatch(r'[a-fA-F0-9]{32}', provider_md5):
            receipt['providerMd5'] = provider_md5.lower()
        if type(provider_size) is int and provider_size >= 0:
            receipt['providerBytes'] = provider_size
        if on_uploaded:
            receipt = on_uploaded(receipt)
        return verify_upload(receipt)
    except ArchiveError:
        raise
    except Exception:
        raise ArchiveError('upload_failed') from None


def main():
    client = None
    try:
        client = Client()
        claimed = client.call('claim'); client.headers['X-Archive-Lease'] = claimed['leaseToken']
        heartbeat = threading.Thread(target=client.heartbeats, daemon=True); heartbeat.start()
        with tempfile.TemporaryDirectory(prefix='toyo-archive-') as directory:
            result = claimed.get('verifiedResult')
            if result and result.get('storageMode') == 'drive':
                client.call('cold-complete', {})
                print('Archive job completed')
                return 0
            if not result:
                receipt = claimed.get('uploadReceipt')
                source = None
                if not receipt:
                    source = client.call('source')
                if source and source.get('storageMode') == 'drive':
                    from drive_storage import Drive
                    from archive_inspector import safe_extract, verify_manifest
                    token=source.pop('driveToken');folder=source.pop('driveFolder')
                    client.stage='packaging'
                    path=build_archive(source,Path(directory))
                    restored=Path(directory)/'verified';restored.mkdir()
                    safe_extract(path,restored)
                    roots=list(restored.iterdir())
                    if len(roots)!=1 or not roots[0].is_dir():raise ArchiveError('verification_failed')
                    verify_manifest(roots[0],required=True)
                    if client.lost.is_set():raise ArchiveError('source_unavailable')
                    client.stage='uploading'
                    drive=Drive(token,lambda value:client.call('cold-progress',value),refresh=lambda:client.call('drive-token')['token'])
                    card=source['publicCard'];count=sum(len(c.get('tracks',[])) for c in card['content']['chapters'])
                    receipt=drive.upload(path,folder,os.environ['TOYO_ARCHIVE_JOB'],card['cardId'],
                        'demo' if '[DEMO]' in path.name else 'full',{'title':str(card.get('title','Untitled'))[:240],'tracks':count},claimed.get('coldProgress'))
                    client.call('cold-checkpoint',receipt);client.call('cold-complete',{})
                    print('Archive job completed');return 0
                if receipt:
                    client.stage = 'verifying'
                    result = verify_upload(receipt)
                else:
                    gofile_token = source.pop('gofileToken', None) or os.environ.get('GOFILE_TOKEN')
                    client.stage = 'packaging'
                    path = build_archive(source, Path(directory))
                    if client.lost.is_set():
                        raise ArchiveError('source_unavailable')
                    client.stage = 'uploading'
                    result = upload(path, gofile_token, on_uploaded=lambda receipt: client.call('receipt', receipt))
                client.call('checkpoint', result)
            if client.lost.is_set():
                raise ArchiveError('source_unavailable')
            client.call('complete',result)
        print('Archive job completed')
        return 0
    except Exception as error:
        code = error.code if isinstance(error,ArchiveError) else 'processing_failed'
        if client and 'X-Archive-Lease' in client.headers:
            try:
                client.call('fail',{'code':code})
            except Exception:
                pass
        print('Archive job failed: '+code)
        return 1
    finally:
        if client:
            client.stopped.set()


if __name__ == '__main__':
    raise SystemExit(main())
