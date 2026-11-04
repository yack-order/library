"""Bounded downloads of publisher resource links. Never sends membership cookies."""
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from runner import ArchiveError, stream_file, digest
from archive_safety import safe_filename

DOCUMENTS = {'.pdf', '.txt', '.rtf', '.doc', '.docx', '.odt', '.ppt', '.pptx',
             '.xls', '.xlsx', '.csv', '.png', '.jpg', '.jpeg', '.webp', '.gif'}
MAX_FILES = 1000
MAX_BYTES = 512 * 1024**2


def download_url(value):
    p = urlsplit(value)
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
        raise ArchiveError('resource_link_invalid')
    if p.hostname in {'www.dropbox.com', 'dropbox.com'}:
        query = [(k, v) for k, v in parse_qsl(p.query) if k not in {'dl', 'raw'}]
        return urlunsplit(('https', p.netloc, p.path, urlencode(query + [('dl', '1')]), ''))
    if p.hostname == 'drive.google.com':
        match = re.fullmatch(r'/file/d/([A-Za-z0-9_-]+)/view', p.path)
        if not match:
            raise ArchiveError('resource_link_unsupported')
        return 'https://drive.google.com/uc?export=download&id=' + match[1]
    if p.hostname != 'mommabearaudio.com':
        raise ArchiveError('resource_link_invalid')
    return value


def unpack_resource(path, destination, *, audio=False):
    """ZIP paths are validated before any extraction, including excluded files."""
    destination.mkdir(parents=True, exist_ok=True)
    allowed = {'.mp3'} if audio else DOCUMENTS
    if Path(path.name.removesuffix('.download')).suffix.lower() in allowed or not zipfile.is_zipfile(path):
        # A file link must have a recognized filename; HTML is never a document.
        name = path.name.removesuffix('.download')
        if Path(name).suffix.lower() not in allowed:
            raise ArchiveError('resource_format_unsupported')
        with path.open('rb') as probe:
            head = probe.read(256).lstrip().lower()
        if head.startswith((b'<!doctype html', b'<html')):
            raise ArchiveError('resource_download_failed')
        target = destination / safe_filename(name)
        if target.exists():
            raise ArchiveError('resource_name_collision')
        path.replace(target)
        return [target]
    with zipfile.ZipFile(path) as archive:
        rows = archive.infolist()
        if len(rows) > MAX_FILES or sum(r.file_size for r in rows) > MAX_BYTES:
            raise ArchiveError('resource_limit')
        files, seen = [], set()
        for row in rows:
            # Dropbox folder ZIPs include a zero-byte root directory marker.
            # It is metadata only; never turn it into an extraction destination.
            if row.filename == '/' and row.is_dir() and row.file_size == 0 and not (row.flag_bits & 1) and not stat.S_ISLNK(row.external_attr >> 16):
                continue
            name = PurePosixPath(row.filename)
            if (name.is_absolute() or '..' in name.parts or '\\' in row.filename
                    or re.match(r'^[A-Za-z]:', row.filename)
                    or stat.S_ISLNK(row.external_attr >> 16) or row.flag_bits & 1):
                raise ArchiveError('unsafe_resource_archive')
            if row.is_dir() or '__MACOSX' in name.parts or name.name.startswith('.'):
                continue
            if name.suffix.lower() not in allowed:
                if name.suffix.lower() in {'.zip', '.7z'}:
                    raise ArchiveError('nested_resource_archive')
                continue
            parts = [safe_filename(p) for p in name.parts]
            relative = '/'.join(parts)
            if relative.casefold() in seen:
                raise ArchiveError('resource_name_collision')
            seen.add(relative.casefold())
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(row) as source, target.open('xb') as output:
                size = 0
                while chunk := source.read(65536):
                    size += len(chunk)
                    if size > row.file_size or size > MAX_BYTES:
                        raise ArchiveError('resource_limit')
                    output.write(chunk)
            if size != row.file_size:
                raise ArchiveError('resource_download_failed')
            files.append(target)
    if not files:
        raise ArchiveError('resource_files_missing')
    return files


def fetch_resource(url, destination, *, audio=False):
    destination.mkdir(parents=True, exist_ok=True)
    # Keep the original filename for direct resources; folder ZIPs are detected
    # by signature. No URL/query string is ever used as an output filename.
    name = safe_filename(Path(urlsplit(url).path).name or 'resource')
    temporary = destination.parent / (destination.name + '-' + name + '.download')
    try:
        mime = stream_file(download_url(url), temporary, maximum=MAX_BYTES)
        suffixes = {'application/pdf':'.pdf','text/plain':'.txt','image/png':'.png','image/jpeg':'.jpg','audio/mpeg':'.mp3'}
        if not Path(name).suffix and not zipfile.is_zipfile(temporary) and mime in suffixes:
            named = temporary.with_name(temporary.name.removesuffix('.download') + suffixes[mime] + '.download')
            temporary.rename(named)
            temporary = named
        return unpack_resource(temporary, destination, audio=audio)
    finally:
        temporary.unlink(missing_ok=True)


def capture_supplements(resources, work):
    root = work / 'captured-supplements'
    root.mkdir()
    if len(resources) > 50:
        raise ArchiveError('resource_limit')
    for index, resource in enumerate(resources, 1):
        folder = root / (f'{index:02d}-' + safe_filename(resource['label'], limit=100))
        fetch_resource(resource['url'], folder)
        files = [p for p in root.rglob('*') if p.is_file()]
        if len(files) > MAX_FILES or sum(p.stat().st_size for p in files) > MAX_BYTES:
            raise ArchiveError('resource_limit')
    return root, {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob('*')) if p.is_file()}


def ordered_mp3s(files):
    if len(files) == 1:
        return files
    def number(path):
        match = re.match(r'^(?:(?:track|chapter|episode)[ _-]*)?(\d+)(?:\D|$)', path.stem, re.I)
        if not match:
            raise ArchiveError('track_order_needs_review')
        return int(match[1])
    numbers = [number(p) for p in files]
    if len(set(numbers)) != len(numbers):
        # Some publisher folders number an explicit introduction separately
        # from their numbered main sequence. Accept only that unambiguous layout.
        intros = [p for p in files if re.match(r'^1[ _.-]+(?:.*[ _.-])?(?:intro(?:duction)?|prelude)\b', p.stem, re.I)]
        chapters = [p for p in files if p not in intros]
        if (len(intros) == 1 and chapters
                and all(number(p) >= 1 for p in chapters)
                and len({number(p) for p in chapters}) == len(chapters)):
            return intros + sorted(chapters, key=number)
        raise ArchiveError('track_order_needs_review')
    return sorted(files, key=number)
