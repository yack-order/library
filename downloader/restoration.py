"""Restoration associations and original visual bytes; never persist source URLs."""
import hashlib
import io
import json
from pathlib import PurePosixPath
import re
import shutil
import warnings

from PIL import Image
from archive_errors import ArchiveError

IMAGE_TYPES = {'PNG': ('.png', 'image/png'), 'JPEG': ('.jpg', 'image/jpeg'),
               'GIF': ('.gif', 'image/gif'), 'WEBP': ('.webp', 'image/webp')}
MAX_IMAGE_BYTES = 16 * 1024**2
MAX_IMAGE_PIXELS = 25_000_000
MAX_ANIMATION_FRAMES = 2000
MAX_ANIMATION_PIXELS = 100_000_000
MAP_NAME = 'restoration-map.json'
AUDIO_SUFFIXES = {'.mp3', '.m4a', '.aac', '.ogg', '.wav'}


def checksum(path):
    with open(path, 'rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def image_info(path):
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ArchiveError('resource_limit')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in IMAGE_TYPES:
                    raise ArchiveError('media_format_unsupported')
                extension, mime = IMAGE_TYPES[image.format]
                frames = getattr(image, 'n_frames', 1)
                if frames > MAX_ANIMATION_FRAMES:
                    raise ArchiveError('resource_limit')
                pixels = 0
                for index in range(frames):
                    image.seek(index)
                    size = image.width * image.height
                    pixels += size
                    if size > MAX_IMAGE_PIXELS or pixels > MAX_ANIMATION_PIXELS:
                        raise ArchiveError('resource_limit')
                    image.load()
                return extension, mime
    except ArchiveError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ArchiveError('resource_limit') from None
    except Exception:
        raise ArchiveError('media_format_unsupported') from None


def preserve_image(source, stem):
    extension, mime = image_info(source)
    target = stem.with_suffix(extension)
    if target.exists():
        if checksum(target) != checksum(source):
            raise ArchiveError('identity_conflict')
    else:
        shutil.copyfile(source, target)
    return target, mime


def cover_png(path):
    """Transient derivative for audio tags, never replaces the saved original."""
    with Image.open(path) as image:
        clean = image.convert('RGBA')
        clean.info.clear()
        output = io.BytesIO()
        clean.save(output, format='PNG')
        return output.getvalue()


def visual_references(value, pointer='', visual=False):
    if isinstance(value, dict):
        for key, item in sorted(value.items()):
            name = key.lower()
            image_field = any(word in name for word in ('image', 'icon', 'cover', 'artwork', 'thumbnail', 'picture'))
            display_field = name in {'display', 'defaulttrackdisplay', 'ambient'}
            audio_field = any(word in name for word in ('audio', 'stream', 'playback', 'preview', 'download', 'sound', 'music', 'clip'))
            scope = image_field or ((visual or display_field) and not audio_field)
            path = pointer + '/' + key.replace('~', '~0').replace('/', '~1')
            yield from visual_references(item, path, scope)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from visual_references(item, pointer + '/' + str(index), visual)
    elif visual and isinstance(value, str) and value:
        # Reference-like strings only; labels and display settings are not URLs.
        if re.match(r'^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)', value):
            yield pointer, value


def visual_url(reference):
    if re.fullmatch(r'yoto:#[A-Za-z0-9_-]{43}', reference):
        return 'https://card-content.yotoplay.com/yoto/' + reference[6:]
    if reference.startswith('https://'):
        return reference
    return None


def pointer_value(card, pointer):
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise ValueError('pointer')
    node = card
    for component in pointer.split('/')[1:]:
        if re.search(r'~(?![01])', component):
            raise ValueError('pointer escape')
        key = component.replace('~1', '/').replace('~0', '~')
        if isinstance(node, list):
            if not re.fullmatch(r'0|[1-9][0-9]*', key):
                raise ValueError('array index')
            node = node[int(key)]
        else:
            node = node[key]
    return node


def retained_pointer(card, pointer):
    try:
        return isinstance(pointer_value(card, pointer), str)
    except (ValueError, KeyError, IndexError, TypeError):
        return False


def track_positions(card):
    for ci, chapter in enumerate((card.get('content') or {}).get('chapters') or []):
        for ti, track in enumerate(chapter.get('tracks') or []):
            yield {'chapterIndex': ci, 'trackIndex': ti,
                   'chapterKey': chapter.get('key'), 'trackKey': track.get('key')}


def file_entry(root, path):
    return {'path': path.relative_to(root).as_posix(), 'bytes': path.stat().st_size,
            'sha256': checksum(path)}


def new_map(playlist):
    return {'schemaVersion': 1, 'playlist': playlist, 'audio': [], 'visuals': []}


def write_map(root, mapping):
    (root / MAP_NAME).write_text(json.dumps(mapping, indent=2) + '\n', encoding='utf-8')
    verify_map(root)


def mapped_path(root, name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ArchiveError('unsafe_archive')
    parts = PurePosixPath(name)
    if parts.is_absolute() or '..' in parts.parts or parts.as_posix() != name:
        raise ArchiveError('unsafe_archive')
    path = root / name
    if not path.is_file() or path.is_symlink() or root.resolve() not in path.resolve().parents:
        raise ArchiveError('verification_failed')
    return path


def verify_map(root, manifest=None):
    path = root / MAP_NAME
    if not path.exists():
        return None  # Historical archives remain readable.
    try:
        if path.is_symlink():
            raise ArchiveError('unsafe_archive')
        if path.stat().st_size > 4 * 1024**2:
            raise ArchiveError('resource_limit')
        mapping = json.loads(path.read_text())
        if set(mapping) != {'schemaVersion', 'playlist', 'audio', 'visuals'} or mapping['schemaVersion'] != 1:
            raise ValueError('schema')
        playlist = mapped_path(root, mapping['playlist'])
        if playlist.suffix != '.json' or playlist.name in {MAP_NAME, 'manifest.json'}:
            raise ValueError('playlist')
        if playlist.stat().st_size > 4 * 1024**2:
            raise ArchiveError('resource_limit')
        decoded = json.loads(playlist.read_text(encoding='utf-8-sig'))
        card = decoded.get('card') or decoded.get('props', {}).get('pageProps', {}).get('card') or decoded
        positions = list(track_positions(card))
        if not isinstance(mapping['audio'], list) or not isinstance(mapping['visuals'], list):
            raise ValueError('entries')
        if positions and len(mapping['audio']) != len(positions):
            raise ValueError('track count')
        seen = set()
        image_types = {}
        checked_files = {}
        for number, entry in enumerate(mapping['audio']):
            if set(entry) - {'chapterIndex','trackIndex','chapterKey','trackKey','status','path','bytes','sha256'}:
                raise ValueError('fields')
            if positions and any(entry.get(k) != v for k, v in positions[number].items()):
                raise ValueError('track position')
            if entry['status'] not in {'verified', 'legacy_order_unverified', 'unresolved'}:
                raise ValueError('audio status')
            if entry['status'] == 'verified' and not positions:
                raise ValueError('missing playlist structure')
            if 'path' in entry:
                if mapped_path(root, entry['path']).suffix.lower() not in AUDIO_SUFFIXES:
                    raise ValueError('audio type')
                if entry['path'] in seen:
                    raise ValueError('duplicate audio path')
                seen.add(entry['path'])
        for entry in mapping['visuals']:
            if set(entry) - {'pointer','status','path','bytes','sha256','mimeType'}:
                raise ValueError('fields')
            pointer = entry['pointer']
            if pointer is not None and not retained_pointer(card, pointer):
                raise ValueError('pointer')
            if entry['status'] not in {'verified', 'unmapped', 'unresolved'}:
                raise ValueError('visual status')
            if (entry['status'] == 'verified' and pointer is None or
                    entry['status'] == 'unmapped' and pointer is not None):
                raise ValueError('association')
            if 'path' in entry:
                name = entry['path']
                if name not in image_types:
                    image_types[name] = image_info(mapped_path(root, name))[1]
                if image_types[name] != entry['mimeType']:
                    raise ValueError('image type')
        for entry in mapping['audio'] + mapping['visuals']:
            if entry['status'] not in {'verified','legacy_order_unverified','unmapped','unresolved'}:
                raise ValueError('status')
            if 'path' not in entry:
                if entry['status'] != 'unresolved':
                    raise ValueError('missing file')
                continue
            name = entry['path']
            if name not in checked_files:
                checked_files[name] = file_entry(root, mapped_path(root, name))
            expected = {'bytes': entry['bytes'], 'sha256': entry['sha256']}
            if checked_files[name] != {'path': name, **expected}:
                raise ValueError('checksum')
            if manifest is not None and manifest.get(entry['path']) != expected:
                raise ValueError('manifest')
        if manifest is not None and (mapping['playlist'] not in manifest or MAP_NAME not in manifest):
            raise ValueError('manifest membership')
        return mapping
    except ArchiveError:
        raise
    except Exception:
        raise ArchiveError('verification_failed') from None
