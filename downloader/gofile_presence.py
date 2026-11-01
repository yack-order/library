"""Normal public-page checks only. No Premium APIs or anti-bot workarounds."""
import re
from urllib.parse import urlsplit


def classify(text, filename, downloadable=False):
    if re.search(r'captcha|verify (?:that )?you are human|access denied|rate limit|password.required|temporarily unavailable',text,re.I):
        return 'unknown'
    if filename in text and downloadable:
        return 'present'
    if re.search(r'\b(?:this content does not exist|content not found|file not found|this file has been deleted|this folder does not exist)\b',text,re.I):
        return 'missing'
    return 'unknown'


def check_page(url, filename):
    p=urlsplit(url)
    if p.scheme!='https' or p.netloc!='gofile.io' or not re.fullmatch(r'/d/[A-Za-z0-9_-]+',p.path) or p.query or p.fragment:
        return 'unknown'
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True)
            context=browser.new_context(accept_downloads=False)
            page=context.new_page()
            result='unknown'
            # Missing must be observed twice, not inferred from an empty/loading shell.
            for attempt in range(2):
                response=page.goto(url,wait_until='domcontentloaded',timeout=30000)
                if not response or response.status>=500 or urlsplit(page.url).hostname!='gofile.io':return 'unknown'
                for check in range(15):
                    text=page.locator('body').inner_text(timeout=3000)
                    downloadable=page.get_by_role('button',name='Download',exact=True).count()>0
                    result=classify(text,filename,downloadable)
                    if result!='unknown':break
                    page.wait_for_timeout(1000)
                if result!='missing':break
                if attempt==0:page.wait_for_timeout(2000)
            context.close();browser.close();return result
    except Exception:
        return 'unknown'
