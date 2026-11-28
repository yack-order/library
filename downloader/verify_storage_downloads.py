"""Verify real website -> public Gofile downloads after a storage pilot.

Input: private JSON {hot:{cardId,objectId,sha256,bytes},cold:{cardId,objectId,sha256,bytes}}.
TOYO_SESSION_COOKIE_FILE supplies the existing authorized Admin session.
Output records independently downloaded hashes; no URLs or credentials are printed.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import requests
from gofile_presence import download_page


def verify(input_path,output_path):
    proof=json.loads(Path(input_path).read_text())
    cookie=Path(os.environ['TOYO_SESSION_COOKIE_FILE']).read_text().strip()
    origin='https://ui.yackorder.org'
    session=requests.Session();session.headers.update({'Cookie':cookie,'Origin':origin,'Content-Type':'application/json'})
    def api(path,data=None):
        response=session.request('GET' if data is None else 'POST',origin+path,json=data,timeout=(10,60),allow_redirects=False)
        try:
            if response.status_code not in (200,202):raise RuntimeError('website_download_request_failed')
            return response.status_code,response.json()
        finally:response.close()
    results={}
    for kind in ('hot','cold'):
        evidence=proof[kind];card=evidence['cardId']
        if not isinstance(card,str) or len(card)!=5 or not card.isascii() or not card.isalnum():raise RuntimeError('invalid_card_identity')
        base='/api/v1/library/cards/'+card+'/archive'
        _,availability=api(base)
        if availability.get('bytes')!=evidence['bytes']:raise RuntimeError('website_archive_identity_changed')
        deadline=time.monotonic()+1800;unlock=None
        while time.monotonic()<deadline:
            if not unlock or unlock['expiresAt']/1000<time.time()+10:
                _,unlock=api(base+'/unlock',{});time.sleep(3.1)
            status,link=api(base+'/link',{'unlockToken':unlock['unlockToken']})
            if status==200:break
            time.sleep(5)
        else:raise RuntimeError('website_restore_did_not_finish')
        with tempfile.TemporaryDirectory(prefix='toyo-storage-download-') as directory:
            target=Path(directory)/'archive.7z'
            download_page(link['url'],link['filename'],target,evidence['bytes'])
            with target.open('rb') as handle:sha=hashlib.file_digest(handle,'sha256').hexdigest()
            if sha!=evidence['sha256']:raise RuntimeError('website_download_checksum_mismatch')
            results[kind]={'objectId':evidence['objectId'],'bytes':target.stat().st_size,'sha256':sha}
        if kind=='cold':
            _,admin=api('/api/v1/archive/drive/admin')
            job=next((j for j in admin['jobs'] if j['kind']=='delivery' and j['target_id']==evidence['objectId'] and j['state']=='complete'),None)
            if not job:raise RuntimeError('completed_cold_job_required')
            results[kind]['jobId']=job['id']
    path=Path(output_path)
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    os.fchmod(descriptor,0o600)
    with os.fdopen(descriptor,'w') as handle:json.dump(results,handle,indent=2)
    print('Verified website hot and cold downloads against the saved archive hashes.')


if __name__=='__main__':
    try:verify(*sys.argv[1:])
    except Exception:
        print('Website download verification failed; organization remains gated. Review privately.',file=sys.stderr)
        sys.exit(1)
