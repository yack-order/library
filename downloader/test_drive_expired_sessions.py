"""Expired resumable sessions: offline provider fixtures, no real uploads."""
import contextlib
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
import requests
from unittest.mock import Mock
from drive_storage import Drive
from runner import ArchiveError


def response(status,body=None,headers=None):
    value=Mock(status_code=status,headers=headers or {})
    value.json.return_value=body or {}
    value.__enter__=Mock(return_value=value);value.__exit__=Mock(return_value=False)
    return value


class ExpiredSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.path=Path(self.temporary.name)/'Example[abc12] (2024-01-01).7z';self.path.write_bytes(b'archive bytes')
        self.sha=hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.saved={'uploadSession':'https://www.googleapis.com/upload/drive/expired-private-session','uploadSha256':self.sha}
        self.drive=Drive('private-token',checkpoint=Mock());self.drive.readback=Mock(return_value={'verified':True})
        self.file={'id':'completed-file','parents':['folder'],'appProperties':{'toyoJob':'job','toyoSha256':self.sha},'size':str(self.path.stat().st_size),'capabilities':{'canDownload':True}}
        self.drive.metadata=Mock(return_value=self.file)

    def upload(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            result=self.drive.upload(self.path,'folder','job','abc12','full',{'tracks':1},self.saved)
        self.assertNotIn('private',output.getvalue());self.assertEqual(self.path.read_bytes(),b'archive bytes')
        return result

    def test_expired_session_rechecks_absence_then_uploads_once_and_reads_back(self):
        self.drive.json=Mock(side_effect=[{'files':[]},{'files':[]},{'storageQuota':{'limit':'100','usage':'0'}}])
        self.drive.request=Mock(side_effect=[response(404),response(200,headers={'Location':'https://www.googleapis.com/upload/drive/new-session'}),response(308),response(200,{'id':'completed-file'})])
        self.assertEqual(self.upload(),{'verified':True})
        self.assertEqual([c.args[0] for c in self.drive.request.call_args_list],['put','post','put','put'])
        self.assertEqual(self.drive.request.call_args_list[-1].kwargs['data'],b'archive bytes')
        self.drive.readback.assert_called_once_with(self.file,self.path.parent,'abc12','full',{'tracks':1},self.sha)
        self.assertEqual(self.drive.checkpoint.call_args_list[0].args[0]['uploadSha256'],self.sha)
        self.assertEqual(self.saved['uploadSession'],'https://www.googleapis.com/upload/drive/expired-private-session')

    def test_completed_write_after_expiry_is_reused_without_starting_an_upload(self):
        self.drive.json=Mock(side_effect=[{'files':[]},{'files':[{'id':'completed-file'}]}]);self.drive.request=Mock(return_value=response(404))
        self.assertEqual(self.upload(),{'verified':True});self.drive.request.assert_called_once();self.drive.readback.assert_called_once()

    def test_ambiguous_paginated_or_mismatched_completed_writes_stop(self):
        for found in [{'files':[{'id':'a'},{'id':'b'}]},{'files':[],'nextPageToken':'next'},{'files':[{'id':'completed-file'}]}]:
            with self.subTest(found=found):
                self.drive.json=Mock(side_effect=[{'files':[]},found]);self.drive.request=Mock(return_value=response(404));self.drive.readback.reset_mock()
                self.drive.metadata=Mock(return_value={**self.file,'size':'999'})
                with self.assertRaises(ArchiveError) as caught:self.upload()
                self.assertEqual(caught.exception.code,'verification_failed');self.drive.request.assert_called_once();self.drive.readback.assert_not_called()

    def test_unknown_status_and_transport_failures_never_replace_sessions(self):
        for status in [401,403,429,500,503]:
            with self.subTest(status=status):
                self.drive.json=Mock(return_value={'files':[]});self.drive.request=Mock(return_value=response(status))
                with self.assertRaises(ArchiveError):self.upload()
                self.drive.request.assert_called_once();self.drive.json.assert_called_once()

    def test_unknown_transport_or_lookup_failure_never_starts_a_replacement(self):
        for failure in ['transport','lookup']:
            with self.subTest(failure=failure):
                self.drive.json=Mock(side_effect=[{'files':[]},ArchiveError('drive_failed')])
                self.drive.request=Mock(side_effect=requests.exceptions.ReadTimeout('private URL') if failure=='transport' else None,return_value=response(404))
                with self.assertRaises((ArchiveError,requests.exceptions.ReadTimeout)):self.upload()
                self.drive.request.assert_called_once()

    def test_valid_session_resumes_acknowledged_offset_without_replacement(self):
        self.drive.json=Mock(return_value={'files':[]})
        self.drive.request=Mock(side_effect=[response(308,headers={'Range':'bytes=0-3'}),response(200,{'id':'completed-file'})])
        self.assertEqual(self.upload(),{'verified':True})
        self.assertEqual([c.args[0] for c in self.drive.request.call_args_list],['put','put'])
        self.assertEqual(self.drive.request.call_args_list[-1].kwargs['data'],b'ive bytes')
        self.assertEqual(self.drive.request.call_args_list[-1].kwargs['headers']['Content-Range'],'bytes 4-12/13')

    def test_capacity_failure_stops_before_creating_replacement(self):
        self.drive.json=Mock(side_effect=[{'files':[]},{'files':[]},{'storageQuota':{'limit':'100','usage':'100'}}]);self.drive.request=Mock(return_value=response(404))
        with self.assertRaises(ArchiveError) as caught:self.upload()
        self.assertEqual(caught.exception.code,'provider_quota_exceeded');self.drive.request.assert_called_once()

    def test_new_session_404_cannot_create_an_unbounded_restart_loop(self):
        self.drive.json=Mock(side_effect=[{'files':[]},{'files':[]},{'storageQuota':{}}])
        self.drive.request=Mock(side_effect=[response(404),response(200,headers={'Location':'https://www.googleapis.com/upload/drive/new-session'}),response(404)])
        with self.assertRaises(ArchiveError):self.upload()
        self.assertEqual([c.args[0] for c in self.drive.request.call_args_list],['put','post','put'])


if __name__=='__main__':unittest.main()
