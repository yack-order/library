"""Synthetic public DOM; no network or private browser state."""
import unittest
import json
from copy import deepcopy
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from space_crawl import discover, page_url, advance
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
        with patch('space_crawl.PROGRESS_TIMEOUT_MS',1000), patch('space_crawl.time.sleep'):discover(page,client)
        self.assertFalse(client.last['complete']);self.assertTrue(client.last['exhausted'])
        self.assertEqual(client.last['failures']['https://yoto.space/'],{'attempts':3,'code':'discovery_pagination_stalled'});page.close()
    def test_rate_limit_does_not_claim_complete(self):
        page=self.browser.new_page();page.route('https://yoto.space/**',lambda route:route.fulfill(status=429,body='Rate limited'))
        client=Client()
        with patch('space_crawl.time.sleep'):discover(page,client)
        self.assertFalse(client.last['complete']);self.assertEqual(client.last['failures']['https://yoto.space/']['code'],'discovery_rate_limited')
        page.close()
    def test_urls_stay_in_public_navigation_scope(self):
        self.assertIsNone(page_url('https://evil.test/'))
        self.assertIsNone(page_url('/member/private'))
        self.assertEqual(page_url('/made-by-you?page=2'),'https://yoto.space/made-by-you?page=2')

    def test_delayed_posts_wait_past_transient_loading_label(self):
        page=self.browser.new_page()
        page.route('https://yoto.space/**',lambda route:route.fulfill(content_type='text/html',body='''<h1>A public community with enough visible text for discovery</h1><button onclick="this.textContent='Loading';setTimeout(()=>{this.insertAdjacentHTML('beforebegin','<a href=https://share.yoto.co/s/Delayed>Delayed public reply</a>');this.remove()},1400)">Show more</button>'''))
        client=Client()
        with patch('space_crawl.time.sleep'):discover(page,client)
        self.assertTrue(client.last['complete'])
        self.assertEqual(client.last['shares'],[{'url':'https://share.yoto.co/s/Delayed','provenance':'https://yoto.space/'}])
        page.close()
    def test_loading_label_alone_does_not_count_as_progress(self):
        page=self.browser.new_page()
        page.set_content("""<h1>A public community with enough visible text</h1><button onclick="this.textContent='Loading'">Show more</button>""")
        with patch('space_crawl.PROGRESS_TIMEOUT_MS',1000), self.assertRaises(ArchiveError) as result:
            advance(page,page.get_by_role('button',name='Show more'))
        self.assertEqual(result.exception.code,'discovery_pagination_stalled')
        page.close()

    def test_horizontal_carousel_next_is_real_progress(self):
        page=self.browser.new_page()
        page.set_content("""<h1>A public community carousel</h1><div id="carousel" style="width:100px;overflow:hidden"><div style="width:600px">All carousel items already exist in the DOM</div></div><button onclick="document.getElementById('carousel').scrollLeft+=100">Next</button>""")
        advance(page,page.get_by_role('button',name='Next'))
        self.assertEqual(page.locator('#carousel').evaluate('(el)=>el.scrollLeft'),100)
        page.close()

    def test_a_blocked_page_does_not_starve_the_saved_frontier(self):
        page=self.browser.new_page()
        page.route('https://yoto.space/**',lambda route:route.fulfill(status=429,body='Rate limited') if route.request.url.endswith('/bad') else route.fulfill(content_type='text/html',body='<h1>A reachable public post with a reply-only share</h1><a href="https://yoto.io/abc12">Shared playlist</a>'))
        saved={'frontier':['https://yoto.space/bad','https://yoto.space/good'],'visited':[],'shares':[],'complete':False,'canary':False}
        client=Client()
        with patch('space_crawl.PAGE_BATCH',2),patch('space_crawl.time.sleep'):result=discover(page,client,saved)
        self.assertEqual(result['visited'],['https://yoto.space/good'])
        self.assertEqual(result['frontier'],['https://yoto.space/bad'])
        self.assertEqual(len(result['shares']),1);self.assertFalse(result['exhausted'])
        with patch('space_crawl.time.sleep'):result=discover(page,client,result)
        self.assertTrue(result['exhausted']);self.assertFalse(result['complete']);self.assertEqual(result['failures']['https://yoto.space/bad']['attempts'],3)
        page.close()

    def test_public_cursor_resume_is_fair_and_does_not_persist_headers(self):
        calls=[]
        def new_page(scope='a'):
            page=self.browser.new_page()
            html="""<h1>A public paginated community with enough visible content</h1><button id="more" data-after="1" onclick='loadMore(this)'>Show more</button><script>
            async function loadMore(button){const response=await fetch('/graphql?neverPersist=secret',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'NeverPersistThisHeader'},body:JSON.stringify({operationName:'GetPosts',query:'query GetPosts { posts { pageInfo { endCursor hasNextPage } } }',variables:{after:button.dataset.after,scope:SCOPE}})});const data=(await response.json()).data.posts;for(const node of data.nodes){const link=document.createElement('a');link.href=node.url;link.textContent=node.url;document.body.append(link)}button.dataset.after=data.pageInfo.endCursor;if(!data.pageInfo.hasNextPage)button.remove()}
            </script>""".replace('SCOPE',json.dumps(scope))
            page.route('https://yoto.space/**',lambda route:route.fulfill(content_type='text/html',body=html if route.request.url=='https://yoto.space/' else '<h1>A reachable public post without pagination</h1>'))
            def api(route):
                body=route.request.post_data_json;after=body['variables']['after'];calls.append((scope,after));number=int(after)+1
                route.fulfill(content_type='application/json',body=json.dumps({'data':{'posts':{'nodes':[{'url':'/post-'+str(number)}],'pageInfo':{'endCursor':str(number),'hasNextPage':number<3}}}}))
            page.route('https://yoto.space/graphql**',api)
            return page
        page=new_page();client=Client()
        with patch('space_crawl.PAGE_BATCH',1),patch('space_crawl.EXPANSION_BATCH',1),patch('space_crawl.time.sleep'):saved=deepcopy(discover(page,client))
        self.assertEqual(saved['frontier'],['https://yoto.space/post-2','https://yoto.space/'])
        self.assertNotIn('NeverPersist',json.dumps(saved));self.assertNotIn('secret',json.dumps(saved));page.close()
        # A new browser page uses a fresh anonymous session, then jumps to the
        # stored public cursor on its first matching pagination request.
        page=new_page()
        with patch('space_crawl.EXPANSION_BATCH',1),patch('space_crawl.time.sleep'):finished=discover(page,client,deepcopy(saved))
        self.assertTrue(finished['complete']);self.assertEqual(calls,[('a','1'),('a','2')]);page.close()
        # Different filter variables must not inherit another feed's cursor.
        page=new_page('b')
        with patch('space_crawl.PAGE_BATCH',1),patch('space_crawl.EXPANSION_BATCH',1),patch('space_crawl.time.sleep'):discover(page,client,{**deepcopy(saved),'frontier':['https://yoto.space/']})
        self.assertEqual(calls[-1],('b','1'));page.close()
