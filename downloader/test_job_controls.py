import json
import threading
import unittest
from unittest.mock import Mock, patch
from runner import Client, ArchiveError

class JobControlsTests(unittest.TestCase):
    def client(self):
        client=Client.__new__(Client)
        client.base='https://example.test/job'
        client.headers={}
        client.stopped=Mock(spec=threading.Event)
        client.stopped.wait.return_value=False
        return client

    def response(self,status,body=None):
        response=Mock()
        response.status_code=status
        response.json.return_value=body
        response.content=json.dumps(body).encode()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        return response

    @patch('runner.requests.post')
    def test_pause_waits_and_retries_same_checkpoint(self,post):
        client=self.client()
        post.side_effect=[self.response(423),self.response(200,{'ok':True})]
        self.assertEqual(client.call('source'),{'ok':True})
        client.stopped.wait.assert_called_once_with(10)
        self.assertEqual(post.call_args_list[0],post.call_args_list[1])

    @patch('runner.requests.post')
    def test_cancellation_is_terminal(self,post):
        client=self.client()
        post.return_value=self.response(410)
        with self.assertRaises(ArchiveError) as error:client.call('complete')
        self.assertEqual(error.exception.code,'job_cancelled')
        client.stopped.wait.assert_not_called()

    @patch('runner.requests.post')
    def test_only_explicit_api_codes_diagnose_withdrawal_or_reconnect(self,post):
        for action in ('claim','source','complete'):
            for body,expected in [({'code':'archive_withdrawn'},'archive_withdrawn'),
                                  ({'code':'reconnect_required'},'reconnect_required'),
                                  ({'error':'Cold archive unavailable'},'source_unavailable'),
                                  ({'code':'private-provider-detail'},'source_unavailable'),
                                  (['reconnect_required'],'source_unavailable')]:
                with self.subTest(action=action,body=body):
                    client=self.client();post.reset_mock()
                    post.return_value=self.response(409,body)
                    with self.assertRaises(ArchiveError) as error:client.call(action)
                    self.assertEqual(error.exception.code,expected)
                    post.assert_called_once();client.stopped.wait.assert_not_called()

    @patch('runner.requests.post')
    def test_malformed_or_oversized_errors_stay_generic(self,post):
        for raw in (b'not json',b'{"code":"reconnect_required","error":"'+b'x'*16384+b'"}',b'\xff'):
            client=self.client();response=self.response(409);response.content=raw;post.return_value=response
            with self.assertRaises(ArchiveError) as error:client.call('source')
            self.assertEqual(error.exception.code,'source_unavailable')
