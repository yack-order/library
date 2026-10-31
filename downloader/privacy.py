"""Redaction at every persistent text boundary; originals stay in memory only."""
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

REDACTED = '[redacted]'
_URL = re.compile(r'https?://[^\s<>"\x27]+', re.IGNORECASE)
_EMAIL = re.compile(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', re.IGNORECASE)
_SECRET = re.compile(
    r'(?i)\b(authorization|access[_-]?token|refresh[_-]?token|id[_-]?token|'
    r'client[_-]?secret|api[_-]?key|password)\b\s*[:=]?\s*["\x27]?[^\s,;"\x27]+')
_PRIVATE_KEYS = re.compile(
    r'(?i)^(.*(?:token|password|secret|authorization|credential|cookie|email|phone|address|api[_-]?key).*)$'
    r'|^(?:user|account|owner|profile|userId|ownerId)$', re.IGNORECASE)
_PUBLIC_LINKS = set()
_SENSITIVE_VALUES = set()


def register_public_links(data):
    """Only explicit shareLinkUrl on a non-official owner is a public share link.

    CardID and input URL possession never classify a protected URL as public.
    Unknown owners and official/discover cards fail closed. This follows the
    source's designated sharing field, never an inferred URL parameter format.
    """
    register_sensitive_values(data)
    card = data.get('props', {}).get('pageProps', {}).get('card', {})
    if not isinstance(card, dict):
        return
    owner = card.get('userId')
    if (not isinstance(owner, str) or not owner.strip() or owner.strip().lower() == 'yoto'
            or str(card.get('card_type', '')).lower() in {'official', 'discover'}):
        return
    link = card.get('shareLinkUrl')
    if not isinstance(link, str):
        return
    try:
        parsed = urlsplit(link)
        if (parsed.scheme == 'https' and parsed.hostname in {'yoto.io', 'share.yoto.io', 'yoto.space'}
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not re.search(r'[\s\x00-\x1f]', link)):
            _PUBLIC_LINKS.add(link)
    except ValueError:
        return


def register_sensitive_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if (re.search(r'(?i)token|password|secret|authorization|credential|cookie|email|phone|api[_-]?key', str(key))
                    and isinstance(item, str) and len(item) >= 4):
                _SENSITIVE_VALUES.add(item)
            register_sensitive_values(item)
    elif isinstance(value, list):
        for item in value:
            register_sensitive_values(item)


def redact_text(value):
    text = str(value)
    for secret in sorted(_SENSITIVE_VALUES, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = re.sub(r'(?i)\bbearer\s+[^\s,;"\x27]+', 'Bearer [redacted]', text)
    text = re.sub(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}', REDACTED, text)
    text = _URL.sub(lambda m: m.group(0) if m.group(0) in _PUBLIC_LINKS else '[redacted URL]', text)
    text = _EMAIL.sub('[redacted email]', text)
    return _SECRET.sub(lambda m: m.group(1) + '=' + REDACTED, text)


def scrub_json(value):
    if isinstance(value, dict):
        return {redact_text(k): scrub_json(v) for k, v in value.items()
                if not _PRIVATE_KEYS.match(str(k))}
    if isinstance(value, list):
        return [scrub_json(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class PrivateTextFile:
    def __init__(self, handle):
        self.handle = handle

    def write(self, text):
        return self.handle.write(redact_text(text))

    def close(self):
        self.handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def open_private_text(path, mode='w'):
    target = Path(path)
    if target.is_symlink():
        raise ValueError('Refusing symlink text output')
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    flags |= os.O_APPEND if mode == 'a' else os.O_TRUNC
    descriptor = os.open(target, flags, 0o600)
    return PrivateTextFile(os.fdopen(descriptor, mode, encoding='utf-8', newline=''))


def export_json(directory, title, data):
    from archive_safety import safe_filename
    descriptor, name = tempfile.mkstemp(prefix=safe_filename(title, limit=120) + '-', suffix='.json', dir=directory)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(scrub_json(data), handle, indent=4)
    return name


def export_log(directory, kind, text):
    from archive_safety import output_path, safe_filename
    logs = Path(output_path(directory, 'logs'))
    logs.mkdir(exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='YJE-' + safe_filename(kind) + '-', suffix='.log', dir=logs)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(redact_text(text))
    return name


def csv_cell(value):
    text = redact_text(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text
