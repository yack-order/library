"""Offline preservation and repacking tests with synthetic media only."""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

import py7zr
from PIL import Image, PngImagePlugin
import runner
import archive_inspector
import restoration
from archive_errors import ArchiveError
from privacy import scrub_json


class RestorationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory()
        sample = Path(cls.workspace.name)/'sample.mp3'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2','-c:a','libmp3lame',str(sample)],check=True)
        cls.audio = sample.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.workspace.cleanup()

    def image(self, kind='GIF'):
        frames = [Image.new('RGBA',(16,16),color) for color in ('red','blue')]
        output=io.BytesIO()
        if kind=='JPEG':
            frames[0].convert('RGB').save(output,format=kind)
        elif kind=='STATIC':
            metadata=PngImagePlugin.PngInfo();metadata.add_text('Comment','original@example.test')
            frames[0].save(output,format='PNG',pnginfo=metadata)
        else:
            frames[0].save(output,format=kind,save_all=True,append_images=frames[1:],duration=[100,250],loop=3)
        return output.getvalue()

    def card(self):
        return {'cardId':'abc12','title':'Interactive','metadata':{'author':'Published Author','category':'Stories','cover':{'imageL':'https://private.test/icon?token=credential'}},'content':{
            'cover':{'imageL':'https://private.test/icon?token=credential'},
            'playbackType':'interactive','config':{'disableTrackNav':True,'autoadvance':'none'},
            'chapters':[{'key':key,'defaultTrackDisplay':'yoto:#'+'A'*43,
                         'tracks':[{'key':'01','title':'Same title','type':'audio',
                                    'trackUrl':'https://private.test/audio?token=credential',
                                    'display':{'iconUrl16x16':'https://private.test/icon?token=credential'},
                                    'events':{'onRhb':{'cmd':'goto','params':{'chapterkey':'second','trackKey':'01'}}}}]}
                        for key in ('first','second')]}}

    def build(self, root, kind='GIF', prefetched=False, audio_mime='audio/mpeg'):
        card=self.card();image=self.image(kind)
        public=scrub_json(copy.deepcopy(card))
        work=root/'build';work.mkdir()
        def download(url,path,**kwargs):
            visual='icon' in url or 'card-content' in url
            Path(path).write_bytes(image if visual else self.audio)
            return 'image/gif' if visual else audio_mime
        captures=None
        with patch.object(runner,'stream_file',side_effect=download):
            if prefetched:
                capture=root/'capture';capture.mkdir()
                captures,_=runner.capture_space_media({'card':card},capture)
            archive=runner.build_archive({'card':card,'publicCard':public},work,captures)
        restored=root/'restored';restored.mkdir()
        archive_inspector.verify_generated_archive(archive,restored)
        return archive,restored/'Interactive',image,public

    def test_apng_http_mime_preserves_original_animation_bytes(self):
        payload=self.image('PNG')
        response=Mock(status_code=200,is_redirect=False)
        response.headers={'Content-Type':'image/apng','Content-Length':str(len(payload))}
        response.iter_content.return_value=[payload]
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        session=Mock();session.get.return_value=response
        session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
        with tempfile.TemporaryDirectory() as tmp, patch.object(runner,'public_url',side_effect=lambda url:url), patch.object(runner.requests,'Session',return_value=session):
            saved,mime=runner.normalized_image('https://images.test/misleading.gif',Path(tmp)/'icon.png')
            self.assertEqual(mime,'image/png')
            self.assertEqual(saved.read_bytes(),payload)
            with Image.open(saved) as image:
                self.assertEqual(image.n_frames,2);self.assertEqual(image.info['loop'],3)

    def test_roundtrip_mapping_and_original_visual_formats(self):
        for kind in ('STATIC','JPEG','GIF','PNG','WEBP'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                archive,folder,image,public=self.build(Path(tmp),kind,prefetched=kind=='GIF')
                mapping=restoration.verify_map(folder)
                self.assertEqual(len(mapping['audio']),2)
                self.assertEqual([e['chapterKey'] for e in mapping['audio']],['first','second'])
                self.assertEqual([e['trackKey'] for e in mapping['audio']],['01','01'])
                self.assertEqual(len({e['path'] for e in mapping['audio']}),2)
                self.assertEqual(len(mapping['visuals']),6)
                for entry in mapping['visuals']:
                    self.assertEqual((folder/entry['path']).read_bytes(),image)
                    self.assertEqual(entry['sha256'],hashlib.sha256(image).hexdigest())
                self.assertEqual(len(list((folder/'images').iterdir())),2)
                self.assertEqual(json.loads((folder/mapping['playlist']).read_text())['content'],public['content'])
                text=(folder/restoration.MAP_NAME).read_text()
                self.assertNotIn('https://',text);self.assertNotIn('credential',text)
                output=Path(tmp)/'import';output.mkdir()
                result=archive_inspector.sanitize_archive(archive,output)
                unpacked=Path(tmp)/'repacked';unpacked.mkdir()
                archive_inspector.safe_extract(result['path'],unpacked)
                repacked=unpacked/'Interactive';archive_inspector.verify_manifest(repacked,required=True)
                again=restoration.verify_map(repacked)
                self.assertEqual([e['chapterKey'] for e in again['audio']],['first','second'])
                self.assertTrue(all(e['status']=='verified' for e in again['audio']))
                for entry in again['visuals']:self.assertEqual((repacked/entry['path']).read_bytes(),image)
                self.assertEqual(json.loads((repacked/'playlist.json').read_text())['content'],public['content'])

    def test_new_wav_archive_can_be_repacked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sample=root/'source.wav'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2',str(sample)],check=True)
            with patch.object(self,'audio',sample.read_bytes()):
                archive,_,_,_=self.build(root,audio_mime='audio/wav')
            work=root/'import';work.mkdir();result=archive_inspector.sanitize_archive(archive,work)
            unpacked=root/'wav-result';unpacked.mkdir();archive_inspector.safe_extract(result['path'],unpacked)
            mapping=restoration.verify_map(unpacked/'Interactive')
            self.assertTrue(all(e['path'].endswith('.wav') for e in mapping['audio']))

    def test_legacy_associations_are_unverified_and_images_unmapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);archive,folder,image,public=self.build(root)
            (folder/restoration.MAP_NAME).unlink();(folder/'manifest.json').unlink()
            legacy=root/'legacy[abc12].7z'
            with py7zr.SevenZipFile(legacy,'w') as handle:handle.writeall(folder,'Interactive')
            work=root/'import';work.mkdir();result=archive_inspector.sanitize_archive(legacy,work)
            unpacked=root/'legacy-result';unpacked.mkdir();archive_inspector.safe_extract(result['path'],unpacked)
            mapping=restoration.verify_map(unpacked/'Interactive')
            self.assertTrue(all(e['status']=='legacy_order_unverified' for e in mapping['audio']))
            self.assertTrue(any(e['status']=='unmapped' and e['pointer'] is None for e in mapping['visuals']))
            self.assertTrue(any(e['status']=='unresolved' for e in mapping['visuals']))

    def test_repacking_uses_mapping_instead_of_filename_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,folder,_,_=self.build(root)
            mapping=restoration.verify_map(folder)
            for index,entry in enumerate(mapping['audio']):
                source=folder/entry['path'];target=source.parent/('z.mp3' if index==0 else 'a.mp3')
                source.rename(target);entry.update(restoration.file_entry(folder,target))
            cover=next(e for e in mapping['visuals'] if e['pointer']=='/metadata/cover/imageL')
            old_path=cover['path'];original=folder/old_path;renamed=original.with_name('generic.bin');original.rename(renamed)
            for entry in mapping['visuals']:
                if entry.get('path')==old_path:entry.update(restoration.file_entry(folder,renamed))
            restoration.write_map(folder,mapping)
            files={p.relative_to(folder).as_posix():{'bytes':p.stat().st_size,'sha256':restoration.checksum(p)}
                   for p in folder.rglob('*') if p.is_file() and p.name!='manifest.json'}
            (folder/'manifest.json').write_text(json.dumps({'files':files}))
            source=root/'renamed[abc12].7z'
            with py7zr.SevenZipFile(source,'w') as handle:handle.writeall(folder,'Interactive')
            work=root/'import';work.mkdir();result=archive_inspector.sanitize_archive(source,work)
            unpacked=root/'mapped-result';unpacked.mkdir();archive_inspector.safe_extract(result['path'],unpacked)
            again=restoration.verify_map(unpacked/'Interactive')
            # Retagging each file at the same original position must reproduce its hash.
            self.assertEqual([e['sha256'] for e in again['audio']],[e['sha256'] for e in mapping['audio']])

    def test_missing_and_duplicate_keys_are_not_regenerated(self):
        card=self.card()
        card['content']['chapters'][1]['key']='first'
        del card['content']['chapters'][1]['tracks'][0]['key']
        with tempfile.TemporaryDirectory() as tmp,patch.object(self,'card',return_value=card):
            _,folder,_,_=self.build(Path(tmp))
            mapping=restoration.verify_map(folder)
            self.assertEqual([e['chapterKey'] for e in mapping['audio']],['first','first'])
            self.assertEqual([e['trackKey'] for e in mapping['audio']],['01',None])
            self.assertEqual([e['chapterIndex'] for e in mapping['audio']],[0,1])

    def test_repacking_keeps_auxiliary_evidence_and_rebinds_episode_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);_,folder,_,_=self.build(root)
            mapping=restoration.verify_map(folder)
            rows=[]
            for index,entry in enumerate(mapping['audio']):
                old=folder/entry['path'];renamed=old.with_name(f'legacy-{index}.mp3');old.rename(renamed)
                entry.update(restoration.file_entry(folder,renamed))
                rows.append({'id':('a' if index==0 else 'b')*64,'title':'Episode',
                             'releaseDate':'2026-11-13','releasedAt':'2026-11-13T12:00:00Z',
                             'memberPath':entry['path'],'sha256':entry['sha256'],'bytes':entry['bytes'],
                             'privateUrl':'https://private.test/credential'})
            restoration.write_map(folder,mapping)
            (folder/'podcast-episodes.json').write_text(json.dumps(list(reversed(rows))))
            version={'id':'version-1','number':1,'fingerprint':'f'*64}
            snapshot={'sha256':'a'*64,'label':'2026-11-13','trackCount':2,'kind':'immutable_historical_playlist'}
            (folder/'playlist-version.json').write_text(json.dumps({**version,'privateUrl':'https://private.test/credential'}))
            (folder/'source-snapshot.json').write_text(json.dumps(snapshot))
            (folder/'auxiliary-array.json').write_text('[1,2]')
            files={p.relative_to(folder).as_posix():{'bytes':p.stat().st_size,'sha256':restoration.checksum(p)}
                   for p in folder.rglob('*') if p.is_file() and p.name!='manifest.json'}
            (folder/'manifest.json').write_text(json.dumps({'files':files}))
            source=root/'evidence[abc12].7z'
            with py7zr.SevenZipFile(source,'w') as handle:handle.writeall(folder,'Interactive')
            work=root/'import';work.mkdir();result=archive_inspector.sanitize_archive(source,work)
            unpacked=root/'evidence-result';unpacked.mkdir();archive_inspector.safe_extract(result['path'],unpacked)
            saved=unpacked/'Interactive';archive_inspector.verify_manifest(saved,required=True)
            again=restoration.verify_map(saved)
            inventory=json.loads((saved/'podcast-episodes.json').read_text())
            self.assertEqual([e['id'] for e in inventory],['a'*64,'b'*64])
            for episode,audio in zip(inventory,again['audio']):
                self.assertEqual(episode['memberPath'],audio['path'])
                self.assertEqual(episode['sha256'],audio['sha256'])
                self.assertEqual(episode['bytes'],audio['bytes'])
                self.assertNotIn('privateUrl',episode)
            self.assertEqual(json.loads((saved/'playlist-version.json').read_text()),version)
            self.assertEqual(json.loads((saved/'source-snapshot.json').read_text()),snapshot)

            for failure in ['path','hash','duplicate','coverage']:
                with self.subTest(inventory=failure):
                    bad=copy.deepcopy(rows)
                    if failure=='path':bad[0]['memberPath']='../escape.mp3'
                    if failure=='hash':bad[0]['sha256']='0'*64
                    if failure=='duplicate':bad[1]['id']=bad[0]['id']
                    if failure=='coverage':bad=bad[:1]
                    (folder/'podcast-episodes.json').write_text(json.dumps(bad))
                    files={p.relative_to(folder).as_posix():{'bytes':p.stat().st_size,'sha256':restoration.checksum(p)}
                           for p in folder.rglob('*') if p.is_file() and p.name!='manifest.json'}
                    (folder/'manifest.json').write_text(json.dumps({'files':files}))
                    damaged=root/f'bad-{failure}[abc12].7z'
                    with py7zr.SevenZipFile(damaged,'w') as handle:handle.writeall(folder,'Interactive')
                    invalid=root/f'invalid-{failure}';invalid.mkdir()
                    with self.assertRaises(ArchiveError):archive_inspector.sanitize_archive(damaged,invalid)

    def test_mapping_tampering_is_rejected(self):
        for failure in ('unsafe','missing','hash','bytes','pointer','position','manifest','duplicate'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                _,folder,_,_=self.build(Path(tmp))
                mapping=restoration.verify_map(folder)
                if failure=='unsafe':mapping['audio'][0]['path']='../escape.mp3'
                if failure=='missing':mapping['audio'][0]['path']='audio_files/missing.mp3'
                if failure=='hash':mapping['audio'][0]['sha256']='0'*64
                if failure=='bytes':mapping['audio'][0]['bytes']=0
                if failure=='pointer':mapping['visuals'][0]['pointer']='/missing'
                if failure=='position':mapping['audio'][0]['chapterKey']='changed'
                if failure=='duplicate':mapping['audio'][1].update({k:mapping['audio'][0][k] for k in ('path','bytes','sha256')})
                (folder/restoration.MAP_NAME).write_text(json.dumps(mapping))
                manifest=json.loads((folder/'manifest.json').read_text())['files']
                if failure=='manifest':manifest.pop(mapping['audio'][0]['path'])
                with self.assertRaises(ArchiveError):restoration.verify_map(folder,manifest)

    def test_map_cannot_bypass_playlist_limit_or_follow_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);playlist=root/'playlist.json'
            playlist.write_text(json.dumps({'content':{'chapters':[]}}))
            restoration.write_map(root,restoration.new_map('playlist.json'))
            playlist.write_bytes(b' '*(4*1024**2+1))
            with self.assertRaises(ArchiveError) as caught:restoration.verify_map(root)
            self.assertEqual(caught.exception.code,'resource_limit')
            mapping=root/restoration.MAP_NAME;target=root/'target.json';mapping.rename(target);mapping.symlink_to(target)
            with self.assertRaises(ArchiveError) as caught:restoration.verify_map(root)
            self.assertEqual(caught.exception.code,'unsafe_archive')

    def test_image_validation_and_misleading_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'misleading.jpg';source.write_bytes(self.image())
            target,mime=restoration.preserve_image(source,root/'original')
            self.assertEqual(target.suffix,'.gif');self.assertEqual(mime,'image/gif')
            self.assertEqual(source.read_bytes(),target.read_bytes())
            for limit in ('MAX_IMAGE_BYTES','MAX_IMAGE_PIXELS','MAX_ANIMATION_FRAMES','MAX_ANIMATION_PIXELS'):
                with self.subTest(limit=limit),patch.object(restoration,limit,1):
                    with self.assertRaises(ArchiveError):restoration.image_info(source)
            source.write_bytes(b'<html>not an image</html>')
            with self.assertRaises(ArchiveError):restoration.image_info(source)

    def test_visual_context_does_not_capture_audio_endpoints(self):
        card={'content':{'ambient':{'audioUrl':'https://media.test/audio.mp3',
             'display':{'iconUrl16x16':'https://media.test/icon.gif',
                        'audio':{'url':'https://media.test/other.mp3'},
                        'dynamic':{'icon':'yoto:#'+'A'*43}}},
             'trackDisplayAudioUrl':'https://media.test/track.mp3'}}
        self.assertEqual(list(restoration.visual_references(card)),[
            ('/content/ambient/display/dynamic/icon','yoto:#'+'A'*43),
            ('/content/ambient/display/iconUrl16x16','https://media.test/icon.gif')])

    def test_unsupported_visual_reference_is_explicit(self):
        card={'content':{'chapters':[{'display':{'icon16x16':'unsupported:reference'}}]}}
        self.assertEqual(list(restoration.visual_references(card)),[('/content/chapters/0/display/icon16x16','unsupported:reference')])
        self.assertIsNone(restoration.visual_url('unsupported:reference'))

        card=self.card();card['content']['chapters'][0]['display']={'icon16x16':'unsupported:reference'}
        with tempfile.TemporaryDirectory() as tmp,patch.object(self,'card',return_value=card):
            _,folder,_,_=self.build(Path(tmp))
            mapping=restoration.verify_map(folder)
            self.assertIn({'pointer':'/content/chapters/0/display/icon16x16','status':'unresolved'},mapping['visuals'])
