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
