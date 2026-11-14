"""Offline retry packaging and provider diagnostic regressions."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import py7zr
from archive_safety import create_verified_archive
from drive_storage import Drive


class ArchiveRetryTests(unittest.TestCase):
    def test_owned_archives_have_identical_bytes_across_member_order_and_local_timestamps(self):
        hashes=[]
        with tempfile.TemporaryDirectory() as temporary:
            for number,names in enumerate([['one','two'],['two','one']]):
                parent=Path(temporary)/str(number);source=parent/'Example';source.mkdir(parents=True)
                for name in names:
                    member=source/name;member.write_bytes((name*20).encode());os.utime(member,(100000+number*123,100000+number*123))
                nested=source/'nested';nested.mkdir();(nested/'three').write_text('same nested content')
                archive=Path(create_verified_archive(source,parent/'output.7z',reproducible=True))
                hashes.append(hashlib.sha256(archive.read_bytes()).hexdigest())
                restored=parent/'restored'
                with py7zr.SevenZipFile(archive) as opened:opened.extractall(restored)
                self.assertEqual((restored/'Example/nested/three').read_text(),'same nested content')
            self.assertEqual(hashes[0],hashes[1])

    def test_default_desktop_packaging_preserves_source_timestamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'Example';source.mkdir();member=source/'file';member.write_bytes(b'data');os.utime(member,(123456789,123456789))
            create_verified_archive(source,Path(temporary)/'archive.7z')
            self.assertEqual(member.stat().st_mtime,123456789)

    def test_provider_failure_logs_only_fixed_reason_and_operation(self):
        response=Mock(status_code=403)
        response.iter_content.return_value=[json.dumps({'error':{'message':'private https://secret.test/token','errors':[{'reason':'storageQuotaExceeded','secret':'private-token'}]}}).encode()]
        output=io.StringIO()
        with patch('drive_storage.requests.get',return_value=response),contextlib.redirect_stdout(output):
            self.assertIs(Drive('private-token').request('get','https://www.googleapis.com/drive/v3/files?private-query=value'),response)
        self.assertEqual(json.loads(output.getvalue()),{'event':'archive.drive_failure','operation':'lookup','http_status':403,'reason':'storageQuotaExceeded'})

    def test_transport_failure_preserves_original_exception_without_private_text(self):
        error=requests.exceptions.ReadTimeout('private https://secret.test/signed?token=value')
        output=io.StringIO()
        with patch('drive_storage.requests.put',side_effect=error),contextlib.redirect_stdout(output):
            with self.assertRaises(requests.exceptions.ReadTimeout) as raised:
                Drive('private-token').request('put','https://www.googleapis.com/upload/drive/private-session',headers={'Content-Length':'0'})
        self.assertIs(raised.exception,error)
        self.assertEqual(json.loads(output.getvalue()),{'event':'archive.drive_failure','operation':'upload_status','kind':'timeout'})

    def test_oversized_provider_error_body_is_not_logged_or_consumed_unbounded(self):
        response=Mock(status_code=503);response.iter_content.return_value=iter([b'private'*4096,b'more-private-data'])
        output=io.StringIO()
        with patch('drive_storage.requests.get',return_value=response),contextlib.redirect_stdout(output):
            Drive('private-token').request('get','https://www.googleapis.com/drive/v3/files/private-file')
        self.assertEqual(json.loads(output.getvalue()),{'event':'archive.drive_failure','operation':'metadata','http_status':503,'reason':'other'})


    def test_unexpected_failures_never_log_exception_text_or_uncontrolled_stages(self):
        from runner import report_unexpected_failure
        for error,kind in [(PermissionError(13,'private /secret/path'),'filesystem'),(ValueError('https://private.test/signed?token=secret'),'metadata'),(requests.exceptions.SSLError('private TLS endpoint'),'tls')]:
            output=io.StringIO()
            with contextlib.redirect_stdout(output):report_unexpected_failure(error,'private-stage-secret')
            value=json.loads(output.getvalue());self.assertEqual(value['kind'],kind);self.assertEqual(value['stage'],'other')
            self.assertNotIn('private',output.getvalue());self.assertNotIn('secret',output.getvalue())

    def test_both_single_job_entrypoints_emit_safe_unexpected_failure_categories(self):
        import runner,storage_runner
        for module,client_name,entry in [(runner,'Client',runner.main),(storage_runner,'StorageClient',storage_runner.run_one)]:
            client=Mock();client.headers={};client.stage='fetching';client.call.side_effect=ValueError('private endpoint body')
            output=io.StringIO()
            with patch.object(module,client_name,return_value=client),contextlib.redirect_stdout(output):
                self.assertEqual(entry(),1)
            records=[json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
            self.assertEqual(records,[{'event':'archive.unexpected_failure','stage':'fetching','kind':'metadata'}]);self.assertNotIn('private',output.getvalue())


    def test_inspection_retry_matches_the_existing_zero_timestamp_archive_format(self):
        import archive_inspector,subprocess
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);source=base/'Original';source.mkdir();audio=source/'audio_files';audio.mkdir()
            (source/'card.txt').write_text('CardID:: abc12\nTitle:: Example\nAuthor:: Test\nCategory:: Stories\n')
            (source/'tracks.txt').write_text('TrackNumber:: 1\nTitle:: Tone\nDuration:: 0.1\n')
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.1','-y',str(audio/'01.mp3')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            original=Path(create_verified_archive(source,base/'Example[abc12] (2024-01-01).7z'))
            before=hashlib.sha256(original.read_bytes()).hexdigest()
            def legacy_package(folder,target,**ignored):
                folder=Path(folder)
                for item in [*folder.rglob('*'),folder]:os.utime(item,(0,0),follow_symlinks=False)
                return create_verified_archive(folder,target)
            first=base/'legacy';first.mkdir()
            with patch.object(archive_inspector,'create_verified_archive',side_effect=legacy_package):legacy=archive_inspector.sanitize_archive(original,first)
            second=base/'retry';second.mkdir();current=archive_inspector.sanitize_archive(original,second)
            self.assertEqual(legacy['sha256'],current['sha256']);self.assertEqual(legacy['bytes'],current['bytes'])
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),before)


if __name__=='__main__':unittest.main()
