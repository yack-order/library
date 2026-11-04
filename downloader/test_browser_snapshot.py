import unittest
from browser_snapshot import browser_snapshot
from runner import ArchiveError


class BrowserSnapshotTests(unittest.TestCase):
    def capture(self):
        url = 'https://mommabearaudio.com/synthetic/'
        return {'initialUrls': [url], 'finalUrls': [url], 'capturedAt': 123,
                'pages': [{'url': url, 'title': 'Synthetic', 'password': False,
                           'headings': ['Synthetic'], 'paragraphs': ['A <story> & more'], 'images': [],
                           'links': [{'label': 'Yoto Link', 'url': 'https://share.yoto.co/s/Synthetic', 'tag': 'a'},
                                     {'label': 'Transcript', 'url': 'https://www.dropbox.com/scl/fo/synthetic?rlkey=private', 'tag': 'a'}]}]}

    def test_captures_content_and_keeps_resource_access_keys(self):
        result = browser_snapshot(self.capture())
        self.assertEqual(result['entries'][0]['description'], 'A <story> & more')
        self.assertIn('rlkey=private', result['entries'][0]['supplements'][0]['url'])
        self.assertNotIn('pages', result)

    def test_missing_changed_duplicate_and_login_pages_cannot_be_complete(self):
        for mutation in ('missing', 'changed', 'duplicate', 'login'):
            capture = self.capture()
            if mutation == 'missing': capture['pages'] = []
            if mutation == 'changed': capture['finalUrls'] = []
            if mutation == 'duplicate': capture['pages'] *= 2
            if mutation == 'login': capture['pages'][0]['password'] = True
            with self.subTest(mutation=mutation), self.assertRaises(ArchiveError):
                browser_snapshot(capture)
