import json
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch
import zipfile

import spotify_poc as poc


class PlaylistTests(unittest.TestCase):
    def test_canonicalizes_playlist_without_sharing_data(self):
        self.assertEqual(poc.playlist_url(poc.DEFAULT_PLAYLIST + '?si=secret#fragment'), poc.DEFAULT_PLAYLIST)
        self.assertEqual(poc.playlist_url(poc.DEFAULT_PLAYLIST.replace('/playlist/', '/intl-en/playlist/')), poc.DEFAULT_PLAYLIST)

    def test_rejects_wrong_hosts_types_and_injection(self):
        for url in ['http://open.spotify.com/playlist/' + 'a'*22,
                    'https://open.spotify.com.evil.test/playlist/' + 'a'*22,
                    'https://open.spotify.com@evil.test/playlist/' + 'a'*22,
                    'https://open.spotify.com/track/' + 'a'*22,
                    'https://open.spotify.com:443/playlist/' + 'a'*22,
                    poc.DEFAULT_PLAYLIST + '; echo unsafe']:
            with self.subTest(url=url), self.assertRaises(poc.POCError):
                poc.playlist_url(url)

    def test_metadata_allowlist_does_not_copy_signed_urls_or_tokens(self):
        data = {'songs': [{'title': 'Song', 'artist': 'Artist',
                          'url': 'https://open.spotify.com/track/' + 'a'*22,
                          'downloadUrl': 'https://files.test/private', 'token': 'secret'}]}
        self.assertNotIn('private', json.dumps(poc.clean_tracks(data, 'spotdown')))
        self.assertNotIn('secret', json.dumps(poc.clean_tracks(data, 'spotdown')))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audio = self.root / 'source.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=0.2', '-y', str(self.audio)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def track(self, title, id='a'):
        return {'title': title, 'artist': 'Artist', 'url': 'https://open.spotify.com/track/' + id*22}

    def download(self, title):
        return {'suggested': title + '_spotdown.org.mp3', 'path': self.audio}

    def test_multiple_downloads_are_packaged_in_playlist_order(self):
        dest = self.root/'archive.zip'
        result = poc.package_spotdown([self.track('First'), self.track('Second', 'b')],
                                     [self.download('Second'), self.download('First')], dest)
        self.assertEqual(result['status'], 'complete')
        with zipfile.ZipFile(dest) as z:
            self.assertEqual(z.namelist()[:2], ['0001 - First.mp3', '0002 - Second.mp3'])
        self.assertEqual(len(poc.inspect_zip(dest)['audio']), 2)

    def test_duplicate_occurrences_do_not_overwrite_files(self):
        result = poc.package_spotdown([self.track('Same'), self.track('Same')],
                                     [self.download('Same')], self.root/'archive.zip')
        self.assertEqual(result['downloadedTracks'], 2)
        self.assertNotEqual(result['tracks'][0]['filename'], result['tracks'][1]['filename'])

    def test_same_title_different_tracks_are_not_silently_assigned(self):
        result = poc.package_spotdown([self.track('Same'), self.track('Same', 'b')],
                                     [self.download('Same')], self.root/'archive.zip')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['tracks'][0]['failure'], 'ambiguous_same_title')

    def test_partial_download_reports_missing_tracks(self):
        result = poc.package_spotdown([self.track('First'), self.track('Missing', 'b')],
                                     [self.download('First')], self.root/'archive.zip')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['downloadedTracks'], 1)
        self.assertEqual(result['tracks'][1]['failure'], 'not_downloaded')

    def test_rejects_html_disguised_as_audio_and_archive(self):
        path = self.root/'fake.mp3'
        path.write_text('<html>Access denied</html>')
        with self.assertRaises(poc.POCError):
            poc.audio_info(path)
        with self.assertRaises(poc.POCError):
            poc.inspect_zip(path)

    def test_rejects_archive_traversal(self):
        path = self.root/'bad.zip'
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('../escape.mp3', b'not audio')
        with self.assertRaises(poc.POCError):
            poc.inspect_zip(path)


class DownloadTests(unittest.TestCase):
    def test_download_is_only_recorded_after_save_finishes(self):
        # Other runner tests exercise Playwright's sync event loop; use a fresh
        # interpreter to keep the async download check independent of that state.
        code = '''
import asyncio, tempfile
from pathlib import Path
import spotify_poc as poc
async def check():
    with tempfile.TemporaryDirectory() as tmp:
        state = poc.WebsiteRun('spotdown', Path(tmp))
        ready = asyncio.Event()
        class Download:
            suggested_filename = 'Song_spotdown.org.mp3'
            async def save_as(self, path):
                await ready.wait()
                path.write_bytes(b'complete bytes')
        task = asyncio.create_task(state.save(Download()))
        await asyncio.sleep(0)
        assert not state.downloads
        ready.set()
        await task
        assert len(state.downloads) == 1
asyncio.run(check())
'''
        subprocess.run([sys.executable, '-c', code], cwd=Path(poc.__file__).parent,
                       check=True, timeout=30)


class GofileTests(unittest.TestCase):
    def test_checks_json_status_even_when_http_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'test.zip'
            path.write_bytes(b'zip')
            response = Mock(status_code=200)
            response.json.return_value = {'status': 'error-token'}
            manager = Mock()
            manager.__enter__ = Mock(return_value=response)
            manager.__exit__ = Mock(return_value=False)
            with patch.object(poc.requests, 'post', return_value=manager) as post:
                with self.assertRaisesRegex(poc.POCError, 'gofile_upload_rejected'):
                    poc.upload_gofile(path, 'synthetic')
                self.assertEqual(post.call_count, 1)

    def test_uncertain_upload_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'test.zip'
            path.write_bytes(b'zip')
            with patch.object(poc.requests, 'post', side_effect=TimeoutError) as post:
                with self.assertRaisesRegex(poc.POCError, 'gofile_upload_uncertain'):
                    poc.upload_gofile(path, 'synthetic')
                self.assertEqual(post.call_count, 1)


if __name__ == '__main__':
    unittest.main()
