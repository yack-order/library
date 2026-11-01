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

    def test_owner_metadata_supplies_the_readback_url_without_premium_directlink_creation(self):
        payload=b'verified archive bytes'
        receipt={'filename':'Example.7z','bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest(),
                 'contentId':'file-id','pageUrl':'https://gofile.io/d/example','parentFolder':'folder-id','directLink':None}
        response=Mock();response.status_code=200
        response.json.return_value={'status':'ok','data':{'children':{'file-id':{'id':'file-id','link':'https://store.gofile.io/download/web/file-id/Example.7z'}}}}
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        def restore(url,path,**kwargs):
            self.assertEqual(kwargs['bearer'],'owner-token');Path(path).write_bytes(payload)
        with patch.object(runner.requests,'get',return_value=response) as get, patch.object(runner,'stream_file',side_effect=restore):
            result=runner.verify_upload(receipt,'owner-token')
            self.assertEqual(result['sha256'],receipt['sha256'])
            self.assertEqual(get.call_args.args[0],'https://api.gofile.io/contents/folder-id')
            self.assertNotIn('directLink',result)
    def test_readback_denial_stays_unverified(self):
        receipt={'parentFolder':'folder-id','directLink':None}
        response=Mock();response.status_code=403;response.json.return_value={'status':'error-notPremium'}
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        with patch.object(runner.requests,'get',return_value=response):
            with self.assertRaises(runner.ArchiveError) as error:runner.verify_upload(receipt,'owner-token')
            self.assertEqual(error.exception.code,'verification_unavailable')

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
