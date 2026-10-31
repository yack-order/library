"""Filesystem boundaries for untrusted playlist metadata."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import tempfile


def safe_filename(value, fallback="Untitled", limit=180):
    """Return a single portable filename component, never a special directory."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '', str(value or '')).strip(' .')
    name = name.encode('utf-8')[:limit].decode('utf-8', errors='ignore').rstrip(' .')
    if not name or name.split('.')[0].upper() in {
        'CON', 'PRN', 'AUX', 'NUL',
        *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10)),
    }:
        return fallback
    return name


def output_path(directory, name):
    """Anchor a filename to the chosen output directory; reject existing symlinks."""
    root = Path(directory).resolve(strict=True)
    if not root.is_dir() or name != safe_filename(name) or Path(name).name != name:
        raise ValueError('Invalid output filename')
    target = root / name
    if target.is_symlink():
        raise ValueError('Refusing a symlink output')
    return str(target)


@dataclass(frozen=True)
class Workspace:
    root: Path
    path: Path
    identity: tuple

    @classmethod
    def create(cls, directory):
        root = Path(directory).resolve(strict=True)
        path = Path(tempfile.mkdtemp(prefix='.yje-work-', dir=root))
        info = path.stat()
        return cls(root, path, (info.st_dev, info.st_ino))

    def cleanup(self):
        """Remove only this fresh directory, never a reused or replaced directory."""
        info = self.path.lstat()
        if (self.path.parent != self.root or not self.path.name.startswith('.yje-work-')
                or self.path.is_symlink() or (info.st_dev, info.st_ino) != self.identity):
            raise ValueError('Workspace ownership changed; keeping files')
        shutil.rmtree(self.path)


def create_verified_archive(folder_path, requested_path):
    """Verify a temporary archive, then publish without replacing an existing file.

    Failure leaves the owned download workspace intact for manual recovery.
    """
    import py7zr

    source = Path(folder_path)
    requested = Path(requested_path)
    temporary = None
    try:
        if source.is_symlink() or not source.is_dir():
            raise ValueError('Invalid archive source')
        expected = {source.name}
        for item in source.rglob('*'):
            if item.is_symlink():
                raise ValueError('Refusing symlink archive members')
            expected.add(str(Path(source.name) / item.relative_to(source)).replace('\\', '/'))
        descriptor, temporary = tempfile.mkstemp(prefix='.yje-archive-', suffix='.7z', dir=requested.parent)
        with os.fdopen(descriptor, 'w+b') as handle:
            with py7zr.SevenZipFile(handle, 'w') as archive:
                archive.writeall(source, source.name)
            handle.flush()
            os.fsync(handle.fileno())
        with py7zr.SevenZipFile(temporary, 'r') as archive:
            if set(archive.getnames()) != expected or archive.testzip() is not None:
                raise ValueError('Archive integrity verification failed')
        # link() publishes a complete file atomically and never overwrites a path.
        for index in range(10000):
            candidate = requested if index == 0 else requested.with_name(
                f'{requested.stem} ({index}){requested.suffix}')
            try:
                os.link(temporary, candidate)
                return str(candidate)
            except FileExistsError:
                continue
        raise FileExistsError('Archive output names exhausted')
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
