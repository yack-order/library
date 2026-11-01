"""Opaque-ID job runner for inventory, sanitizing imports and cold-to-hot transfers."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid

from runner import ArchiveError, Client, digest, upload, verify_upload
from drive_storage import Drive
from archive_inspector import inspect_archive
from gofile_presence import check_page


class StorageClient(Client):
    def __init__(self):
        super().__init__()
        self.base=self.base.replace('/archive/jobs/','/archive/drive/jobs/')


def inventory(client,source):
    drive=Drive(source['readToken'],refresh=lambda:client.call('source')['readToken'])
    cursor=source['checkpoint'].get('cursor') or {'queue':[source['sourceFolder']],'parent':None,'page':None,'seen':[]}
    while cursor['queue'] or cursor['parent']:
        if not cursor['parent']:
            cursor['parent']=cursor['queue'].pop(0);cursor['page']=None
        parent=cursor['parent']
        response=drive.json('files',q="'%s' in parents and trashed=false" % parent,
                            pageSize=25,pageToken=cursor['page'] or '',supportsAllDrives='true',includeItemsFromAllDrives='true',
                            fields='nextPageToken,files(id,name,mimeType,size,version,parents,capabilities(canDownload))')
        files=[]
        for item in response.get('files',[]):
            if parent not in item.get('parents',[]):raise ArchiveError('source_changed')
            if item.get('mimeType')=='application/vnd.google-apps.folder':
                if item['id'] not in cursor['seen']:
                    cursor['queue'].append(item['id']);cursor['seen'].append(item['id'])
                    if len(cursor['seen'])>1000:raise ArchiveError('resource_limit')
            elif item.get('name','').lower().endswith('.7z'):
                size=int(item.get('size',0))
                if 0<size<=20*1024**3:
                    files.append({'id':item['id'],'version':str(item.get('version','')),'parent':parent,'name':item['name'],'bytes':size})
        cursor['page']=response.get('nextPageToken')
        if not cursor['page']:cursor['parent']=None
        client.call('inventory-page',{'files':files,'cursor':cursor})
        if client.lost.is_set():raise ArchiveError('source_unavailable')
    client.call('complete',{})


def seed(client,source,work):
    checkpoint=source['checkpoint']
    if checkpoint.get('coldReceipt'):
        client.call('complete',{});return
    candidate=source['candidate']
    read=Drive(source['readToken'],refresh=lambda:client.call('source')['readToken'])
    # Direct parent comes from a source-folder-only inventory; shortcuts are ignored.
    metadata=read.metadata(candidate['source_file'])
    if candidate['source_parent'] not in metadata.get('parents',[]):raise ArchiveError('source_changed')
    original=work/candidate['filename']
    if original.name!=candidate['filename'] or '/' in candidate['filename'] or '\\' in candidate['filename']:
        raise ArchiveError('unsafe_archive')
    read.download(candidate['source_file'],original,expected_version=candidate['source_version'])
    result=inspect_archive(original,work)
    inspection={key:result[key] for key in ['cardId','variant','sha256','bytes','title','tracks']}
    if source['kind']=='inspect':
        client.call('inspection',inspection);client.call('complete',{});return
    if candidate['card_id']!=result['cardId']:raise ArchiveError('identity_conflict')
    if result.get('rawMetadata') and not checkpoint.get('metadataCaptured'):
        # Exact source bytes stay in private Toyo capture, never stdout/artifacts.
        import requests
        raw=Path(result['rawMetadata']).read_bytes()
        response=requests.post(client.base+'/metadata',data=raw,headers={**client.headers,'Content-Type':'application/json'},timeout=(10,120),allow_redirects=False)
        try:
            if response.status_code!=200:raise ArchiveError('identity_conflict')
        finally:response.close()
    write=Drive(source['writeToken'],lambda value:client.call('checkpoint',value),refresh=lambda:client.call('source')['writeToken'])
    receipt=write.upload(result['path'],source['destinationFolder'],os.environ['TOYO_ARCHIVE_JOB'],result['cardId'],result['variant'],result['index'],checkpoint)
    client.call('cold-receipt',receipt);client.call('complete',{})


def deliver(client,source,work):
    checkpoint=source['checkpoint'];object=source['object']
    if checkpoint.get('hotReceipt'):
        verify_upload(checkpoint['hotReceipt']);client.call('complete',{'presence':'missing'});return
    presence=check_page(source['hot']['pageUrl'],object['filename']) if source.get('hot') else 'missing'
    client.call('page-result',{'presence':presence})
    if presence=='unknown':raise ArchiveError('page_unknown')
    if presence=='present':client.call('complete',{'presence':'present'});return
    drive=Drive(source['runtimeToken'],refresh=lambda:client.call('source')['runtimeToken']);path=work/object['filename']
    if path.name!=object['filename']:raise ArchiveError('unsafe_archive')
    metadata=drive.download(object['drive_file_id'],path,maximum=object['bytes'],expected_version=object['drive_version'])
    if source['destinationFolder'] not in metadata.get('parents',[]) or digest(path)!=object['sha256']:
        raise ArchiveError('verification_failed')
    upload(path,source['gofileToken'],on_uploaded=lambda receipt:client.call('hot-receipt',receipt))
    client.call('complete',{'presence':'missing'})


def handover(client,source,work):
    drive=Drive(source['runtimeToken'],lambda value:client.call('checkpoint',value),refresh=lambda:client.call('source')['runtimeToken'])
    if not source['checkpoint'].get('writeProbeVerified'):
        import py7zr
        probe=work/('toyo-handover-'+os.environ['TOYO_ARCHIVE_JOB']+'.7z')
        text=work/'probe.txt';text.write_text('Toyo private archive write/readback probe\n')
        with py7zr.SevenZipFile(probe,'w') as archive:archive.write(text,'probe.txt')
        receipt=drive.upload(probe,source['destinationFolder'],os.environ['TOYO_ARCHIVE_JOB'],'probe','full',{'title':'Private write/readback probe','tracks':1},source['checkpoint'])
        client.call('write-probe',receipt)
    while True:
        objects=source.get('objects',[])
        if not objects:break
        checked=[]
        for object in objects:
            current=drive.metadata(object['drive_file_id']);version=str(current['version'])
            changed=version!=object['drive_version']
            metadata=drive.download(object['drive_file_id'],work/'read-probe',maximum=object['bytes'],expected_version=version,probe=not changed)
            if source['destinationFolder'] not in metadata.get('parents',[]):raise ArchiveError('verification_failed')
            item={'id':object['id'],'driveVersion':version}
            if changed:
                checksum=digest(work/'read-probe')
                if checksum!=object['sha256']:raise ArchiveError('verification_failed')
                item['verifiedSha256']=checksum
            checked.append(item)
        client.call('handover-page',{'objects':checked});source=client.call('source')
        if client.lost.is_set():raise ArchiveError('source_unavailable')
    client.call('complete',{})


def main():
    client=None
    try:
        client=StorageClient();claimed=client.call('claim');client.headers['X-Archive-Lease']=claimed['leaseToken']
        heartbeat=threading.Thread(target=client.heartbeats,daemon=True);heartbeat.start()
        with tempfile.TemporaryDirectory(prefix='toyo-storage-') as directory:
            source=client.call('source');work=Path(directory)
            if source['kind']=='inventory':inventory(client,source)
            elif source['kind'] in ('inspect','import'):seed(client,source,work)
            elif source['kind']=='delivery':deliver(client,source,work)
            elif source['kind']=='handover':handover(client,source,work)
            else:raise ArchiveError('processing_failed')
        print('Storage job completed');return 0
    except Exception as error:
        code=error.code if isinstance(error,ArchiveError) else 'processing_failed'
        if client and 'X-Archive-Lease' in client.headers:
            try:client.call('fail',{'code':code})
            except Exception:pass
        print('Storage job failed: '+code);return 1
    finally:
        if client:client.stopped.set()


if __name__=='__main__':raise SystemExit(main())
