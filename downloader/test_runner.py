"""Synthetic offline verification: no playlists or provider uploads are used."""
import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import runpy
import tempfile
import unittest
from unittest.mock import patch, Mock
import py7zr
from mutagen.id3 import ID3
import runner

class ArchiveTests(unittest.TestCase):
    def test_drive_build_releases_unpacked_workspace_before_restore_without_overwriting(self):
        from archive_inspector import safe_extract, verify_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sample=root/'sample.mp3'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2','-c:a','libmp3lame',str(sample)],check=True)
            work=root/'job';work.mkdir();sentinel=work/'retained-input';sentinel.write_bytes(b'keep')
            raw={'cardId':'abc12','title':'Example','content':{'chapters':[{'tracks':[{'title':'Track','trackUrl':'https://media.test/opaque'}]}]}}
            public=copy.deepcopy(raw);del public['content']['chapters'][0]['tracks'][0]['trackUrl']
            def download(url,path,**kwargs):Path(path).write_bytes(sample.read_bytes());return 'audio/mpeg'
            with patch.object(runner,'stream_file',side_effect=download):
                archive=runner.build_drive_archive({'card':raw,'publicCard':public},work)
                self.assertEqual(set(work.iterdir()),{archive,sentinel})
                original=archive.read_bytes()
                with self.assertRaises(FileExistsError):runner.build_drive_archive({'card':raw,'publicCard':public},work)
                self.assertEqual(archive.read_bytes(),original);self.assertEqual(sentinel.read_bytes(),b'keep')
                self.assertEqual(set(work.iterdir()),{archive,sentinel})
            restored=root/'restored';restored.mkdir();safe_extract(archive,restored)
            verify_manifest(restored/'Example',required=True)
            self.assertEqual(len(list(restored.rglob('*.mp3'))),1)

    def test_layout_privacy_tags_and_checksums(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            sample = work/'sample.mp3'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2','-c:a','libmp3lame',str(sample)],check=True)
            raw = {'cardId':'abc12','title':'Example','userId':'yoto','email':'private@example.test',
                   'metadata':{'author':'Published Author','category':'Stories','description':'Discover: 1 track'},
                   'content':{'chapters':[{'tracks':[{'title':'Track','trackUrl':'https://media.example.test/private?token=credential','duration':1,'key':'1'}]}]}}
            public = copy.deepcopy(raw); public.pop('email');public['userId']='yoto'; public['content']['chapters'][0]['tracks'][0]['trackUrl']='redacted'
            def download(url,path,**kwargs):
                Path(path).write_bytes(sample.read_bytes());return 'audio/mpeg'
            supplements=work/'publisher-documents';supplements.mkdir()
            (supplements/'Transcript.pdf').write_bytes(b'%PDF synthetic publisher transcript')
            output=work/'output';output.mkdir()
            with patch.object(runner,'stream_file',side_effect=download):
                archive=runner.build_archive({'card':raw,'publicCard':public,'supplementFolder':str(supplements)},output)
            self.assertIn('[DEMO][abc12]',archive.name)
            extracted=work/'extracted'
            with py7zr.SevenZipFile(archive,'r') as handle:
                self.assertIsNone(handle.testzip())
            with py7zr.SevenZipFile(archive,'r') as handle:
                handle.extractall(extracted)
            folder=extracted/'Example'
            self.assertEqual((folder/'supplements'/'Transcript.pdf').read_bytes(),b'%PDF synthetic publisher transcript')
            self.assertTrue((folder/'card.txt').exists());self.assertTrue((folder/'tracks.txt').exists())
            audio=list((folder/'audio_files').glob('*.mp3'))[0]
            self.assertEqual(str(ID3(audio)['TPE1']),'Published Author')
            self.assertEqual(str(ID3(audio)['TRCK']),'1/1')
            manifest=json.loads((folder/'manifest.json').read_text())
            for name,entry in manifest['files'].items():
                self.assertEqual(hashlib.sha256((folder/name).read_bytes()).hexdigest(),entry['sha256'])
            for path in folder.rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'credential',path.read_bytes());self.assertNotIn(b'private@example.test',path.read_bytes())
    def test_capture_failure_reports_kind_without_private_resource_urls(self):
        for kind in ['image', 'audio']:
            private = 'https://media.example.test/private?token=private-token'
            raw = {'content': {'chapters': [{'tracks': [{'trackUrl': private}]}]}}
            if kind == 'image': raw['metadata'] = {'cover': {'imageL': private}}
            with tempfile.TemporaryDirectory() as tmp, patch.object(runner, 'stream_file', side_effect=runner.ArchiveError('media_access_denied')), patch('builtins.print') as output:
                with self.assertRaises(runner.ArchiveError):
                    runner.capture_space_media({'card': raw}, Path(tmp))
                self.assertEqual(output.call_args.args, ('Archive resource failed: ' + kind,))
                self.assertNotIn('private-token', str(output.call_args_list))

    def test_worker_held_gofile_token_is_used_in_memory_only(self):
        client = Mock()
        client.headers = {}; client.lost.is_set.return_value = False
        client.call.side_effect = [
            {'leaseToken':'private-lease'},
            {'card':{},'publicCard':{},'gofileToken':'private-gofile-token'},
            {'ok':True}, {'ok':True}
        ]
        with patch.object(runner,'Client',return_value=client), patch.object(runner,'build_archive',return_value=Path('/tmp/synthetic.7z')) as build, patch.object(runner,'upload',return_value={'pageUrl':'https://gofile.io/d/synthetic'}) as upload, patch.object(runner.threading,'Thread'), patch('builtins.print') as output:
            self.assertEqual(runner.main(),0)
            self.assertEqual(upload.call_args.args,(Path('/tmp/synthetic.7z'),'private-gofile-token'))
            self.assertIn('on_uploaded', upload.call_args.kwargs)
            self.assertNotIn('gofileToken', build.call_args.args[0])
            self.assertNotIn('private-gofile-token',str(output.call_args_list))

    def receipt(self, payload=b'verified archive bytes'):
        md5=hashlib.md5(payload).hexdigest()
        return {'filename':'Example.7z','bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest(),
                'contentId':'file-id','pageUrl':'https://gofile.io/d/example','parentFolder':'folder-id',
                'md5':md5,'providerMd5':md5,'providerBytes':len(payload),'uploadedAt':1234567890000,
                'verification':'upload-receipt'}

    def test_free_receipt_resumes_without_any_provider_request(self):
        receipt=self.receipt()
        with patch.object(runner.requests,'get') as get, patch.object(runner,'stream_file') as download:
            result=runner.verify_upload(receipt,'owner-token')
            self.assertEqual(result,receipt)
            get.assert_not_called();download.assert_not_called()

    def test_receipt_resume_needs_no_source_fetch_or_upload(self):
        client=Mock();client.headers={};client.lost.is_set.return_value=False
        receipt=self.receipt()
        client.call.side_effect=[{'leaseToken':'lease','uploadReceipt':receipt},{'ok':True},{'ok':True}]
        with patch.object(runner,'Client',return_value=client), patch.object(runner.threading,'Thread'), patch.object(runner,'build_archive') as build, patch.object(runner,'upload') as upload:
            self.assertEqual(runner.main(),0)
            self.assertEqual([c.args[0] for c in client.call.call_args_list],['claim','checkpoint','complete'])
            build.assert_not_called();upload.assert_not_called()

    def test_legacy_and_incomplete_receipts_stay_unavailable(self):
        for field in ['md5','providerMd5','providerBytes','uploadedAt','verification']:
            receipt=self.receipt();receipt.pop(field)
            with self.assertRaises(runner.ArchiveError) as error:runner.verify_upload(receipt)
            self.assertEqual(error.exception.code,'verification_unavailable')

    def response(self, status='ok', data=None, http=200):
        response=Mock();response.status_code=http
        response.json.return_value={'status':status,'data':data or {}}
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        return response

    def test_free_upload_matches_receipt_and_never_lists_content(self):
        payload=b'locally verified archive';receipt=self.receipt(payload)
        data={'id':'file-id','parentFolder':'folder-id','downloadPage':receipt['pageUrl'],
              'size':len(payload),'md5':receipt['md5'].upper()}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'Example.7z';path.write_bytes(payload)
            saved=[]
            def persist(receipt):saved.append(receipt.copy());return receipt
            with patch.object(runner.requests,'post',return_value=self.response(data=data)) as post, patch.object(runner.requests,'get') as get:
                result=runner.upload(path,'private-owner-token',on_uploaded=persist)
                self.assertEqual(result['verification'],'upload-receipt')
                self.assertEqual(result['providerMd5'],receipt['md5'])
                self.assertEqual(saved[0],result)
                self.assertEqual(post.call_args.args[0],'https://upload.gofile.io/uploadfile')
                self.assertEqual(post.call_args.kwargs['headers']['Authorization'],'Bearer private-owner-token')
                get.assert_not_called()

    def test_mismatched_upload_is_saved_but_cannot_complete(self):
        payload=b'archive'
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'Example.7z';path.write_bytes(payload)
            for data in [{'size':len(payload)+1,'md5':hashlib.md5(payload).hexdigest()},
                         {'size':len(payload),'md5':'0'*32}, {'size':len(payload)}]:
                data.update({'id':'file-id','parentFolder':'folder-id','downloadPage':'https://gofile.io/d/example'})
                saved=Mock(side_effect=lambda x:x)
                with patch.object(runner.requests,'post',return_value=self.response(data=data)):
                    with self.assertRaises(runner.ArchiveError):runner.upload(path,'owner-token',on_uploaded=saved)
                saved.assert_called_once()

    def test_upload_transport_failures_are_distinct_and_never_retried_or_exposed(self):
        failures=[(runner.requests.exceptions.ConnectTimeout,'upload_connect_timeout'),
                  (runner.requests.exceptions.ReadTimeout,'upload_response_timeout'),
                  (runner.requests.exceptions.ConnectionError,'upload_connection_failed'),
                  (ValueError,'upload_response_invalid')]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'Example.7z';path.write_bytes(b'archive')
            for error,code in failures:
                with self.subTest(code=code),patch.object(runner.requests,'post',side_effect=error('secret-url-and-token')) as post:
                    with self.assertRaises(runner.ArchiveError) as caught:runner.upload(path,'private-owner-token')
                    self.assertEqual(caught.exception.code,code)
                    self.assertNotIn('secret-url-and-token',str(caught.exception))
                    self.assertEqual(post.call_count,1)

    def test_plain_text_rate_limit_is_classified_without_parsing_or_retrying(self):
        response=self.response(http=429);response.json.side_effect=ValueError('private response')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'Example.7z';path.write_bytes(b'archive')
            with patch.object(runner.requests,'post',return_value=response) as post:
                with self.assertRaises(runner.ArchiveError) as caught:runner.upload(path,'private-token')
                self.assertEqual(caught.exception.code,'provider_rate_limited');response.json.assert_not_called();self.assertEqual(post.call_count,1)

    def test_provider_json_errors_are_classified_even_with_http_200(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'Example.7z';path.write_bytes(b'archive')
            for status,code in [('error-notPremium','provider_plan_required'),('error-token','upload_auth_required'),
                                ('error-limits','provider_quota_exceeded'),('error-rateLimit','provider_rate_limited')]:
                with patch.object(runner.requests,'post',return_value=self.response(status=status)), patch('builtins.print') as output:
                    with self.assertRaises(runner.ArchiveError) as error:runner.upload(path,'private-owner-token')
                    self.assertEqual(error.exception.code,code)
                    self.assertNotIn('private-owner-token',str(output.call_args_list))

    def test_radio_and_failed_media_never_produce_an_archive(self):
        raw={'cardId':'abc12','title':'Radio','content':{'playbackType':'radio','chapters':[{'tracks':[{'title':'Live','trackUrl':'https://example.test/live'}]}]}}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(runner.ArchiveError):
                runner.build_archive({'card':raw,'publicCard':raw},Path(tmp))
            self.assertEqual(list(Path(tmp).glob('*.7z')),[])
    def test_invalid_network_destinations_are_rejected(self):
        for url in ['http://example.test/a','https://user:pass@example.test/a','https://127.0.0.1/a','https://[::1]/a']:
            with self.assertRaises(runner.ArchiveError):runner.public_url(url)

if __name__=='__main__':unittest.main()

class SpaceVersionTests(unittest.TestCase):
    def test_uses_captured_bytes_and_version_suffix_without_refetching(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            sample = work / 'sample.mp3'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2','-c:a','libmp3lame',str(sample)],check=True)
            image = work / 'image.png';Image.new('RGB',(16,16),'red').save(image)
            raw = {'cardId':'abc12','title':'Versioned','metadata':{'cover':{'imageL':'https://images.test/cover.png'}},'content':{'chapters':[{'tracks':[{'title':'First','type':'audio','trackUrl':'https://media.test/audio.mp3'}]}]}}
            def download(url, path, **kwargs):
                Path(path).write_bytes((image if 'images.test' in url else sample).read_bytes())
                return 'image/png' if 'images.test' in url else 'audio/mpeg'
            with patch.object(runner,'stream_file',side_effect=download):
                captures, hashes = runner.capture_space_media({'card':raw},work)
            self.assertEqual(hashes['audio:0'],runner.digest(sample))
            self.assertEqual(hashes['image:0'],runner.digest(image))
            public = copy.deepcopy(raw);del public['content']['chapters'][0]['tracks'][0]['trackUrl']
            output = work/'out';output.mkdir()
            with patch.object(runner,'stream_file',side_effect=AssertionError('must use frozen bytes')):
                archive = runner.build_archive({'card':raw,'publicCard':public,'playlistVersion':{'number':3}},output,captures)
            self.assertTrue(archive.name.endswith(' (v3).7z'))
            extracted=work/'extracted'
            with py7zr.SevenZipFile(archive,'r') as handle:handle.extractall(extracted)
            self.assertEqual(len(list(extracted.rglob('*.mp3'))),1)
            for file in extracted.rglob('*'):
                if file.is_file() and file.suffix in {'.json','.txt'}:
                    self.assertNotIn('media.test',file.read_text())

class OggArchiveTests(unittest.TestCase):
    def test_binary_mime_audio_is_recognized_from_finite_supported_bytes(self):
        for suffix, codec, expected in [('.mp3','libmp3lame','audio/mpeg'),('.m4a','aac','audio/mp4'),
                                      ('.aac','aac','audio/aac'),('.ogg','libopus','audio/ogg')]:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                sample=Path(tmp)/('sample'+suffix)
                subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2','-c:a',codec,str(sample)],check=True)
                payload=sample.read_bytes();response=Mock(status_code=200,is_redirect=False)
                response.headers={'Content-Type':'application/octet-stream','Content-Length':str(len(payload))}
                response.iter_content.return_value=[payload]
                response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
                session=Mock();session.get.return_value=response;session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
                with patch.object(runner,'public_url',side_effect=lambda url:url),patch.object(runner.requests,'Session',return_value=session):
                    target=Path(tmp)/'download'
                    self.assertEqual(runner.stream_file('https://media.test/opaque',target,allowed=set(runner.AUDIO_FORMATS)),expected)
                    self.assertEqual(target.read_bytes(),payload)

    def test_binary_mime_does_not_admit_html_images_or_live_streams(self):
        for payload,allowed,extra,code in [(b'<html>not audio</html>',set(runner.AUDIO_FORMATS),{},'media_format_unsupported'),
                (b'not an image',{'image/png'},{},'media_format_unsupported'),
                (b'unbounded stream',set(runner.AUDIO_FORMATS),{'icy-name':'Radio'},'media_live_unsupported')]:
            with self.subTest(code=code,allowed=allowed), tempfile.TemporaryDirectory() as tmp:
                response=Mock(status_code=200,is_redirect=False)
                response.headers={'Content-Type':'application/octet-stream',**extra};response.iter_content.return_value=[payload]
                response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
                session=Mock();session.get.return_value=response;session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
                with patch.object(runner,'public_url',side_effect=lambda url:url),patch.object(runner.requests,'Session',return_value=session),patch.object(runner.time,'sleep') as sleep:
                    target=Path(tmp)/'download'
                    with self.assertRaises(runner.ArchiveError) as error:runner.stream_file('https://media.test/opaque',target,allowed=allowed)
                    self.assertEqual(error.exception.code,code);self.assertFalse(target.exists());sleep.assert_not_called()

    def test_finite_podcast_streams_keep_audio_order_tags_cover_and_restore_support(self):
        import base64
        from mutagen import File as AudioFile
        from mutagen.flac import Picture
        from PIL import Image
        from archive_inspector import sanitize_archive
        for codec in ['vorbis', 'libopus']:
            with self.subTest(codec=codec), tempfile.TemporaryDirectory() as tmp:
                work=Path(tmp);sample=work/'sample.ogg'
                subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2','-metadata','comment=https://private.test/play?token=secret','-ac','2','-c:a',codec,'-strict','-2',str(sample)],check=True)
                image=work/'cover.png';Image.new('RGB',(16,16),'red').save(image)
                raw={'cardId':'ogg12','title':'Ogg playlist','metadata':{'author':'Published Author','cover':{'imageL':'https://images.test/cover.png'}},'content':{'chapters':[{'tracks':[{'title':'First','type':'stream','trackUrl':'https://private.test/first?token=secret'},{'title':'Second','type':'stream','trackUrl':'https://private.test/second?token=secret'}]}]}}
                public=copy.deepcopy(raw)
                for track in public['content']['chapters'][0]['tracks']:del track['trackUrl']
                def download(url,path,**kwargs):
                    is_image='images.test' in url
                    Path(path).write_bytes((image if is_image else sample).read_bytes())
                    return 'image/png' if is_image else 'audio/ogg'
                with patch.object(runner,'stream_file',side_effect=download):
                    captures,hashes=runner.capture_space_media({'card':raw},work)
                self.assertEqual(hashes['audio:0'],runner.digest(sample))
                output=work/'output';output.mkdir()
                archive=runner.build_archive({'card':raw,'publicCard':public},output,captures)
                extracted=work/'extracted'
                with py7zr.SevenZipFile(archive,'r') as handle:handle.extractall(extracted)
                tracks=sorted(extracted.rglob('*.ogg'));self.assertEqual(len(tracks),2)
                for number,path in enumerate(tracks,1):
                    audio=AudioFile(path);self.assertEqual(audio['tracknumber'],[str(number)])
                    self.assertEqual(audio['artist'],['Published Author']);self.assertNotIn('comment',audio)
                    self.assertGreater(audio.info.length,0)
                    cover=Picture(base64.b64decode(audio['metadata_block_picture'][0]));self.assertEqual(cover.data,next(extracted.rglob('cover.png')).read_bytes())
                    self.assertNotIn(b'token=secret',path.read_bytes())
                inspect=work/'inspection';inspect.mkdir()
                result=sanitize_archive(archive,inspect)
                self.assertEqual(result['tracks'],2)

    def test_denied_missing_and_unsupported_media_are_safe_terminal_diagnostics(self):
        for status,mime,expected in [(403,'text/html','media_access_denied'),(404,'text/html','media_unavailable'),(200,'text/html','media_format_unsupported')]:
            with self.subTest(status=status,mime=mime), tempfile.TemporaryDirectory() as tmp:
                response=Mock();response.status_code=status;response.is_redirect=False
                response.headers={'Content-Type':mime};response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
                session=Mock();session.get.return_value=response;session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
                with patch.object(runner,'public_url',side_effect=lambda url:url),patch.object(runner.requests,'Session',return_value=session),patch.object(runner.time,'sleep') as sleep:
                    with self.assertRaises(runner.ArchiveError) as error:runner.stream_file('https://private.test/source?token=secret',Path(tmp)/'partial',allowed=set(runner.AUDIO_FORMATS))
                    self.assertEqual(error.exception.code,expected);self.assertNotIn('secret',str(error.exception))
                    self.assertEqual(session.get.call_count,1);sleep.assert_not_called()
                    self.assertFalse((Path(tmp)/'partial').exists())


class ArchiveEntrypointTests(unittest.TestCase):
    def test_script_entrypoint_preserves_imported_helper_failure_codes(self):
        from archive_inspector import ArchiveError as HelperError
        for code in ('resource_limit', 'unsafe_archive', 'verification_failed'):
            with self.subTest(code=code):
                namespace=runpy.run_path(runner.__file__,run_name='archive_script_fixture')
                self.assertIs(namespace['ArchiveError'],HelperError)
                client=Mock();client.headers={};client.heartbeats=Mock()
                def call(action,value=None):
                    if action=='claim':return {'leaseToken':'synthetic-private-lease'}
                    if action=='source':return {'storageMode':'drive','driveToken':'synthetic-private-token','driveFolder':'synthetic-folder'}
                    return {}
                client.call.side_effect=call
                output=io.StringIO()
                with patch.dict(namespace['main'].__globals__,{'Client':lambda:client,'build_archive':Mock(side_effect=HelperError(code))}),redirect_stdout(output):
                    self.assertEqual(namespace['main'](),1)
                client.call.assert_any_call('fail',{'code':code})
                self.assertIn('Archive job failed: '+code,output.getvalue())
                self.assertNotIn('synthetic-private',output.getvalue())


class ArchiveHttpClientTests(unittest.TestCase):
    def test_accepts_long_public_podcast_redirect_chain(self):
        redirects=[]
        for index in range(5):
            response=Mock();response.is_redirect=True;response.headers={'Location':f'https://tracking{index}.test/episode'}
            response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);redirects.append(response)
        audio=Mock();audio.is_redirect=False;audio.status_code=200
        audio.headers={'Content-Type':'audio/mpeg','Content-Length':'5'};audio.iter_content.return_value=[b'audio']
        audio.__enter__=Mock(return_value=audio);audio.__exit__=Mock(return_value=False)
        session=Mock();session.get.side_effect=[*redirects,audio]
        session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
        with tempfile.TemporaryDirectory() as tmp,patch.object(runner.requests,'Session',return_value=session),patch.object(runner,'public_url',side_effect=lambda url:url) as validated:
            path=Path(tmp)/'audio'
            runner.stream_file('https://podcast.test/episode',path,allowed=set(runner.AUDIO_FORMATS))
            self.assertEqual(path.read_bytes(),b'audio')
            self.assertEqual(session.get.call_count,6);self.assertEqual(validated.call_count,6)

    def test_redirect_loop_remains_bounded_and_removes_partial_file(self):
        response=Mock();response.is_redirect=True;response.headers={'Location':'https://podcast.test/loop?private=value'}
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        session=Mock();session.get.return_value=response
        session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
        with tempfile.TemporaryDirectory() as tmp,patch.object(runner.requests,'Session',return_value=session),patch.object(runner,'public_url',side_effect=lambda url:url):
            path=Path(tmp)/'audio';path.write_bytes(b'partial')
            with self.assertRaises(runner.ArchiveError) as error:runner.stream_file('https://podcast.test/start',path,attempts=1)
            self.assertEqual(error.exception.code,'media_failed');self.assertNotIn('private',str(error.exception))
            self.assertEqual(session.get.call_count,runner.MAX_MEDIA_REDIRECTS+1);self.assertFalse(path.exists())

    def test_longer_redirect_allowance_still_rejects_private_destination(self):
        response=Mock();response.is_redirect=True;response.headers={'Location':'https://127.0.0.1/private'}
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        session=Mock();session.get.return_value=response
        session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
        def addresses(host,*args,**kwargs):
            return [(runner.socket.AF_INET,runner.socket.SOCK_STREAM,6,'',('127.0.0.1' if host=='127.0.0.1' else '93.184.216.34',443))]
        with tempfile.TemporaryDirectory() as tmp,patch.object(runner.requests,'Session',return_value=session),patch.object(runner.socket,'getaddrinfo',side_effect=addresses):
            with self.assertRaises(runner.ArchiveError):runner.stream_file('https://podcast.test/start',Path(tmp)/'audio',attempts=1)
            self.assertEqual(session.get.call_count,1)

    def test_identifies_itself_on_each_validated_public_redirect_without_credentials(self):
        redirect=Mock();redirect.is_redirect=True;redirect.headers={'Location':'https://media.test/episode.mp3'}
        redirect.__enter__=Mock(return_value=redirect);redirect.__exit__=Mock(return_value=False)
        response=Mock();response.is_redirect=False;response.status_code=200
        response.headers={'Content-Type':'audio/mpeg','Content-Length':'5'};response.iter_content.return_value=[b'audio']
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        session=Mock();session.get.side_effect=[redirect,response]
        session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
        with tempfile.TemporaryDirectory() as tmp,patch.object(runner.requests,'Session',return_value=session),patch.object(runner,'public_url',side_effect=lambda url:url) as validated:
            path=Path(tmp)/'episode.mp3'
            self.assertEqual(runner.stream_file('https://podcast.test/episode',path,allowed=set(runner.AUDIO_FORMATS)),'audio/mpeg')
            self.assertEqual(path.read_bytes(),b'audio')
            self.assertEqual(validated.call_count,2)
            self.assertFalse(session.trust_env)
            for call in session.get.call_args_list:
                self.assertEqual(call.kwargs['headers']['User-Agent'],runner.ARCHIVE_USER_AGENT)
                self.assertEqual(call.kwargs['headers']['Accept-Encoding'],'identity')
                self.assertNotIn('Authorization',call.kwargs['headers'])
                self.assertFalse(call.kwargs['allow_redirects'])
