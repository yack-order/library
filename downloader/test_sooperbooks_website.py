import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

from PIL import Image
from archive_errors import ArchiveError
from archive_inspector import verify_generated_archive
from sooperbooks import build_sooperbooks_archive, media_url, LICENSE


class WebsiteArchiveTests(unittest.TestCase):
    def test_preserves_audio_artwork_order_and_attribution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            audio=root/'original.mp3'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2','-metadata','title=Publisher original',str(audio)],check=True)
            artwork=root/'original.png';Image.new('RGB',(3,3),'red').save(artwork)
            calls=[]
            def fetch(url,path,image=False):
                calls.append(url)
                if image:
                    dest=path.with_suffix('.png');shutil.copyfile(artwork,dest);return dest
                shutil.copyfile(audio,path);return path
            tracks=[{'sourceId':'Credits','title':'Opening Credits','order':0,'url':'https://media.sooperbooks.com/credits.mp3','cover':None,'yoto':None,'sourceUrl':None},
                    {'sourceId':'Chapter1','title':'Chapter One','order':1,'url':'https://media.sooperbooks.com/ch1.mp3','cover':None,'yoto':None,'sourceUrl':None}]
            source={'publicCard':{'cardId':'sooperbooks:Book1'},'sooperbooks':{'sourceId':'Book1','title':'Complete Book','sourceUrl':'https://sooperbooks.com/download/book/','license':LICENSE,'cover':'https://media.sooperbooks.com/art.png','tracks':tracks}}
            archive=build_sooperbooks_archive(source,root,fetch)
            extracted=root/'verified';extracted.mkdir();verify_generated_archive(archive,extracted)
            folder=next(extracted.iterdir());metadata=json.loads((folder/'metadata.json').read_text())
            self.assertEqual(metadata['cardId'],'sooperbooks:Book1')
            self.assertEqual([t['sourceId'] for t in metadata['content']['chapters'][0]['tracks']],['Credits','Chapter1'])
            for path in (folder/'audio_files').iterdir():self.assertEqual(path.read_bytes(),audio.read_bytes())
            self.assertEqual(next((folder/'images').iterdir()).read_bytes(),artwork.read_bytes())
            self.assertIn(LICENSE,(folder/'ATTRIBUTION.txt').read_text())
            self.assertGreater(metadata['content']['chapters'][0]['tracks'][0]['durationSeconds'],0)
            from drive_storage import Drive
            sha=hashlib.sha256(archive.read_bytes()).hexdigest()
            receipt={'id':'saved-file-id','name':archive.name,'parents':['folder-id'],'version':'1','size':str(archive.stat().st_size),'capabilities':{'canDownload':True},'appProperties':{'toyoJob':'original-job','toyoSha256':sha}}
            drive=Drive('private-token');drive.metadata=Mock(return_value=receipt)
            def restore(_id,target,**_kwargs):
                shutil.copyfile(archive,target);return receipt
            drive.download=Mock(side_effect=restore)
            with patch('drive_storage.requests.post') as post,patch('drive_storage.requests.put') as put:
                recovered=drive.recover_uploaded(root,'folder-id','original-job','sooperbooks:Book1',{'uploadedFile':'saved-file-id','uploadSha256':sha})
            self.assertEqual(recovered['cardId'],'sooperbooks:Book1');self.assertEqual(recovered['index']['tracks'],2)
            drive.download.assert_called_once_with('saved-file-id',drive.download.call_args.args[1],expected_version='1',expected_sha256=sha)
            post.assert_not_called();put.assert_not_called()
            path=next((folder/'audio_files').iterdir());path.write_bytes(b'changed')
            from archive_inspector import verify_manifest
            with self.assertRaises(ArchiveError):verify_manifest(folder,required=True)

    def test_rejects_unsafe_media_and_encodes_spaces(self):
        for value in ['http://media.sooperbooks.com/a.mp3','https://media.sooperbooks.com.evil/a.mp3','https://x@media.sooperbooks.com/a.mp3','https://media.sooperbooks.com/a.mp3?token=x','https://media.sooperbooks.com/a/../b.mp3']:
            with self.assertRaises(ArchiveError):media_url(value)
        self.assertIn('stories%20/a.mp3',media_url('https://media.sooperbooks.com/stories /a.mp3'))

    def test_failed_download_removes_partial_file(self):
        from sooperbooks import download
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'partial.mp3'
            path.write_bytes(b'partial')
            with patch('sooperbooks.requests.get',side_effect=TimeoutError()):
                with self.assertRaises(ArchiveError):download('https://media.sooperbooks.com/a.mp3',path)
            self.assertFalse(path.exists())

if __name__=='__main__':unittest.main()
