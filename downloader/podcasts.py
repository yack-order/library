"""Reuse verified podcast audio; source URL rotation never replaces a retained episode."""
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from runner import ArchiveError, AUDIO_FORMATS, digest, stream_file
from archive_safety import safe_filename, MAX_BUILD_FILE


def episode_filenames(episodes):
    stems=[]
    for episode in episodes:
        day=episode.get('releaseDate')
        try:
            if not isinstance(day,str) or date.fromisoformat(day).isoformat()!=day:raise ValueError()
        except (ValueError,TypeError):day='Unknown date'
        stems.append(safe_filename(f"{day} - {episode.get('title') or 'Untitled episode'}",limit=140))
    counts={stem.casefold():sum(s.casefold()==stem.casefold() for s in stems) for stem in stems}
    return [f'{stem} [{episode["id"]}]' if counts[stem.casefold()]>1 else stem for stem,episode in zip(stems,episodes)]


def restore_previous(podcast,drive,work):
    previous=podcast.get('previous')
    if not previous:return None
    from archive_inspector import verify_generated_archive
    path=work/'previous.7z';drive.download(previous['fileId'],path,maximum=previous['bytes'])
    if path.stat().st_size!=previous['bytes'] or digest(path)!=previous['sha256']:raise ArchiveError('verification_failed')
    restored=work/'previous';restored.mkdir();verify_generated_archive(path,restored)
    path.unlink()
    return next(restored.iterdir())


def legacy_inventory(root,podcast,card_id):
    documents=[]
    for path in root.glob('*.json'):
        try:
            card=json.loads(path.read_text(encoding='utf-8'))
            if card.get('cardId')==card_id and isinstance(card.get('content'),dict):documents.append(card)
        except (ValueError,AttributeError):pass
    if len(documents)!=1:raise ArchiveError('podcast_legacy_inventory_required')
    tracks=[t for c in documents[0]['content'].get('chapters',[]) for t in c.get('tracks',[])]
    files=list((root/'audio_files').glob('*'))
    if len(tracks)!=len(files) or not tracks:raise ArchiveError('podcast_legacy_inventory_required')
    indexed={}
    for path in files:
        match=re.match(r'^(\d+) - ',path.name)
        if not match or path.suffix not in set(AUDIO_FORMATS.values()) or int(match[1]) in indexed:raise ArchiveError('podcast_legacy_inventory_required')
        indexed[int(match[1])]=path
    known={e['providerKey']:e for e in podcast.get('legacyCandidates',podcast['episodes']) if e.get('providerKey')}
    result=[]
    for number,track in enumerate(tracks,1):
        path=indexed.get(number)
        if path is None:raise ArchiveError('podcast_legacy_inventory_required')
        key=track.get('key');episode=known.get(key,{})
        if not key:raise ArchiveError('podcast_legacy_inventory_required')
        result.append({'id':episode.get('id') or hashlib.sha256(('yoto:'+key).encode()).hexdigest(),
                       'providerKey':key,'title':episode.get('title') or track.get('title') or 'Untitled episode',
                       'releasedAt':episode.get('releasedAt'),'releaseDate':episode.get('releaseDate'),
                       'memberPath':str(path.relative_to(root)),'sha256':digest(path),'bytes':path.stat().st_size})
    return {'archiveSha256':podcast['previous']['sha256'],'episodes':result}


def prepare_podcast(source,drive,work,client):
    podcast=source['podcast'];previous=restore_previous(podcast,drive,work)
    if podcast.get('legacy'):
        if previous is None:raise ArchiveError('podcast_legacy_inventory_required')
        client.call('podcast-legacy',legacy_inventory(previous,podcast,source['card']['cardId']))
        return None
    captures={};folder=work/'podcast-captures';folder.mkdir()
    for index,episode in enumerate(podcast['episodes']):
        retained=episode.get('retained')
        if retained:
            member=retained.get('memberPath','')
            if previous is None or not re.fullmatch(r'audio_files/[^/\\\x00-\x1f]+\.(mp3|m4a|aac|ogg)',member):raise ArchiveError('verification_failed')
            path=previous/member
            if not path.is_file() or path.stat().st_size!=retained['bytes'] or digest(path)!=retained['sha256']:raise ArchiveError('verification_failed')
            mime=next((m for m,ext in AUDIO_FORMATS.items() if ext==path.suffix),None)
            if mime is None:raise ArchiveError('verification_failed')
        else:
            url=episode.get('url')
            if not url:raise ArchiveError('podcast_episode_unavailable')
            path=folder/str(index)
            mime=stream_file(url,path,maximum=MAX_BUILD_FILE,allowed=set(AUDIO_FORMATS))
        captures['audio:'+str(index)]=(path,episode.get('url',''),mime)
    return captures


def verified_inventory(restored,source):
    root=next(restored.iterdir());path=root/'podcast-episodes.json'
    if not path.is_file():raise ArchiveError('verification_failed')
    rows=json.loads(path.read_text(encoding='utf-8'))
    expected={e['id']:e for e in source['podcast']['episodes']}
    if len(rows)!=len(expected):raise ArchiveError('verification_failed')
    for row in rows:
        if row['id'] not in expected:raise ArchiveError('verification_failed')
        audio=root/row['memberPath']
        if audio.stat().st_size!=row['bytes'] or digest(audio)!=row['sha256']:raise ArchiveError('verification_failed')
        retained=expected[row['id']].get('retained')
        if retained and (row['sha256']!=retained['sha256'] or row['bytes']!=retained['bytes']):raise ArchiveError('podcast_archive_would_shrink')
    return [{k:row[k] for k in ['id','memberPath','sha256','bytes']} for row in rows]
