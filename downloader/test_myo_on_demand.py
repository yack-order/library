"""Offline Gofile-only protocol tests: no media, provider uploads, or Drive calls."""
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import runner

class MyoProtocolTests(unittest.TestCase):
    def client(self, claimed, source=None):
        client=Mock()
        client.headers={}
        client.lost.is_set.return_value=False
        client.stopped=Mock()
        def call(action, payload=None):
            if action=='claim': return {'leaseToken':'lease',**claimed}
            if action=='source': return source
            return {'ok':True}
        client.call.side_effect=call
        return client

    def test_uncertain_upload_is_held_without_source_build_or_upload(self):
        client=self.client({'uploadUncertain':True})
        with patch.object(runner,'Client',return_value=client),patch.object(runner,'build_archive') as build,patch.object(runner,'upload') as upload:
            self.assertEqual(runner.main(),1)
        build.assert_not_called();upload.assert_not_called()
        self.assertEqual([c.args[0] for c in client.call.call_args_list],['claim','fail'])
        self.assertEqual(client.call.call_args.args[1]['code'],'upload_uncertain')

    def test_presence_only_job_never_builds_uploads_or_requests_credentials(self):
        client=self.client({}, {'storageMode':'gofile-check','pageUrl':'https://gofile.io/d/public','filename':'Family.7z'})
        with patch.object(runner,'Client',return_value=client),patch('gofile_presence.check_page',return_value='unknown') as presence,patch.object(runner,'build_archive') as build,patch.object(runner,'upload') as upload:
            self.assertEqual(runner.main(),0)
        presence.assert_called_once_with('https://gofile.io/d/public','Family.7z');build.assert_not_called();upload.assert_not_called()
        self.assertEqual([c.args[0] for c in client.call.call_args_list],['claim','source','myo-presence'])

    def test_verified_packaging_and_durable_intent_precede_the_only_gofile_upload(self):
        client=self.client({}, {'storageMode':'gofile-only','gofileToken':'synthetic','card':{},'publicCard':{}})
        order=[]
        def upload(path, token, on_uploaded):
            order.append('upload');self.assertEqual(client.call.call_args.args[0],'gofile-intent');on_uploaded({'pageUrl':'https://gofile.io/d/public'});return {'pageUrl':'https://gofile.io/d/public'}
        with patch.object(runner,'Client',return_value=client),patch.object(runner,'build_archive',return_value=Path('/synthetic/Family.7z')),patch('archive_inspector.verify_generated_archive',side_effect=lambda *a:order.append('verified')),patch.object(runner,'upload',side_effect=upload):
            self.assertEqual(runner.main(),0)
        self.assertEqual(order,['verified','upload'])
        self.assertEqual([c.args[0] for c in client.call.call_args_list],['claim','source','gofile-intent','receipt','checkpoint','complete'])

    def test_durable_receipt_recovery_never_refetches_source_or_reuploads(self):
        client=self.client({'uploadReceipt':{'pageUrl':'https://gofile.io/d/public'}})
        with patch.object(runner,'Client',return_value=client),patch.object(runner,'verify_upload',return_value={'pageUrl':'https://gofile.io/d/public'}),patch.object(runner,'build_archive') as build,patch.object(runner,'upload') as upload:
            self.assertEqual(runner.main(),0)
        build.assert_not_called();upload.assert_not_called()
        self.assertEqual([c.args[0] for c in client.call.call_args_list],['claim','checkpoint','complete'])

if __name__=='__main__':unittest.main()
