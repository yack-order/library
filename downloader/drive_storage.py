"""Bounded Drive transfers. OAuth refresh tokens remain in Toyo, never this runner."""
import hashlib
import json
from pathlib import Path
import re
import time
import tempfile
from urllib.parse import urlencode, urljoin, urlsplit

import requests
from runner import ArchiveError, digest, public_url

MAX_ARCHIVE = 20 * 1024**3
CHUNK = 8 * 1024**2  # A multiple of Google's 256 KiB resumable chunk unit.


class Drive:
    def __init__(self, token, checkpoint=None, refresh=None):
        if not token:
            raise ArchiveError('reconnect_required')
        self.token = token
        self.checkpoint = checkpoint or (lambda data: None)
        self.refresh=refresh

    def request(self,method,url,**kwargs):
        headers=dict(kwargs.pop('headers',{}))
        for attempt in range(2):
            headers['Authorization']='Bearer '+self.token
            response=getattr(requests,method)(url,headers=headers,**kwargs)
            if response.status_code!=401 or not self.refresh or attempt:return response
            response.close();self.token=self.refresh()
        raise ArchiveError('reconnect_required')

    def json(self, path, **params):
        url = 'https://www.googleapis.com/drive/v3/' + path
        if params:
            url += '?' + urlencode(params)
        try:
            with self.request('get',url,stream=True,timeout=(10, 30),allow_redirects=False) as response:
                if response.status_code != 200 or int(response.headers.get('Content-Length', 0)) > 2*1024**2:
                    raise ArchiveError('drive_failed')
                chunks=[];size=0
                for chunk in response.iter_content(65536):
                    size+=len(chunk)
                    if size>2*1024**2:raise ArchiveError('resource_limit')
                    chunks.append(chunk)
                return json.loads(b''.join(chunks))
        except ArchiveError:
            raise
        except Exception:
            raise ArchiveError('drive_failed') from None

    def metadata(self, file_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', file_id):
            raise ArchiveError('drive_failed')
        return self.json('files/'+file_id, supportsAllDrives='true',
                         fields='id,name,parents,version,size,md5Checksum,sha256Checksum,trashed,capabilities(canDownload),appProperties')

    def download(self, file_id, path, *, maximum=MAX_ARCHIVE, expected_version=None, probe=False):
        metadata = self.metadata(file_id)
        if metadata.get('trashed') or not metadata.get('capabilities', {}).get('canDownload'):
            raise ArchiveError('source_unavailable')
        if expected_version and str(metadata.get('version')) != str(expected_version):
            raise ArchiveError('source_changed')
        if int(metadata.get('size', 0)) > maximum:
            raise ArchiveError('resource_limit')
        url = 'https://www.googleapis.com/drive/v3/files/'+file_id+'?alt=media&supportsAllDrives=true'
        try:
            started = time.monotonic()
            for redirect in range(4):
                host = urlsplit(public_url(url)).hostname
                if host != 'www.googleapis.com' and not host.endswith('.googleusercontent.com'):
                    raise ArchiveError('drive_failed')
                headers = {'Accept-Encoding': 'identity'}
                if host == 'www.googleapis.com':
                    headers['Authorization'] = 'Bearer '+self.token
                if probe:
                    headers['Range'] = 'bytes=0-0'
                request=self.request('get',url,headers=headers,stream=True,timeout=(10,30),allow_redirects=False) if host=='www.googleapis.com' else requests.get(url,headers=headers,stream=True,timeout=(10,30),allow_redirects=False)
                with request as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers['Location']); continue
                    if response.status_code not in (200, 206) or ('text/html' in response.headers.get('Content-Type','')):
                        raise ArchiveError('drive_failed')
                    size = 0
                    with open(path, 'wb') as handle:
                        for chunk in response.iter_content(65536):
                            if probe:
                                if chunk: handle.write(chunk[:1]); return metadata
                            size += len(chunk)
                            if size > maximum or time.monotonic()-started > 1200:
                                raise ArchiveError('resource_limit')
                            handle.write(chunk)
                    if not size or size != int(metadata['size']):
                        raise ArchiveError('verification_failed')
                    if metadata.get('md5Checksum'):
                        with open(path, 'rb') as handle:
                            if hashlib.file_digest(handle, 'md5').hexdigest() != metadata['md5Checksum']:
                                raise ArchiveError('verification_failed')
                    return metadata
            raise ArchiveError('drive_failed')
        except ArchiveError:
            Path(path).unlink(missing_ok=True); raise
        except Exception:
            Path(path).unlink(missing_ok=True)
            raise ArchiveError('drive_failed') from None

    @staticmethod
    def session_url(url):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'www.googleapis.com' or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ArchiveError('drive_failed')
        if not parsed.path.startswith('/upload/drive/'):
            raise ArchiveError('drive_failed')
        return url

    def upload(self, path, folder, job_id, card_id, variant, index, resume=None):
        path=Path(path); sha=digest(path); size=path.stat().st_size
        if not 0 < size <= MAX_ARCHIVE: raise ArchiveError('resource_limit')
        resume=resume or {}
        # Locate an acknowledged write after interruption without repeating it.
        if resume.get('uploadedFile'):
            matches=[self.metadata(resume['uploadedFile'])]
        else:
            query="'%s' in parents and trashed=false and (appProperties has { key='toyoJob' and value='%s' } or appProperties has { key='toyoSha256' and value='%s' })" % (folder,job_id,sha)
            matches=self.json('files',q=query,pageSize=10,fields='files(id,name,parents,version,size,md5Checksum,sha256Checksum,appProperties)',supportsAllDrives='true',includeItemsFromAllDrives='true').get('files',[])
        if matches:
            file=matches[0]
            properties=file.get('appProperties',{})
            if folder not in file.get('parents',[]) or (properties.get('toyoJob')!=job_id and properties.get('toyoSha256')!=sha):
                raise ArchiveError('verification_failed')
            expected_sha=file['appProperties'].get('toyoSha256')
            return self.readback(file, path.parent, card_id, variant, index, expected_sha)
        headers={'Authorization':'Bearer '+self.token}
        session=resume.get('uploadSession') if resume.get('uploadSha256')==sha else None
        if not session:
            quota=self.json('about',fields='storageQuota').get('storageQuota',{})
            if quota.get('limit') and int(quota['limit'])-int(quota.get('usage',0))<size:
                raise ArchiveError('provider_quota_exceeded')
            metadata={'name':path.name,'parents':[folder], 'appProperties':{'toyoJob':job_id,'toyoSha256':sha}}
            with self.request('post','https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&supportsAllDrives=true',
                               headers={**headers,'Content-Type':'application/json','X-Upload-Content-Type':'application/x-7z-compressed','X-Upload-Content-Length':str(size)},
                               json=metadata,timeout=(10,30),allow_redirects=False) as response:
                if response.status_code not in (200,201) or 'Location' not in response.headers: raise ArchiveError('drive_failed')
                session=self.session_url(response.headers['Location'])
            self.checkpoint({'uploadSession':session,'uploadSha256':sha})
        session=self.session_url(session)
        offset=0
        # Query the server offset; expired sessions stop rather than silently fork.
        with self.request('put',session,headers={**headers,'Content-Length':'0','Content-Range':'bytes */'+str(size)},data=b'',timeout=(10,30),allow_redirects=False) as response:
            if response.status_code in (200,201):
                file_id=response.json()['id'];self.checkpoint({'uploadedFile':file_id})
                return self.readback(self.metadata(file_id),path.parent,card_id,variant,index,sha)
            if response.status_code != 308: raise ArchiveError('drive_failed')
            if response.headers.get('Range'):offset=int(response.headers['Range'].rsplit('-',1)[1])+1
        with open(path,'rb') as handle:
            handle.seek(offset)
            while offset<size:
                chunk=handle.read(min(CHUNK,size-offset))
                if not chunk:raise ArchiveError('verification_failed')
                end=offset+len(chunk)-1
                with self.request('put',session,headers={**headers,'Content-Type':'application/x-7z-compressed','Content-Length':str(len(chunk)),'Content-Range':f'bytes {offset}-{end}/{size}'},
                                  data=chunk,timeout=(10,120),allow_redirects=False) as response:
                    if response.status_code in (200,201):
                        file_id=response.json()['id'];self.checkpoint({'uploadedFile':file_id})
                        return self.readback(self.metadata(file_id),path.parent,card_id,variant,index,sha)
                    if response.status_code!=308:raise ArchiveError('drive_failed')
                    acknowledged=int(response.headers.get('Range','bytes=0-'+str(end)).rsplit('-',1)[1])+1
                    if acknowledged!=end+1:raise ArchiveError('drive_failed')
                    offset=acknowledged
        raise ArchiveError('drive_failed')

    def recover_uploaded(self, work, folder, job_id, card_id, progress):
        """Finish an acknowledged runner upload without fetching media or writing Drive."""
        try:
            return self._recover_uploaded(work,folder,job_id,card_id,progress)
        except ArchiveError:
            raise
        except (ValueError,KeyError,TypeError,AttributeError,OSError):
            raise ArchiveError('verification_failed') from None

    def _recover_uploaded(self, work, folder, job_id, card_id, progress):
        from archive_inspector import verify_generated_archive
        if not re.fullmatch(r'[A-Za-z0-9]{5}',str(card_id)) or not isinstance(progress,dict):
            raise ArchiveError('verification_failed')
        sha=progress.get('uploadSha256')
        if not isinstance(sha,str) or not re.fullmatch(r'[a-f0-9]{64}',sha):
            raise ArchiveError('verification_unavailable')
        if progress.get('uploadedFile'):
            file=self.metadata(progress['uploadedFile'])
        else:
            # An upload can finish before its final acknowledgement is saved.
            # Locate only an unambiguous completed original write; never start one.
            if not progress.get('uploadSession'):
                raise ArchiveError('verification_unavailable')
            self.session_url(progress['uploadSession'])
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',str(folder)) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',str(job_id)):
                raise ArchiveError('verification_failed')
            query="'%s' in parents and trashed=false and appProperties has { key='toyoJob' and value='%s' } and appProperties has { key='toyoSha256' and value='%s' }" % (folder,job_id,sha)
            found=self.json('files',q=query,pageSize=2,fields='files(id),nextPageToken',supportsAllDrives='true',includeItemsFromAllDrives='true')
            files=found.get('files',[])
            print(json.dumps({'event':'archive.retained_upload','completed_matches':len(files),'additional_matches':bool(found.get('nextPageToken'))}))
            if not files:raise ArchiveError('verification_unavailable')
            if len(files)!=1 or found.get('nextPageToken'):raise ArchiveError('verification_failed')
            file=self.metadata(files[0]['id'])
        properties=file.get('appProperties',{})
        filename=file.get('name','')
        if (file.get('trashed') or file.get('capabilities',{}).get('canDownload') is not True
                or folder not in file.get('parents',[]) or properties.get('toyoJob')!=job_id
                or properties.get('toyoSha256')!=sha or not isinstance(filename,str)
                or len(filename)>240 or re.search(r'[/\\\x00-\x1f]',filename)
                or not re.search(r'\['+re.escape(card_id)+r'\] \(\d{4}-\d{2}-\d{2}\)\.7z$',filename)):
            raise ArchiveError('verification_failed')
        with tempfile.TemporaryDirectory(prefix='retained-upload-',dir=work) as temporary:
            archive=Path(temporary)/'retained.7z'
            latest=self.download(file['id'],archive,expected_version=str(file['version']))
            if digest(archive)!=sha or archive.stat().st_size!=int(file.get('size',0)):
                raise ArchiveError('verification_failed')
            expanded=Path(temporary)/'verified';expanded.mkdir()
            verify_generated_archive(archive,expanded)
            root=next(expanded.iterdir())
            manifest=json.loads((root/'manifest.json').read_text())
            candidates=[p for p in root.glob('*.json') if p.name not in {'manifest.json','source-snapshot.json'}]
            if len(candidates)!=1 or candidates[0].stat().st_size>4*1024**2:
                raise ArchiveError('verification_failed')
            card=json.loads(candidates[0].read_text())
            if not isinstance(card,dict) or card.get('cardId')!=card_id or manifest.get('cardId')!=card_id:
                raise ArchiveError('verification_failed')
            tracks=[t for chapter in card.get('content',{}).get('chapters',[]) for t in chapter.get('tracks',[])]
            audio=root/'audio_files'
            if not 1<=len(tracks)<=2000 or not audio.is_dir() or len([p for p in audio.rglob('*') if p.is_file()])!=len(tracks):
                raise ArchiveError('verification_failed')
            with archive.open('rb') as handle:md5=hashlib.file_digest(handle,'md5').hexdigest()
            return {'cardId':card_id,'variant':'demo' if '[DEMO]' in filename else 'full',
                    'filename':filename,'bytes':archive.stat().st_size,'sha256':sha,'md5':md5,
                    'driveFileId':file['id'],'driveVersion':str(latest['version']),'verifiedSha256':sha,
                    'index':{'title':str(card.get('title','Untitled'))[:240],'tracks':len(tracks)}}

    def readback(self,file,work,card_id,variant,index,expected_sha):
        restored=Path(work)/'drive-readback.7z'
        try:
            latest=self.download(file['id'],restored,expected_version=str(file['version']))
            sha=digest(restored)
            if not expected_sha or sha!=expected_sha:raise ArchiveError('verification_failed')
            with open(restored,'rb') as handle:md5=hashlib.file_digest(handle,'md5').hexdigest()
            return {'cardId':card_id,'variant':variant,'filename':file['name'],'bytes':restored.stat().st_size,
                    'sha256':sha,'md5':md5,'driveFileId':file['id'],'driveVersion':str(latest['version']),
                    'verifiedSha256':sha,'index':index}
        finally:restored.unlink(missing_ok=True)
