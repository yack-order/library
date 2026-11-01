"""Synthetic offline verification: no playlists or provider uploads are used."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
import py7zr
from mutagen.id3 import ID3
import runner

class ArchiveTests(unittest.TestCase):
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
            output=work/'output';output.mkdir()
            with patch.object(runner,'stream_file',side_effect=download):
                archive=runner.build_archive({'card':raw,'publicCard':public},output)
            self.assertIn('[DEMO][abc12]',archive.name)
            extracted=work/'extracted'
            with py7zr.SevenZipFile(archive,'r') as handle:
                self.assertIsNone(handle.testzip())
            with py7zr.SevenZipFile(archive,'r') as handle:
                handle.extractall(extracted)
            folder=extracted/'Example'
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
