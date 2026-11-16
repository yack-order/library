"""Normal public-page checks only. No Premium APIs or anti-bot workarounds."""
import re
from pathlib import Path
import subprocess
import sys
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
            # Imports never need Chromium. Delivery installs it only when a
            # previous Gofile page actually needs a browser presence check.
            if not Path(playwright.chromium.executable_path).is_file():
                subprocess.run([sys.executable,'-m','playwright','install','--with-deps','chromium'],
                               check=True,timeout=600)
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


def download_page(url, filename, destination, expected_bytes):
    """Download only through the normal public page; ambiguity and challenges stop recovery."""
    p=urlsplit(url)
    if p.scheme!='https' or p.netloc!='gofile.io' or not re.fullmatch(r'/d/[A-Za-z0-9_-]+',p.path) or p.query or p.fragment:
        raise RuntimeError('podcast_previous_archive_unavailable')
    if type(expected_bytes) is not int or not 0<expected_bytes<=20*1024**3:raise RuntimeError('resource_limit')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            subprocess.run([sys.executable,'-m','playwright','install','--with-deps','chromium'],check=True,timeout=600)
        browser=playwright.chromium.launch(headless=True)
        context=browser.new_context(accept_downloads=True)
        try:
            page=context.new_page();response=page.goto(url,wait_until='domcontentloaded',timeout=30000)
            if not response or response.status>=400:raise RuntimeError('podcast_previous_archive_unavailable')
            page.get_by_text(filename,exact=True).wait_for(timeout=30000)
            buttons=page.get_by_role('button',name='Download',exact=True)
            if buttons.count()!=1 or classify(page.locator('body').inner_text(),filename,True)!='present':raise RuntimeError('podcast_previous_archive_unavailable')
            with page.expect_download(timeout=600000) as pending:buttons.click()
            download=pending.value
            if download.suggested_filename!=filename:raise RuntimeError('podcast_previous_archive_unavailable')
            target=urlsplit(download.url)
            if target.scheme!='https' or (target.hostname!='gofile.io' and not (target.hostname or '').endswith('.gofile.io')) or target.username or target.password or target.port:raise RuntimeError('podcast_previous_archive_unavailable')
            direct=download.url;download.cancel()
            from runner import stream_file
            stream_file(direct,destination,maximum=expected_bytes,allowed={'application/x-7z-compressed','application/octet-stream','binary/octet-stream'},attempts=1)
            if destination.stat().st_size!=expected_bytes:raise RuntimeError('verification_failed')
        finally:
            context.close();browser.close()
