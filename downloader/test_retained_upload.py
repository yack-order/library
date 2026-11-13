"""Retained runner-owned archives: real extraction, no media fetch or provider writes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock,patch
import py7zr
import runner
from drive_storage import Drive
from runner import ArchiveError

class RetainedUploadTests(unittest.TestCase):
    def fixture(self,base,variant='full',mode='ok'):
        source=base/'source';source.mkdir();root=source/'Saved title';root.mkdir();audio=root/'audio_files';audio.mkdir()
        (audio/'01.mp3').write_bytes(b'synthetic already validated audio')
        card={'cardId':'wrong' if mode=='card_id' else 'abc12','title':'Saved title','content':{'chapters':[{'tracks':[{'title':'Saved track'}]}]}}
        if mode=='track_count':card['content']['chapters'][0]['tracks'].append({'title':'Missing track'})
        (root/'Saved title.json').write_text('invalid' if mode=='json' else json.dumps(card))
        files={str(p.relative_to(root)):{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in root.rglob('*') if p.is_file()}
        if mode=='manifest_hash':next(iter(files.values()))['sha256']='0'*64
        if mode!='missing_manifest':(root/'manifest.json').write_text(json.dumps({'cardId':'wrong' if mode=='manifest_id' else 'abc12','version':1,'files':files}))
        path=base/'saved.7z'
        with py7zr.SevenZipFile(path,'w') as archive:archive.writeall(root,root.name)
        sha=hashlib.sha256(path.read_bytes()).hexdigest();name='MYO - Saved title'+('[DEMO]' if variant=='demo' else '')+'[abc12] (2025-01-02).7z'
        metadata={'id':'saved-file-id','name':name,'parents':['folder-id'],'version':'7','size':str(path.stat().st_size),'capabilities':{'canDownload':True},'appProperties':{'toyoJob':'original-job','toyoSha256':sha}}
        drive=Drive('private-token');drive.metadata=Mock(return_value=metadata)
        def download(_id,target,**kwargs):
            self.assertEqual(_id,'saved-file-id');self.assertEqual(kwargs,{'expected_version':'7'});shutil.copyfile(path,target);return metadata
        drive.download=Mock(side_effect=download)
        return drive,metadata,{'uploadedFile':'saved-file-id','uploadSha256':sha},path

    def test_retained_full_and_demo_archives_use_saved_metadata_without_any_upload(self):
        for variant in ['full','demo']:
            with self.subTest(variant=variant),tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);drive,file,progress,path=self.fixture(base,variant)
                with patch('drive_storage.requests.post') as post,patch('drive_storage.requests.put') as put:
                    result=drive.recover_uploaded(base,'folder-id','original-job','abc12',progress)
                self.assertEqual(result['variant'],variant);self.assertEqual(result['filename'],file['name'])
                self.assertEqual(result['index'],{'title':'Saved title','tracks':1});self.assertEqual(result['sha256'],progress['uploadSha256'])
                self.assertEqual(result['bytes'],path.stat().st_size);self.assertEqual(result['md5'],hashlib.md5(path.read_bytes()).hexdigest())
                post.assert_not_called();put.assert_not_called();self.assertFalse(list(base.glob('retained-upload-*')))

    def test_foreign_files_and_invalid_metadata_are_rejected_before_download(self):
        for mode in ['parent','job','sha','filename','unsafe_name','trashed','download']:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);drive,file,progress,_=self.fixture(base)
                if mode=='parent':file['parents']=['other-folder']
                elif mode=='job':file['appProperties']['toyoJob']='other-job'
                elif mode=='sha':file['appProperties']['toyoSha256']='0'*64
                elif mode=='filename':file['name']='Saved[other] (2025-01-02).7z'
                elif mode=='unsafe_name':file['name']='../Saved[abc12] (2025-01-02).7z'
                elif mode=='trashed':file['trashed']=True
                else:file['capabilities']['canDownload']=False
                with self.assertRaises(ArchiveError):drive.recover_uploaded(base,'folder-id','original-job','abc12',progress)
                drive.download.assert_not_called()

    def test_incomplete_checkpoint_does_not_guess_a_file_or_source(self):
        for progress in [{},{'uploadSession':'https://www.googleapis.com/upload/drive/session'},{'uploadedFile':'saved-file-id','uploadSha256':'invalid'}]:
            drive=Drive('private-token');drive.metadata=Mock()
            with tempfile.TemporaryDirectory() as tmp,self.assertRaises(ArchiveError):drive.recover_uploaded(Path(tmp),'folder-id','original-job','abc12',progress)
            drive.metadata.assert_not_called()

    def test_lost_final_acknowledgement_uses_only_one_exact_completed_original_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);drive,file,progress,_=self.fixture(base)
            del progress['uploadedFile'];progress['uploadSession']='https://www.googleapis.com/upload/drive/session'
            drive.json=Mock(return_value={'files':[{'id':file['id']}]})
            result=drive.recover_uploaded(base,'folder-id','original-job','abc12',progress)
            self.assertEqual(result['driveFileId'],file['id'])
            self.assertEqual(drive.json.call_args.args,('files',));query=drive.json.call_args.kwargs['q']
            self.assertIn("key='toyoJob' and value='original-job'",query)
            self.assertIn("key='toyoSha256' and value='"+progress['uploadSha256']+"'",query)
            self.assertNotIn(' or ',query);self.assertEqual(drive.json.call_args.kwargs['pageSize'],2)

    def test_missing_or_ambiguous_completed_writes_never_start_an_upload(self):
        for found in [{'files':[]},{'files':[{'id':'one'},{'id':'two'}]},{'files':[{'id':'one'}],'nextPageToken':'more'}]:
            drive=Drive('private-token');drive.json=Mock(return_value=found);drive.metadata=Mock()
            with tempfile.TemporaryDirectory() as tmp,patch('drive_storage.requests.post') as post,patch('drive_storage.requests.put') as put:
                with self.assertRaises(ArchiveError):drive.recover_uploaded(Path(tmp),'folder-id','original-job','abc12',{'uploadSession':'https://www.googleapis.com/upload/drive/session','uploadSha256':'a'*64})
                post.assert_not_called();put.assert_not_called();drive.metadata.assert_not_called()

    def test_invalid_session_or_query_identity_does_not_probe_drive(self):
        for session,job in [('https://other.test/upload/drive/session','original-job'),('https://www.googleapis.com/upload/drive/session',"invalid'job")]:
            drive=Drive('private-token');drive.json=Mock()
            with tempfile.TemporaryDirectory() as tmp,self.assertRaises(ArchiveError):drive.recover_uploaded(Path(tmp),'folder-id',job,'abc12',{'uploadSession':session,'uploadSha256':'a'*64})
            drive.json.assert_not_called()

    def test_independent_bytes_and_size_must_match_the_original_checkpoint(self):
        for mode in ['sha','size']:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);drive,file,progress,path=self.fixture(base)
                if mode=='sha':progress['uploadSha256']='0'*64;file['appProperties']['toyoSha256']='0'*64
                else:file['size']=str(path.stat().st_size+1)
                with self.assertRaises(ArchiveError):drive.recover_uploaded(base,'folder-id','original-job','abc12',progress)
                self.assertFalse(list(base.glob('retained-upload-*')))

    def test_manifest_and_saved_card_identity_and_track_count_are_verified(self):
        for mode in ['card_id','manifest_id','track_count','missing_manifest','manifest_hash','json']:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);drive,_,progress,_=self.fixture(base,mode=mode)
                with self.assertRaises(ArchiveError):drive.recover_uploaded(base,'folder-id','original-job','abc12',progress)
                self.assertFalse(list(base.glob('retained-upload-*')))

    def test_completed_upload_resumes_before_source_fetch_without_rebuild_or_upload(self):
        for only in [False,True]:
            with self.subTest(only=only):
                client=Mock();client.headers={};client.lost.is_set.return_value=False
                client.call.side_effect=[{'leaseToken':'lease','coldProgress':{'uploadedFile':'saved-file-id'},'retainedUploadOnly':only},{'token':'private-token','cardId':'abc12','driveFolder':'folder-id'},{'ok':True},{'ok':True}]
                drive=Mock();drive.recover_uploaded.return_value={'cardId':'abc12','variant':'demo'}
                with patch.object(runner,'Client',return_value=client),patch.object(runner.threading,'Thread'),patch('drive_storage.Drive',return_value=drive),patch.dict(os.environ,{'TOYO_ARCHIVE_JOB':'original-job'}),patch.object(runner,'build_drive_archive') as build,patch.object(runner,'stream_file') as media,patch.object(runner,'upload') as upload:
                    self.assertEqual(runner.main(),0)
                self.assertEqual([c.args[0] for c in client.call.call_args_list],['claim','drive-token','cold-checkpoint','cold-complete'])
                build.assert_not_called();media.assert_not_called();upload.assert_not_called()

    def test_verification_only_mode_never_falls_back_to_source_or_upload(self):
        for mode in ['protocol','verification']:
            with self.subTest(mode=mode):
                client=Mock();client.headers={}
                token={'token':'private-token'}
                if mode=='verification':token.update(cardId='abc12',driveFolder='folder-id')
                client.call.side_effect=[{'leaseToken':'lease','coldProgress':{'uploadedFile':'saved-file-id'},'retainedUploadOnly':True},token,{'ok':True}]
                drive=Mock();drive.recover_uploaded.side_effect=ArchiveError('verification_failed')
                with patch.object(runner,'Client',return_value=client),patch.object(runner.threading,'Thread'),patch('drive_storage.Drive',return_value=drive),patch.dict(os.environ,{'TOYO_ARCHIVE_JOB':'original-job'}),patch.object(runner,'build_drive_archive') as build,patch.object(runner,'stream_file') as media,patch.object(runner,'upload') as upload:
                    self.assertEqual(runner.main(),1)
                self.assertEqual([c.args[0] for c in client.call.call_args_list],['claim','drive-token','fail'])
                build.assert_not_called();media.assert_not_called();upload.assert_not_called()
