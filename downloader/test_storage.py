"""Offline synthetic fixtures only: no real Drive reads, Gofile uploads or browsers."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile
import stat
import signal
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
    def test_child_resource_kills_are_limits_and_other_unexplained_exits_stay_unsafe(self):
        for returncode,expected in [(-signal.SIGKILL,'resource_limit'),(-signal.SIGXCPU,'resource_limit'),(1,'unsafe_archive')]:
            with self.subTest(returncode=returncode),tempfile.TemporaryDirectory() as tmp,patch.object(archive_inspector.subprocess,'run',return_value=Mock(returncode=returncode)) as process:
                with self.assertRaises(ArchiveError) as error:archive_inspector.inspect_archive('original.7z',Path(tmp),fast_large=True)
                self.assertEqual(error.exception.code,expected)
                self.assertEqual(process.call_args.args[0][-1],'--fast-large')

    def test_legacy_and_json_preview_labels_are_preserved_without_changing_sources(self):
        cases=[('Discover: 1 track',None,False,'demo'),
               ('Story: A preview',None,False,'demo'),
               ('Discover: 1 track','Discover: The full card',False,'full'),
               ('Original playlist','Discover: A preview',False,'demo'),
               ('Original playlist','Discover: The full card',True,'demo'),
               ('Discover the science',None,False,'full')]
        for legacy_description,json_description,explicit_demo,variant in cases:
            with self.subTest(legacy=legacy_description,json=json_description,explicit=explicit_demo),tempfile.TemporaryDirectory() as tmp:
                work=Path(tmp);self.fixture(work);folder=work/'legacy'/'Example'
                with (folder/'card.txt').open('a') as handle:handle.write('Description:: '+legacy_description+'\n')
                if json_description is None:(folder/'playlist.json').unlink()
                else:
                    raw=json.loads((folder/'playlist.json').read_text());raw['card']['metadata']['description']=json_description
                    (folder/'playlist.json').write_text(json.dumps(raw))
                source=work/('Legacy'+('[DEMO]' if explicit_demo else '')+'[abc12].zip')
                with zipfile.ZipFile(source,'w') as archive:
                    for path in folder.rglob('*'):
                        if path.is_file():archive.write(path,str(path.relative_to(folder.parent)))
                before=source.read_bytes();out=work/'out';out.mkdir();result=archive_inspector.sanitize_archive(source,out)
                self.assertEqual(result['variant'],variant);self.assertEqual('[DEMO]' in Path(result['path']).name,variant=='demo')
                self.assertEqual(source.read_bytes(),before);self.assertEqual(result['sourceSha256'],hashlib.sha256(before).hexdigest())
                self.assertEqual(result['tracks'],1)

    def test_generated_verification_keeps_import_and_safety_limits(self):
        def member(name='Example/audio',size=768*1024**2):
            return Mock(filename=name,is_symlink=False,is_junction=False,is_socket=False,uncompressed=size)
        nine_gib=[member('Example/audio'+str(i)) for i in range(12)]
        cases=[('import',nine_gib,False,64*1024**3,'resource_limit'),
               ('generated',nine_gib,False,64*1024**3,None),
               ('generated',[member('Example/audio'+str(i)) for i in range(20)],False,64*1024**3,None),
               ('generated',[member('Example/audio'+str(i)) for i in range(20)],False,30*1024**3,'resource_limit'),
               ('generated',[member('Example/audio'+str(i)) for i in range(22)],False,64*1024**3,'resource_limit'),
               ('import',[member(size=archive_inspector.MAX_FILE+1)],False,64*1024**3,'resource_limit'),
               ('generated',[member(size=archive_inspector.MAX_FILE+1)],False,64*1024**3,None),
               ('generated',[member(size=archive_inspector.MAX_BUILD_FILE+1)],False,64*1024**3,'resource_limit'),
               ('generated',nine_gib,False,18*1024**3,'resource_limit'),
               ('generated',[member('../escape',1)],False,64*1024**3,'unsafe_archive'),
               ('generated',[member(size=1)],True,64*1024**3,'encrypted_archive'),
               ('generated',[member(size=1)]*(archive_inspector.MAX_MEMBERS+1),False,64*1024**3,'resource_limit')]
        for kind,members,encrypted,free,error in cases:
            with self.subTest(kind=kind,members=len(members),error=error),tempfile.TemporaryDirectory() as tmp:
                archive=Mock();archive.files=members;archive.needs_password.return_value=encrypted
                archive.__enter__=Mock(return_value=archive);archive.__exit__=Mock(return_value=False)
                def extract(destination):
                    folder=Path(destination)/'Example';folder.mkdir(parents=True);(folder/'payload').write_bytes(b'x')
                    (folder/'manifest.json').write_text(json.dumps({'files':{'payload':{'bytes':1,'sha256':hashlib.sha256(b'x').hexdigest()}}}))
                archive.extractall.side_effect=extract
                with patch.object(py7zr,'SevenZipFile',return_value=archive),patch.object(archive_inspector.shutil,'disk_usage',return_value=Mock(free=free)):
                    verify=archive_inspector.safe_extract if kind=='import' else archive_inspector.verify_generated_archive
                    if error:
                        with self.assertRaises(ArchiveError) as caught:verify('synthetic',Path(tmp)/'restored')
                        self.assertEqual(caught.exception.code,error);archive.extractall.assert_not_called()
                    else:
                        verify('synthetic',Path(tmp)/'restored');archive.extractall.assert_called_once()

    def test_generated_verification_still_requires_one_root_and_valid_manifest(self):
        for failure in ['missing_manifest','bad_hash','extra_root']:
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                member=Mock(filename='Example/payload',is_symlink=False,is_junction=False,is_socket=False,uncompressed=1)
                archive=Mock();archive.files=[member];archive.needs_password.return_value=False
                archive.__enter__=Mock(return_value=archive);archive.__exit__=Mock(return_value=False)
                def extract(destination):
                    folder=Path(destination)/'Example';folder.mkdir(parents=True);(folder/'payload').write_bytes(b'x')
                    if failure!='missing_manifest':(folder/'manifest.json').write_text(json.dumps({'files':{'payload':{'bytes':1,'sha256':'wrong'}}}))
                    if failure=='extra_root':(Path(destination)/'unexpected').mkdir()
                archive.extractall.side_effect=extract
                with patch.object(py7zr,'SevenZipFile',return_value=archive),patch.object(archive_inspector.shutil,'disk_usage',return_value=Mock(free=64*1024**3)):
                    with self.assertRaises(ArchiveError) as caught:archive_inspector.verify_generated_archive('synthetic',Path(tmp)/'restored')
                    self.assertEqual(caught.exception.code,'verification_failed')

    def test_ogg_opus_inputs_are_remuxed_without_transcoding(self):
        for suffix in ['.mp3','.opus']:
            with self.subTest(source_suffix=suffix),tempfile.TemporaryDirectory() as tmp:
                work=Path(tmp);self.fixture(work);folder=work/'legacy'/'Example'
                sample=next((folder/'audio_files').glob('*.mp3'));sample.unlink();sample=sample.with_suffix(suffix)
                subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2',
                                '-c:a','libopus','-f','ogg',str(sample)],check=True)
                source=work/'Mislabeled [abc12].zip'
                with zipfile.ZipFile(source,'w') as archive:
                    for path in folder.rglob('*'):
                        if path.is_file():archive.write(path,str(path.relative_to(folder.parent)))
                out=work/'out';out.mkdir();result=archive_inspector.sanitize_archive(source,out)
                audio=list((out/'clean').rglob('*.ogg'));self.assertEqual(len(audio),1)
                codec=subprocess.check_output(['ffprobe','-v','error','-show_entries','stream=codec_name','-of','json',str(audio[0])])
                self.assertEqual(json.loads(codec)['streams'][0]['codec_name'],'opus')
                self.assertEqual(result['tracks'],1)

    def test_drive_zip_with_card_json_preserves_metadata_and_sanitizes_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp);original,raw=self.fixture(work)
            folder=work/'legacy'/'Example'
            (folder/'card.txt').unlink()
            (folder/'playlist.json').rename(folder/'card.json')
            (folder/'.backup-state.json').write_text('{"email":"private@example.test"}')
            source=work/'Example [abc12].zip'
            with zipfile.ZipFile(source,'w') as archive:
                for path in folder.rglob('*'):
                    if path.is_file():archive.write(path,str(path.relative_to(folder.parent)))
            before=source.read_bytes();out=work/'out';out.mkdir()
            result=archive_inspector.sanitize_archive(source,out)
            self.assertEqual(result['cardId'],'abc12');self.assertEqual(result['tracks'],1)
            self.assertEqual(Path(result['rawMetadata']).read_text(),raw)
            self.assertEqual(source.read_bytes(),before)
            self.assertTrue(Path(result['path']).name.endswith('[abc12].7z'))
            clean=next((out/'clean').iterdir())
            self.assertFalse((clean/'.backup-state.json').exists())
            self.assertNotIn('private-owner',(clean/'playlist.json').read_text())

    def test_zip_rejects_traversal_symlinks_duplicates_and_oversized_members(self):
        for names,mode,limit in [(['../outside'],0,None),(['Example/file'],stat.S_IFLNK|0o777,None),
                                  (['Example/file','Example/file'],0,None),(['Example/file'],0,0)]:
            with self.subTest(names=names,mode=mode,limit=limit),tempfile.TemporaryDirectory() as tmp:
                work=Path(tmp);source=work/'bad.zip'
                with zipfile.ZipFile(source,'w') as archive:
                    for name in names:
                        info=zipfile.ZipInfo(name);info.external_attr=mode<<16;archive.writestr(info,b'x')
                with patch.object(archive_inspector,'MAX_FILE',limit if limit is not None else archive_inspector.MAX_FILE):
                    with self.assertRaises(ArchiveError):archive_inspector.safe_extract(source,work/'out')

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
            self.assertEqual(next((root/'images').iterdir()).read_bytes(),next((work/'legacy'/'Example'/'images').iterdir()).read_bytes())
            for path in root.rglob('*'):
                if path.is_file() and (root/'images') not in path.parents:
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

    def test_same_workspace_keeps_canonical_name_and_original_bytes(self):
        for suffix in ['', ' (1)', ' (2) (1)']:
            with self.subTest(suffix=suffix),tempfile.TemporaryDirectory() as tmp:
                work=Path(tmp);original,_=self.fixture(work)
                canonical=original.name
                if suffix:
                    renamed=original.with_name(original.stem+suffix+original.suffix)
                    original.rename(renamed);original=renamed
                before=original.read_bytes()
                result=archive_inspector.sanitize_archive(original,work)
                self.assertEqual(Path(result['path']).name,canonical)
                self.assertNotEqual(Path(result['path']),original)
                self.assertEqual(original.read_bytes(),before)
                restored=work/'restored';restored.mkdir()
                archive_inspector.safe_extract(result['path'],restored)
                archive_inspector.verify_manifest(restored/'Example',required=True)

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
    def test_revision_drift_requires_provider_and_downloaded_sha256(self):
        payload=b'preserved archive';sha=hashlib.sha256(payload).hexdigest()
        metadata={'version':'2','size':str(len(payload)),'sha256Checksum':sha,'md5Checksum':hashlib.md5(payload).hexdigest(),'capabilities':{'canDownload':True}}
        for provider_sha,body,expected,error in [(sha,payload,sha,None),(None,payload,sha,'source_changed'),('c'*64,payload,sha,'source_changed'),(sha,payload,None,'source_changed'),(sha,b'X'*len(payload),sha,'verification_failed')]:
            with self.subTest(provider_sha=provider_sha,body=body,expected=expected),tempfile.TemporaryDirectory() as tmp:
                drive=Drive('private-token');drive.metadata=Mock(return_value={**metadata,'sha256Checksum':provider_sha,'md5Checksum':hashlib.md5(body).hexdigest()})
                response=Mock(status_code=200,is_redirect=False,headers={});response.iter_content.return_value=[body]
                response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
                with patch.object(drive,'request',return_value=response) as request,patch('drive_storage.public_url',side_effect=lambda url:url):
                    path=Path(tmp)/'archive.7z'
                    if error:
                        with self.assertRaises(ArchiveError) as caught:drive.download('file',path,expected_version='1',expected_sha256=expected)
                        self.assertEqual(caught.exception.code,error)
                        if error=='source_changed':request.assert_not_called()
                        self.assertFalse(path.exists())
                    else:
                        self.assertEqual(drive.download('file',path,expected_version='1',expected_sha256=expected)['version'],'2')
                        self.assertEqual(path.read_bytes(),payload)

    def test_upload_readback_verifies_known_sha_across_metadata_revision(self):
        payload=b'preserved archive';sha=hashlib.sha256(payload).hexdigest()
        metadata={'version':'2','size':str(len(payload)),'sha256Checksum':sha,'capabilities':{'canDownload':True}}
        for body,error in [(payload,None),(b'X'*len(payload),'verification_failed')]:
            with self.subTest(body=body),tempfile.TemporaryDirectory() as tmp:
                drive=Drive('private-token');drive.metadata=Mock(return_value=metadata)
                response=Mock(status_code=200,is_redirect=False,headers={});response.iter_content.return_value=[body]
                response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
                with patch.object(drive,'request',return_value=response),patch('drive_storage.public_url',side_effect=lambda url:url):
                    if error:
                        with self.assertRaises(ArchiveError) as caught:drive.readback({'id':'file','version':'1','name':'archive.7z'},Path(tmp),'abc12','full',{},sha)
                        self.assertEqual(caught.exception.code,error)
                    else:
                        result=drive.readback({'id':'file','version':'1','name':'archive.7z'},Path(tmp),'abc12','full',{},sha)
                        self.assertEqual(result['verifiedSha256'],sha);self.assertEqual(result['driveVersion'],'2')
                    self.assertFalse((Path(tmp)/'drive-readback.7z').exists())

    def test_preserved_copy_checks_never_request_drive_or_upload(self):
        for presence in ['present','missing','unknown']:
            with self.subTest(presence=presence),tempfile.TemporaryDirectory() as tmp,patch.object(storage_runner,'check_page',return_value=presence),patch.object(storage_runner,'Drive') as drive,patch.object(storage_runner,'upload') as upload:
                client=Mock();source={'checkOnly':True,'checkpoint':{},'object':{'filename':'Archive.7z'},'hot':{'pageUrl':'https://gofile.io/d/example'}}
                if presence=='unknown':
                    with self.assertRaises(ArchiveError):storage_runner.deliver(client,source,Path(tmp))
                    self.assertFalse(any(call.args[0]=='complete' for call in client.call.call_args_list))
                else:
                    storage_runner.deliver(client,source,Path(tmp));self.assertEqual(client.call.call_args.args,('complete',{'presence':presence}))
                drive.assert_not_called();upload.assert_not_called()

    def test_recovered_provider_metadata_checks_the_existing_page_without_uploading(self):
        source=self.source();payload=b'archive bytes';md5=hashlib.md5(payload).hexdigest()
        receipt={'filename':'Example.7z','bytes':len(payload),'sha256':source['object']['sha256'],'contentId':'file-id',
                 'pageUrl':'https://gofile.io/d/example','parentFolder':'folder-id','md5':md5,'providerMd5':md5,
                 'providerBytes':len(payload),'uploadedAt':1800000000000,'verification':'provider-metadata',
                 'observedAt':1800000000000,'uploadedAtPrecision':'minute','evidenceSource':'gofile-owner-properties'}
        source['checkpoint']={'hotReceipt':receipt}
        for presence in ['present','missing','unknown']:
            with tempfile.TemporaryDirectory() as tmp,patch.object(storage_runner,'check_page',return_value=presence) as check,patch.object(storage_runner,'Drive') as drive,patch.object(storage_runner,'upload') as upload:
                client=Mock()
                if presence=='present':storage_runner.deliver(client,source,Path(tmp));self.assertEqual(client.call.call_args.args,('complete',{'presence':'present'}))
                else:
                    with self.assertRaises(ArchiveError):storage_runner.deliver(client,source,Path(tmp))
                    self.assertNotIn('complete',[c.args[0] for c in client.call.call_args_list])
                check.assert_called_once_with(receipt['pageUrl'],receipt['filename']);drive.assert_not_called();upload.assert_not_called()

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
            self.assertEqual(drive.download.call_args.kwargs['expected_sha256'],source['object']['sha256'])
            self.assertEqual(upload.call_args.args[1],'private-gofile-token')
            self.assertNotIn('import',str(client.call.call_args_list))

    def test_checksum_mismatch_never_reaches_gofile(self):
        drive=Mock()
        def download(file,path,**kwargs):Path(path).write_bytes(b'wrong');return {'parents':['private-folder']}
        drive.download.side_effect=download
        with tempfile.TemporaryDirectory() as tmp,patch.object(storage_runner,'check_page',return_value='missing'),patch.object(storage_runner,'Drive',return_value=drive),patch.object(storage_runner,'upload') as upload:
            with self.assertRaises(ArchiveError):storage_runner.deliver(Mock(),self.source(),Path(tmp))
            upload.assert_not_called()

    def test_nested_cold_restore_uses_exact_registered_parent_and_preserves_bytes(self):
        payload=b'archive bytes';drive=Mock();source=self.source()
        source['destinationFolder']='registered_card_folder';source['hot']=None
        def download(file,path,**kwargs):
            self.assertEqual(file,source['object']['drive_file_id'])
            Path(path).write_bytes(payload);return {'parents':['registered_card_folder']}
        drive.download.side_effect=download
        with tempfile.TemporaryDirectory() as tmp,patch.object(storage_runner,'Drive',return_value=drive),patch.object(storage_runner,'upload') as upload:
            client=Mock();storage_runner.deliver(client,source,Path(tmp))
            self.assertEqual(Path(upload.call_args.args[0]).read_bytes(),payload)
            self.assertIn('upload-intent',[c.args[0] for c in client.call.call_args_list])

    def test_nested_restore_rejects_wrong_parent_before_gofile_upload(self):
        source=self.source();source['destinationFolder']='registered_card_folder';source['hot']=None
        drive=Mock()
        def download(file,path,**kwargs):Path(path).write_bytes(b'archive bytes');return {'parents':['unrelated_folder']}
        drive.download.side_effect=download
        with tempfile.TemporaryDirectory() as tmp,patch.object(storage_runner,'Drive',return_value=drive),patch.object(storage_runner,'upload') as upload:
            with self.assertRaises(ArchiveError):storage_runner.deliver(Mock(),source,Path(tmp))
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



class InspectionReuseTests(unittest.TestCase):
    def receipt(self):
        return {'driveFileId':'staged_private_file','cardId':'abc12','variant':'full','filename':'Example[abc12].7z','bytes':123,'sha256':'a'*64,'md5':'b'*32,'index':{'title':'Example','tracks':1}}

    def test_import_reads_back_the_inspected_file_without_source_download_repack_or_upload(self):
        receipt=self.receipt();drive=Mock();drive.metadata.return_value={'parents':['private-folder']};drive.readback.return_value=receipt
        client=Mock();source={'kind':'import','writeToken':'private-token','checkpoint':{},'destinationFolder':'private-folder','reusableReceipt':receipt}
        with tempfile.TemporaryDirectory() as tmp,patch.object(storage_runner,'Drive',return_value=drive),patch.object(storage_runner,'inspect_archive') as inspect:
            storage_runner.seed(client,source,Path(tmp));inspect.assert_not_called();drive.download.assert_not_called();drive.upload.assert_not_called()
            drive.readback.assert_called_once();client.call.assert_any_call('cold-receipt',receipt);client.call.assert_any_call('complete',{})

    def test_missing_moved_or_changed_staged_archive_never_falls_back_to_repacking(self):
        for changed in ['missing','parent','checksum']:
            receipt=self.receipt();drive=Mock();drive.metadata.return_value={'parents':['other-folder'] if changed=='parent' else ['private-folder']};drive.readback.return_value={**receipt,'sha256':'c'*64}
            source={'kind':'import','writeToken':'private-token','checkpoint':{},'destinationFolder':'private-folder','reusableReceipt':None if changed=='missing' else receipt}
            with tempfile.TemporaryDirectory() as tmp,patch.object(storage_runner,'Drive',return_value=drive),patch.object(storage_runner,'inspect_archive') as inspect:
                with self.assertRaises(ArchiveError):storage_runner.seed(Mock(),source,Path(tmp))
                inspect.assert_not_called();drive.upload.assert_not_called()

    def test_inspection_uploads_once_and_publishes_the_receipt_for_later_import(self):
        import os
        receipt=self.receipt();read=Mock();read.metadata.return_value={'parents':['source-folder']};write=Mock();write.upload.return_value=receipt;client=Mock()
        source={'kind':'inspect','readToken':'reader','writeToken':'writer','destinationFolder':'private-folder','checkpoint':{},'candidate':{'source_file':'source-private-file','source_parent':'source-folder','source_version':'1','filename':'Example.7z'}}
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'TOYO_ARCHIVE_JOB':'job'}),patch.object(storage_runner,'Drive',side_effect=[write,read]),patch.object(storage_runner,'inspect_archive',return_value={'path':'sanitized.7z','cardId':'abc12','variant':'full','sha256':'a'*64,'bytes':123,'title':'Example','tracks':1,'sourceSha256':'c'*64,'index':receipt['index']}) as inspect:
            storage_runner.seed(client,source,Path(tmp));inspect.assert_called_once();write.upload.assert_called_once()
            self.assertTrue(inspect.call_args.kwargs['fast_large'])
            published=next(call.args[1] for call in client.call.call_args_list if call.args[0]=='inspection')
            self.assertEqual(published['receipt'],receipt);self.assertEqual(published['formatVersion'],1)

    def test_retained_inspection_uploads_keep_the_original_packing_strategy(self):
        import os
        for field in ['uploadSha256','uploadSession','uploadedFile']:
            with self.subTest(field=field),tempfile.TemporaryDirectory() as tmp:
                receipt=self.receipt();read=Mock();read.metadata.return_value={'parents':['source-folder']};write=Mock();write.upload.return_value=receipt
                source={'kind':'inspect','readToken':'reader','writeToken':'writer','destinationFolder':'private-folder','checkpoint':{field:'retained'},'candidate':{'source_file':'source-file','source_parent':'source-folder','source_version':'1','filename':'Example.7z'}}
                result={'path':'sanitized.7z','cardId':'abc12','variant':'full','sha256':'a'*64,'bytes':123,'title':'Example','tracks':1,'sourceSha256':'c'*64,'index':receipt['index']}
                with patch.dict(os.environ,{'TOYO_ARCHIVE_JOB':'job'}),patch.object(storage_runner,'Drive',side_effect=[write,read]),patch.object(storage_runner,'inspect_archive',return_value=result) as inspect:
                    storage_runner.seed(Mock(),source,Path(tmp));self.assertFalse(inspect.call_args.kwargs['fast_large'])

    def test_acknowledged_inspection_and_completed_import_resume_without_repacking(self):
        for checkpoint,kind in [({'inspectionArtifact':{'receipt':self.receipt()}},'inspect'),({'coldReceipt':self.receipt()},'import')]:
            source={'kind':kind,'writeToken':'writer','checkpoint':checkpoint}
            with tempfile.TemporaryDirectory() as tmp,patch.object(storage_runner,'Drive') as drive,patch.object(storage_runner,'inspect_archive') as inspect:
                client=Mock();storage_runner.seed(client,source,Path(tmp));inspect.assert_not_called();drive.return_value.upload.assert_not_called();client.call.assert_called_once_with('complete',{})


class LazyBrowserTests(unittest.TestCase):
    def test_installs_browser_only_when_an_existing_page_needs_it(self):
        from gofile_presence import check_page
        context=Mock();page=context.new_page.return_value
        page.goto.return_value=Mock(status=200);page.url='https://gofile.io/d/example'
        page.locator.return_value.inner_text.return_value='Example.7z'
        page.get_by_role.return_value.count.return_value=1
        playwright=Mock();playwright.chromium.executable_path='/synthetic/chromium'
        playwright.chromium.launch.return_value.new_context.return_value=context
        manager=Mock();manager.__enter__=Mock(return_value=playwright);manager.__exit__=Mock(return_value=False)
        import sys
        from types import ModuleType
        module=ModuleType('playwright.sync_api');module.sync_playwright=Mock(return_value=manager)
        for installed in [False,True]:
            with patch.dict(sys.modules,{'playwright.sync_api':module}), \
                    patch('gofile_presence.Path.is_file',return_value=installed), \
                    patch('gofile_presence.subprocess.run') as install:
                self.assertEqual(check_page('https://gofile.io/d/example','Example.7z'),'present')
                self.assertEqual(install.call_count,int(not installed))
                if not installed:self.assertIn('chromium',install.call_args.args[0])


class RunnerBatchTests(unittest.TestCase):
    def test_reuses_setup_with_isolated_children_and_continues_after_failure(self):
        import os
        import uuid
        jobs=[str(uuid.uuid4()) for _ in range(3)]
        batch=str(uuid.uuid4())
        with patch.dict(os.environ,{'TOYO_ARCHIVE_JOBS':json.dumps(jobs),'TOYO_ARCHIVE_BATCH':batch},clear=True), \
                patch.object(storage_runner.subprocess,'run',side_effect=[Mock(returncode=0),Mock(returncode=1),Mock(returncode=0)]) as run:
            self.assertEqual(storage_runner.main(),1)
            self.assertEqual(run.call_count,3)
            self.assertEqual([call.kwargs['env']['TOYO_ARCHIVE_JOB'] for call in run.call_args_list],jobs)
            self.assertTrue(all(call.args[0][-1]=='--single' for call in run.call_args_list))
            self.assertTrue(all(call.kwargs['env']['TOYO_ARCHIVE_BATCH']==batch for call in run.call_args_list))

    def test_rejects_oversize_duplicate_and_invalid_batches_before_starting(self):
        import os
        import uuid
        job=str(uuid.uuid4())
        for value in [[job,job],['not-a-job'],[str(uuid.uuid4()) for _ in range(11)],{},[]]:
            with patch.dict(os.environ,{'TOYO_ARCHIVE_JOBS':json.dumps(value)},clear=True),patch.object(storage_runner.subprocess,'run') as run:
                self.assertEqual(storage_runner.main(),1);run.assert_not_called()

    def test_stops_at_runtime_budget_without_starting_another_file(self):
        import os
        import uuid
        jobs=[str(uuid.uuid4()),str(uuid.uuid4())]
        with patch.dict(os.environ,{'TOYO_ARCHIVE_JOBS':json.dumps(jobs)},clear=True), \
                patch.object(storage_runner.time,'monotonic',side_effect=[0,1,18001]), \
                patch.object(storage_runner.subprocess,'run',return_value=Mock(returncode=0)) as run:
            self.assertEqual(storage_runner.main(),1);self.assertEqual(run.call_count,1)


if __name__=='__main__':unittest.main()
