"""Offline synthetic fixtures only: no real Drive reads, Gofile uploads or browsers."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import py7zr
from PIL import Image
from mutagen.id3 import ID3, TXXX
import archive_inspector
from drive_storage import Drive
from gofile_presence import classify
from runner import ArchiveError
import storage_runner


class InspectionTests(unittest.TestCase):
    def fixture(self,work, *,card='abc12',expected=1,live=False):
        folder=work/'legacy'/'Example';folder.mkdir(parents=True)
        audio=folder/'audio_files';audio.mkdir()
        sample=audio/'1 - Track.mp3'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2','-c:a','libmp3lame',str(sample)],check=True)
        tags=ID3(sample);tags.add(TXXX(encoding=3,desc='private',text='https://private.example.test/?token=secret-token'));tags.save(sample)
        (folder/'card.txt').write_text(f'Title:: Example\nAuthor:: Published Author\nCardID:: {card}\nNotes:: private@example.test\n')
        (folder/'tracks.txt').write_text('\n\n'.join(f'TrackNumber:: {i}\nTitle:: Track {i}\n' for i in range(1,expected+1)))
        raw={'cardId':card,'title':'Example','userId':'private-owner','notes':'personal private notes',
             'metadata':{'author':'Published Author','notes':'do not share'},
             'content':{'chapters':[{'tracks':[{'title':f'Track {i}','type':'radio' if live else 'audio','trackUrl':'https://private.example.test/?token=secret-token'} for i in range(expected)]}]}}
        text=json.dumps({'card':raw},indent=4)
        (folder/'playlist.json').write_text(text)
        (folder/'personal-notes.txt').write_text('Do not distribute my personal notes')
        images=folder/'images';images.mkdir();image=Image.new('RGB',(16,16),'blue');exif=Image.Exif();exif[270]='private@example.test'
        image.save(images/'cover.png',exif=exif)
        archive=work/'Stories - Example[abc12] (2026-01-01).7z'
        with py7zr.SevenZipFile(archive,'w') as handle:handle.writeall(folder,'Example')
        return archive,text

    def test_sanitized_copy_preserves_source_and_verifies_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp);original,raw=self.fixture(work);before=original.read_bytes()
            output=work/'output';output.mkdir()
            result=archive_inspector.sanitize_archive(original,output)
            self.assertEqual(original.read_bytes(),before)
            self.assertEqual(Path(result['rawMetadata']).read_text(),raw)
            self.assertEqual(result['cardId'],'abc12');self.assertEqual(result['tracks'],1)
            restored=work/'restored';restored.mkdir();archive_inspector.safe_extract(result['path'],restored)
            root=restored/'Example';archive_inspector.verify_manifest(root,required=True)
            self.assertFalse((root/'personal-notes.txt').exists())
            for path in root.rglob('*'):
                if path.is_file():
                    contents=path.read_bytes()
                    for value in [b'secret-token',b'private@example.test',b'personal private notes',b'private-owner',b'do not share']:
                        self.assertNotIn(value,contents)
            tags=ID3(next((root/'audio_files').glob('*.mp3')))
            self.assertEqual(str(tags['TPE1']),'Published Author')

    def test_incomplete_and_conflicting_identity_stay_unavailable(self):
        for options,code in [({'expected':2},'incomplete_archive'),({'card':'other'},'identity_conflict')]:
            with tempfile.TemporaryDirectory() as tmp:
                work=Path(tmp);original,_=self.fixture(work,**options);output=work/'output';output.mkdir()
                with self.assertRaises(ArchiveError) as error:archive_inspector.sanitize_archive(original,output)
                self.assertEqual(error.exception.code,code)

    def test_bracketed_title_does_not_override_the_trailing_card_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp);original,_=self.fixture(work)
            renamed=original.with_name('Stories - [Piano] Example[abc12] (2026-01-01).7z');original.rename(renamed)
            output=work/'output';output.mkdir();result=archive_inspector.sanitize_archive(renamed,output)
            self.assertEqual(result['cardId'],'abc12')

    def test_captured_radio_tracks_are_not_treated_as_complete_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp);original,_=self.fixture(work,live=True);output=work/'output';output.mkdir()
            with self.assertRaises(ArchiveError) as error:archive_inspector.sanitize_archive(original,output)
            self.assertEqual(error.exception.code,'incomplete_archive')

    def test_symlink_and_traversal_entries_are_rejected_before_extraction(self):
        for name,symlink in [('../outside',False),('/absolute',False),('safe',True),('C:/escape',False),('safe\\escape',False)]:
            member=Mock(filename=name,is_symlink=symlink,is_junction=False,is_socket=False,uncompressed=1)
            archive=Mock();archive.files=[member];archive.needs_password.return_value=False
            archive.__enter__=Mock(return_value=archive);archive.__exit__=Mock(return_value=False)
            with tempfile.TemporaryDirectory() as tmp,patch.object(py7zr,'SevenZipFile',return_value=archive):
                with self.assertRaises(ArchiveError):archive_inspector.safe_extract('synthetic',Path(tmp)/'out')
                archive.extractall.assert_not_called()

    def test_archive_bomb_and_encryption_are_rejected(self):
        for encrypted,size in [(True,1),(False,archive_inspector.MAX_FILE+1)]:
            member=Mock(filename='safe',is_symlink=False,is_junction=False,is_socket=False,uncompressed=size)
            archive=Mock();archive.files=[member];archive.needs_password.return_value=encrypted
            archive.__enter__=Mock(return_value=archive);archive.__exit__=Mock(return_value=False)
            with tempfile.TemporaryDirectory() as tmp,patch.object(py7zr,'SevenZipFile',return_value=archive):
                with self.assertRaises(ArchiveError):archive_inspector.safe_extract('synthetic',Path(tmp)/'out')


class DeliveryTests(unittest.TestCase):
    def source(self):
        payload=b'archive bytes'
        return {'checkpoint':{},'runtimeToken':'private-runtime-token','gofileToken':'private-gofile-token',
                'destinationFolder':'private-folder','hot':{'pageUrl':'https://gofile.io/d/example'},
                'object':{'id':'object-id','filename':'Example.7z','drive_file_id':'drive-file','drive_version':'1','bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}}

    def test_present_and_unknown_pages_never_download_or_upload(self):
        for presence in ['present','unknown']:
            with tempfile.TemporaryDirectory() as tmp,patch.object(storage_runner,'check_page',return_value=presence),patch.object(storage_runner,'Drive') as drive,patch.object(storage_runner,'upload') as upload:
                client=Mock()
                if presence=='unknown':
                    with self.assertRaises(ArchiveError):storage_runner.deliver(client,self.source(),Path(tmp))
                else:storage_runner.deliver(client,self.source(),Path(tmp))
                drive.assert_not_called();upload.assert_not_called()

    def test_missing_page_restores_exact_bytes_with_runtime_credentials_only(self):
        payload=b'archive bytes';drive=Mock()
        def download(file,path,**kwargs):Path(path).write_bytes(payload);return {'parents':['private-folder']}
        drive.download.side_effect=download
        with tempfile.TemporaryDirectory() as tmp,patch.object(storage_runner,'check_page',return_value='missing'),patch.object(storage_runner,'Drive',return_value=drive) as constructor,patch.object(storage_runner,'upload') as upload:
            source=self.source();client=Mock();storage_runner.deliver(client,source,Path(tmp))
            self.assertEqual(constructor.call_args.args,('private-runtime-token',))
            self.assertTrue(callable(constructor.call_args.kwargs['refresh']))
            self.assertEqual(upload.call_args.args[1],'private-gofile-token')
            self.assertNotIn('import',str(client.call.call_args_list))

    def test_checksum_mismatch_never_reaches_gofile(self):
        drive=Mock()
        def download(file,path,**kwargs):Path(path).write_bytes(b'wrong');return {'parents':['private-folder']}
        drive.download.side_effect=download
        with tempfile.TemporaryDirectory() as tmp,patch.object(storage_runner,'check_page',return_value='missing'),patch.object(storage_runner,'Drive',return_value=drive),patch.object(storage_runner,'upload') as upload:
            with self.assertRaises(ArchiveError):storage_runner.deliver(Mock(),self.source(),Path(tmp))
            upload.assert_not_called()

    def test_uncertain_upload_is_not_blindly_repeated(self):
        source=self.source();source['checkpoint']={'uploadStarted':True}
        with tempfile.TemporaryDirectory() as tmp,patch.object(storage_runner,'check_page') as check,patch.object(storage_runner,'Drive') as drive,patch.object(storage_runner,'upload') as upload:
            with self.assertRaises(ArchiveError) as error:storage_runner.deliver(Mock(),source,Path(tmp))
            self.assertEqual(error.exception.code,'upload_uncertain');check.assert_not_called();drive.assert_not_called();upload.assert_not_called()

    def test_page_classifier_does_not_confuse_loading_or_challenges_with_absence(self):
        self.assertEqual(classify('Example.7z','Example.7z',True),'present')
        self.assertEqual(classify('This content does not exist','Example.7z'),'missing')
        for text in ['Loading…','Verify you are human','Access denied','Rate limit exceeded','Password required','Temporarily unavailable','Folder contents are loading']:
            self.assertEqual(classify(text,'Example.7z'),'unknown')

    def test_resumed_completed_drive_upload_does_not_post_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'Example.7z';path.write_bytes(b'bytes')
            drive=Drive('private-token');drive.metadata=Mock(return_value={'id':'file-id','parents':['folder-id'],'appProperties':{'toyoJob':'job-id','toyoSha256':'a'*64}})
            drive.readback=Mock(return_value={'verified':True})
            with patch('drive_storage.requests.post') as post:
                self.assertEqual(drive.upload(path,'folder-id','job-id','abc12','full',{}, {'uploadedFile':'file-id'}),{'verified':True})
                post.assert_not_called()

    def test_resumable_session_never_forwards_tokens_to_other_hosts(self):
        for url in ['http://www.googleapis.com/upload/drive/v3/files','https://evil.test/upload/drive/v3/files','https://www.googleapis.com/other','https://user:pass@www.googleapis.com/upload/drive/v3/files']:
            with self.assertRaises(ArchiveError):Drive.session_url(url)

    def test_expired_access_token_is_refreshed_server_side_once(self):
        first=Mock(status_code=401);second=Mock(status_code=200)
        refresh=Mock(return_value='new-private-token');drive=Drive('old-private-token',refresh=refresh)
        with patch('drive_storage.requests.get',side_effect=[first,second]) as get:
            self.assertIs(drive.request('get','https://www.googleapis.com/drive/v3/about'),second)
            refresh.assert_called_once();first.close.assert_called_once()
            self.assertEqual(get.call_args.kwargs['headers']['Authorization'],'Bearer new-private-token')


if __name__=='__main__':unittest.main()
