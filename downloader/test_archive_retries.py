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


if __name__=='__main__':unittest.main()
