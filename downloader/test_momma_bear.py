"""Synthetic publisher pages and files; never accesses the paid library."""
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch, Mock
from momma_bear import Document, listing, parse_entry, discover, create_playlist, MommaClient
from momma_resources import download_url, unpack_resource, ordered_mp3s, capture_supplements
from runner import ArchiveError
import momma_bear

ENTRY = '''<h1>Games &amp; Activities</h1><img src="https://mommabearaudio.com/cover.png"><p>Stories and games.</p><p>Run time: 14 min</p>
<a href="https://share.yoto.co/s/Synthetic">Yoto Link</a><a href="https://www.dropbox.com/scl/fo/audio?rlkey=secret">MP3 Download Link</a>
<a href="https://www.dropbox.com/scl/fo/docs?rlkey=secret">Map-Transcript-MYO Label</a>'''
LIST = '<h2>All Audio</h2><article><h1><a href="/games/">Games</a></h1><a href="/games/">Learn More</a></article><a href="/subscription/membership-account/">Account</a>'

class MommaTests(unittest.TestCase):
    def test_browser_snapshot_never_opens_membership_or_requires_login(self):
        entry = parse_entry(Document(ENTRY).root, 'https://mommabearaudio.com/games/')
        client = Mock()
        client.headers = {}
        client.call.side_effect = [{'leaseToken': 'lease', 'mode': 'existing', 'entries': [entry]}, {}, {'state': 'imported'}, {}]
        with patch.object(momma_bear, 'MommaClient', return_value=client), patch.object(momma_bear, 'Membership') as membership, patch.object(momma_bear.threading, 'Thread'), patch('builtins.print'):
            self.assertEqual(momma_bear.main(), 0)
        membership.assert_not_called()
        self.assertIn(('discovered', [entry]), [call.args for call in client.call.call_args_list])

    def test_login_failures_report_only_controlled_codes(self):
        client = Mock()
        client.headers = {}
        client.call.side_effect = [{'leaseToken': 'private-lease', 'mode': 'existing',
                                    'login': {'username': 'private-user', 'password': 'private-password'}}, {'ok': True}]
        member = Mock()
        member.login.side_effect = ArchiveError('membership_login_required')
        with patch.object(momma_bear, 'MommaClient', return_value=client), patch.object(momma_bear, 'Membership', return_value=member), patch.object(momma_bear.threading, 'Thread'), patch('builtins.print') as output:
            self.assertEqual(momma_bear.main(), 1)
        self.assertEqual(client.call.call_args.args, ('fail', {'code': 'membership_login_required'}))
        self.assertNotIn('private-password', str(output.call_args_list))
        member.close.assert_called_once()

    def test_member_pages_wait_for_dom_and_report_browser_timeouts_safely(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        member = momma_bear.Membership.__new__(momma_bear.Membership)
        member.browser_page = Mock()
        member.browser_page.goto.return_value.status = 200
        member.browser_page.url = 'https://mommabearaudio.com/login/'
        member.browser_page.title.return_value = 'Member Login'
        member.browser_page.content.return_value = '<h1>Login</h1>'
        member.page('https://mommabearaudio.com/login/')
        member.browser_page.goto.assert_called_once_with('https://mommabearaudio.com/login/', wait_until='domcontentloaded', timeout=60000)
        member.browser_page.goto.side_effect = BrowserTimeout('request contains private information')
        with self.assertRaises(ArchiveError) as caught:
            member.page('https://mommabearaudio.com/login/')
        self.assertEqual(caught.exception.code, 'membership_http_failed')
        self.assertNotIn('private information', str(caught.exception))

    def test_browser_challenge_is_reported_before_http_status(self):
        member = momma_bear.Membership.__new__(momma_bear.Membership)
        member.browser_page = Mock()
        member.browser_page.goto.return_value.status = 403
        member.browser_page.title.return_value = 'Just a moment...'
        with self.assertRaises(ArchiveError) as caught:
            member.page('https://mommabearaudio.com/login/')
        self.assertEqual(caught.exception.code, 'membership_blocked')

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

    def test_explicit_no_yoto_prose_is_not_mistaken_for_a_broken_audio_page(self):
        html = ENTRY.replace('<a href="https://share.yoto.co/s/Synthetic">Yoto Link</a>',
                             '<p>There is no Yoto Link for this playlist as it is build your own.</p>')
        entry = parse_entry(Document(html).root, 'https://mommabearaudio.com/games/')
        self.assertTrue(entry['noYoto'])
        self.assertIsNone(entry['yoto'])
        self.assertIsNotNone(entry['mp3'])

    def test_runtime_excludes_publisher_notes_but_preserves_the_full_description(self):
        html = ENTRY.replace('Run time: 14 min', 'Run time total: 18 minPrintable worksheet is included.')
        entry = parse_entry(Document(html).root, 'https://mommabearaudio.com/games/')
        self.assertEqual(entry['runtime'], 'Run time total: 18 min')
        self.assertIn('Printable worksheet is included.', entry['description'])

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

    def test_dropbox_empty_root_marker_and_explicit_chapter_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'audio.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('/', b'')
                z.writestr('Chapter 10_ End.mp3', b'synthetic')
                z.writestr('Chapter 1_ Start.mp3', b'synthetic')
            files = unpack_resource(archive, root / 'audio', audio=True)
            self.assertEqual([p.name for p in ordered_mp3s(files)], ['Chapter 1_ Start.mp3', 'Chapter 10_ End.mp3'])
            sequence = [Path('Chapter 2_ More.mp3'), Path('1_Introduction.mp3'), Path('Chapter 1_ Start.mp3')]
            self.assertEqual([p.name for p in ordered_mp3s(sequence)], ['1_Introduction.mp3', 'Chapter 1_ Start.mp3', 'Chapter 2_ More.mp3'])
            for unsafe in ['/escape.mp3', '/absolute/']:
                with zipfile.ZipFile(archive, 'w') as z:
                    z.writestr(unsafe, b'')
                with self.assertRaises(ArchiveError):
                    unpack_resource(archive, root / 'unsafe', audio=True)
            with self.assertRaises(ArchiveError):
                ordered_mp3s([Path('Chapter 1_ A.mp3'), Path('Track 1_ B.mp3')])

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


    def test_episode_numbers_are_numeric_and_duplicates_remain_held(self):
        files = [Path('Episode 10 Finale.mp3'), Path('Episode 2 Clue.mp3'), Path('Episode 1 Start.mp3')]
        self.assertEqual(ordered_mp3s(files), [files[2], files[1], files[0]])
        with self.assertRaises(ArchiveError):
            ordered_mp3s([Path('Episode 1 A.mp3'), Path('Episode 1 B.mp3')])
        with self.assertRaises(ArchiveError):
            ordered_mp3s([Path('Episode One.mp3'), Path('Episode Two.mp3')])

    def test_safe_checkpoint_retries_transient_api_failures(self):
        client = MommaClient.__new__(MommaClient)
        with patch('runner.Client.call', side_effect=[momma_bear.requests.Timeout(), ArchiveError('source_unavailable'), {'ok': True}]) as call, patch('momma_bear.time.sleep'):
            self.assertEqual(client.call('entry', {'url': 'synthetic'}), {'ok': True})
            self.assertEqual(call.call_count, 3)

    def test_uncertain_actions_and_cancellation_are_not_replayed(self):
        client = MommaClient.__new__(MommaClient)
        for action in ['claim', 'create', 'finish']:
            with self.subTest(action=action), patch('runner.Client.call', side_effect=momma_bear.requests.Timeout()) as call, patch('builtins.print'):
                with self.assertRaises(momma_bear.requests.Timeout):
                    client.call(action)
                call.assert_called_once()
        with patch('runner.Client.call', side_effect=ArchiveError('job_cancelled')) as call:
            with self.assertRaises(ArchiveError):
                client.call('entry')
            call.assert_called_once()

    def test_checkpoint_retries_are_bounded(self):
        client = MommaClient.__new__(MommaClient)
        with patch('runner.Client.call', side_effect=momma_bear.requests.Timeout()) as call, patch('momma_bear.time.sleep'), patch('builtins.print') as output:
            with self.assertRaises(momma_bear.requests.Timeout):
                client.call('entry', {'url': 'private'})
            self.assertEqual(call.call_count, 3)
            output.assert_called_once_with('Momma Bear API request failed: entry', flush=True)

    def test_heartbeat_recovers_after_transient_failure(self):
        client = MommaClient.__new__(MommaClient)
        client.stopped = Mock()
        client.stopped.wait.side_effect = [False, False, False, True]
        client.lost = Mock()
        client.stage = 'fetching'
        client.call = Mock(side_effect=[momma_bear.requests.Timeout(), ArchiveError('source_unavailable'), {}])
        client.heartbeats()
        self.assertEqual(client.call.call_count, 3)
        client.lost.set.assert_not_called()

    def test_heartbeat_stops_on_cancellation(self):
        client = MommaClient.__new__(MommaClient)
        client.stopped = Mock()
        client.stopped.wait.return_value = False
        client.lost = Mock()
        client.stage = 'fetching'
        client.call = Mock(side_effect=ArchiveError('job_cancelled'))
        client.heartbeats()
        client.call.assert_called_once()
        client.lost.set.assert_called_once()

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
