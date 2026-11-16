import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import copy
import runner
import unittest
from unittest.mock import Mock,patch
from podcasts import episode_filenames,prepare_podcast,verified_inventory,restore_previous
from runner import ArchiveError

class PodcastTests(unittest.TestCase):
    def test_preserved_previous_uses_bounded_public_download_and_checks_sha256(self):
        data=b'exact archived bytes';sha=hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp);drive=Mock();previous={'pageUrl':'https://gofile.io/d/example','filename':'Archive.7z','bytes':len(data),'sha256':sha}
            def download(url,name,path,maximum):
                self.assertEqual(maximum,len(data));path.write_bytes(data)
            def verify(path,restored):(restored/'archive').mkdir()
            with patch('gofile_presence.download_page',side_effect=download),patch('archive_inspector.verify_generated_archive',side_effect=verify):
                self.assertEqual(restore_previous({'previous':previous},drive,work).name,'archive')
            drive.download.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp,patch('gofile_presence.download_page',side_effect=lambda url,name,path,maximum:path.write_bytes(b'wrong')):
            with self.assertRaises(ArchiveError):restore_previous({'previous':previous},Mock(),Path(tmp))

    def test_release_date_names_and_collisions(self):
        rows=[{'id':'a'*64,'releaseDate':'2026-11-13','title':'Same / Title'},
              {'id':'b'*64,'releaseDate':'2026-11-13','title':'Same / Title'},
              {'id':'c'*64,'releaseDate':None,'title':'../unknown'}]
        names=episode_filenames(rows)
        self.assertTrue(names[0].startswith('2026-11-13 - '));self.assertIn('['+'a'*64+']',names[0])
        self.assertNotEqual(names[0],names[1]);self.assertTrue(names[2].startswith('Unknown date - '))
        self.assertTrue(all('/' not in name and '\\' not in name for name in names))

    def test_builder_dates_files_and_preserves_retained_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp);sample=work/'sample.mp3'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2','-c:a','libmp3lame',str(sample)],check=True)
            raw={'cardId':'pod01','title':'Podcast','content':{'chapters':[{'tracks':[{'key':'one','title':'Episode','trackUrl':'https://cdn.example.com/one.mp3'}]}]}}
            public=copy.deepcopy(raw);del public['content']['chapters'][0]['tracks'][0]['trackUrl']
            episode={'id':'a'*64,'title':'Episode','releaseDate':'2026-11-13','releasedAt':'2026-11-13T12:00:00Z','retained':{'sha256':hashlib.sha256(sample.read_bytes()).hexdigest(),'bytes':sample.stat().st_size}}
            output=work/'output';output.mkdir()
            with patch.object(runner,'stream_file') as download:
                runner.build_archive({'card':raw,'publicCard':public,'podcast':{'volume':1,'episodes':[episode]}},output,{'audio:0':(sample,'','audio/mpeg')})
            download.assert_not_called()
            audio=output/'Podcast/audio_files/2026-11-13 - Episode.mp3'
            self.assertEqual(audio.read_bytes(),sample.read_bytes())
            inventory=json.loads((output/'Podcast/podcast-episodes.json').read_text())
            self.assertEqual(inventory[0]['memberPath'],'audio_files/2026-11-13 - Episode.mp3')

    def test_retained_audio_is_reused_without_media_download(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp);root=work/'root';(root/'audio_files').mkdir(parents=True)
            audio=root/'audio_files/old.mp3';audio.write_bytes(b'retained audio')
            retained={'memberPath':'audio_files/old.mp3','sha256':hashlib.sha256(audio.read_bytes()).hexdigest(),'bytes':audio.stat().st_size}
            source={'card':{'cardId':'pod01'},'podcast':{'episodes':[{'id':'a'*64,'retained':retained}]}}
            with patch('podcasts.restore_previous',return_value=root),patch('podcasts.stream_file') as download:
                captures=prepare_podcast(source,Mock(),work,Mock())
            download.assert_not_called();self.assertEqual(captures['audio:0'][0],audio)

    def test_changed_retained_file_fails_without_redownloading(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp);root=work/'root';(root/'audio_files').mkdir(parents=True)
            (root/'audio_files/old.mp3').write_bytes(b'changed')
            source={'podcast':{'episodes':[{'id':'a'*64,'retained':{'memberPath':'audio_files/old.mp3','sha256':'a'*64,'bytes':7}}]}}
            with patch('podcasts.restore_previous',return_value=root),patch('podcasts.stream_file') as download:
                with self.assertRaises(ArchiveError):prepare_podcast(source,Mock(),work,Mock())
            download.assert_not_called()

    def test_inventory_rejects_changed_retained_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            restored=Path(temp);root=restored/'Show';(root/'audio_files').mkdir(parents=True)
            audio=root/'audio_files/a.mp3';audio.write_bytes(b'audio')
            row={'id':'a'*64,'memberPath':'audio_files/a.mp3','sha256':hashlib.sha256(b'audio').hexdigest(),'bytes':5}
            (root/'podcast-episodes.json').write_text(json.dumps([row]))
            source={'podcast':{'episodes':[{'id':'a'*64,'retained':{'sha256':'b'*64,'bytes':5}}]}}
            with self.assertRaises(ArchiveError):verified_inventory(restored,source)

if __name__=='__main__':unittest.main()
