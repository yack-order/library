"""Synthetic public DOM; no network or private browser state."""
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from space_crawl import discover, page_url
from runner import ArchiveError

class Client:
    def __init__(self):self.last=None
    def call(self, action, value):self.last=value.copy()

class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime=sync_playwright().start()
        cls.browser=cls.runtime.chromium.launch(headless=True)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.runtime.stop()
    def test_public_reply_pagination_and_duplicate_links(self):
        page=self.browser.new_page()
        html='''<h1>Public test community listing</h1><a href="/post-one">A public post</a><button onclick="this.insertAdjacentHTML('beforebegin','<a href=https://share.yoto.co/s/Example>Shared in a reply</a>');this.remove()">Show more</button>'''
        page.route('https://yoto.space/**',lambda route:route.fulfill(body=html,content_type='text/html'))
        client=Client()
        with patch('space_crawl.time.sleep'):discover(page,client)
        self.assertTrue(client.last['complete']);self.assertEqual(len(client.last['visited']),2)
        self.assertEqual(len(client.last['shares']),2) # provenance retained for both pages
        self.assertEqual(client.last['frontier'],[])
        page.close()
    def test_stalled_pagination_does_not_claim_complete(self):
        page=self.browser.new_page();page.route('https://yoto.space/**',lambda route:route.fulfill(body='<h1>A public community with enough text to be valid</h1><button>Show more</button>',content_type='text/html'))
        client=Client()
        with self.assertRaises(ArchiveError) as result:discover(page,client)
        self.assertEqual(result.exception.code,'discovery_pagination_stalled');self.assertFalse((client.last or {}).get('complete',False));page.close()
    def test_rate_limit_does_not_claim_complete(self):
        page=self.browser.new_page();page.route('https://yoto.space/**',lambda route:route.fulfill(status=429,body='Rate limited'))
        with self.assertRaises(ArchiveError):discover(page,Client())
        page.close()
    def test_urls_stay_in_public_navigation_scope(self):
        self.assertIsNone(page_url('https://evil.test/'))
        self.assertIsNone(page_url('/member/private'))
        self.assertEqual(page_url('/made-by-you?page=2'),'https://yoto.space/made-by-you?page=2')
