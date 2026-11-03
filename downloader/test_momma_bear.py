"""Synthetic publisher pages and files; never accesses the paid library."""
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch, Mock
from momma_bear import Document, listing, parse_entry, discover, create_playlist
from momma_resources import download_url, unpack_resource, ordered_mp3s, capture_supplements
from runner import ArchiveError

ENTRY = '''<h1>Games &amp; Activities</h1><img src="https://mommabearaudio.com/cover.png"><p>Stories and games.</p><p>Run time: 14 min</p>
<a href="https://share.yoto.co/s/Synthetic">Yoto Link</a><a href="https://www.dropbox.com/scl/fo/audio?rlkey=secret">MP3 Download Link</a>
<a href="https://www.dropbox.com/scl/fo/docs?rlkey=secret">Map-Transcript-MYO Label</a>'''
LIST = '<h2>All Audio</h2><article><h1><a href="/games/">Games</a></h1><a href="/games/">Learn More</a></article><a href="/subscription/membership-account/">Account</a>'

class MommaTests(unittest.TestCase):
    def test_listing_excludes_navigation_and_deduplicates_cards(self):
        urls, pages = listing(Document(LIST).root)
        self.assertEqual(urls, ['https://mommabearaudio.com/games/'])
        self.assertEqual(pages, [])

    def test_audio_metadata_and_private_document_links(self):
        entry = parse_entry(Document(ENTRY).root, 'https://mommabearaudio.com/games/')
        self.assertEqual(entry['title'], 'Games & Activities')
        self.assertEqual(entry['yoto'], 'https://share.yoto.co/s/Synthetic')
        self.assertEqual(len(entry['supplements']), 1)
        self.assertIn('rlkey=secret', entry['supplements'][0]['url'])
        self.assertFalse(entry['noYoto'])

    def test_explicit_missing_yoto_and_printable_only_are_distinct(self):
        missing = ENTRY.replace('<a href="https://share.yoto.co/s/Synthetic">Yoto Link</a>', '<a role="button">No Yoto Link</a>')
        self.assertTrue(parse_entry(Document(missing).root, 'https://mommabearaudio.com/games/')['noYoto'])
        printable = parse_entry(Document('<h1>Printable</h1><p>Not Audio. Digital download only.</p>').root, 'https://mommabearaudio.com/printable/')
        self.assertTrue(printable['printableOnly'])
        with self.assertRaises(ArchiveError):
            parse_entry(Document('<h1>Broken audio layout</h1>').root, 'https://mommabearaudio.com/games/')

    def test_changed_listing_never_commits_a_complete_snapshot(self):
        member, client = Mock(), Mock()
        member.login.return_value = Document(LIST).root
        member.page.return_value = Document(ENTRY).root
        member.dashboard.return_value = Document(LIST.replace('/games/', '/new/')).root
        with patch('momma_bear.time.sleep'), self.assertRaises(ArchiveError):
            discover(member, client, {})
        self.assertNotIn('discovered', [call.args[0] for call in client.call.call_args_list])

    def test_zip_documents_skip_audio_and_block_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'docs.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('Transcript.pdf', b'%PDF synthetic')
                z.writestr('audio.mp3', b'excluded')
            paths = unpack_resource(archive, root / 'docs')
            self.assertEqual([p.name for p in paths], ['Transcript.pdf'])
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('../escape.pdf', b'unsafe')
            with self.assertRaises(ArchiveError):
                unpack_resource(archive, root / 'unsafe')
            self.assertFalse((root.parent / 'escape.pdf').exists())

    def test_zip_collisions_and_html_downloads_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'docs.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('a?.pdf', b'one')
                z.writestr('a*.pdf', b'two')
            with self.assertRaises(ArchiveError):
                unpack_resource(archive, root / 'docs')
            html = root / 'document.pdf.download'
            html.write_text('<!DOCTYPE html><html>Login required</html>')
            with self.assertRaises(ArchiveError):
                unpack_resource(html, root / 'html')

    def test_numbered_tracks_are_ordered_and_ambiguity_is_held(self):
        self.assertEqual(ordered_mp3s([Path('10 End.mp3'), Path('02 Start.mp3')]), [Path('02 Start.mp3'), Path('10 End.mp3')])
        for files in [[Path('A.mp3'), Path('B.mp3')], [Path('01 A.mp3'), Path('01 B.mp3')]]:
            with self.assertRaises(ArchiveError):
                ordered_mp3s(files)

    def test_dropbox_resource_keys_survive_download_normalization(self):
        url = download_url('https://www.dropbox.com/scl/fo/docs?rlkey=secret&dl=0')
        self.assertIn('rlkey=secret', url)
        self.assertIn('dl=1', url)
        with self.assertRaises(ArchiveError):
            download_url('https://evil.test/file.pdf')

    def test_supplement_hashes_are_content_based(self):
        with tempfile.TemporaryDirectory() as directory:
            def download(url, destination):
                destination.mkdir(parents=True)
                target = destination / 'Transcript.pdf'
                target.write_bytes(b'%PDF synthetic')
                return [target]
            with patch('momma_resources.fetch_resource', side_effect=download):
                root, hashes = capture_supplements([{'label': 'Transcript', 'url': 'private'}], Path(directory))
            self.assertEqual(len(hashes), 1)
            self.assertTrue((root / '01-Transcript' / 'Transcript.pdf').exists())

    def test_upload_reuses_yoto_hash_and_creates_ordered_chapters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / '02 End.mp3', root / '01 Start.mp3']
            for path in paths:
                path.write_bytes(b'synthetic')
            client = Mock()
            client.call.return_value = {'cardId': 'abc12'}
            entry = parse_entry(Document(ENTRY).root, 'https://mommabearaudio.com/games/')
            entry['yoto'] = None
            transcode = {'transcode': {'transcodedSha256': 'hash', 'transcodedInfo': {'duration': 1, 'fileSize': 9}}}
            with patch('momma_bear.fetch_resource', return_value=paths), patch('momma_bear.yoto_json', side_effect=[{'upload': {'uploadId': 'upload', 'uploadUrl': None}}, transcode] * 2):
                create_playlist(client, entry, root)
            payload = client.call.call_args.args[1]
            chapters = payload['content']['content']['chapters']
            self.assertEqual([c['title'] for c in chapters], ['Start', 'End'])
            self.assertIn('Toyo Momma Bear source:', payload['content']['metadata']['description'])

if __name__ == '__main__':
    unittest.main()
