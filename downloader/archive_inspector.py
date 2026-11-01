"""Inspect legacy archives as data, sanitize copies, and preserve source files."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys

import py7zr
from PIL import Image
from runner import ArchiveError, digest, strip_source_tags, tag_audio
from archive_safety import create_verified_archive, safe_filename
from privacy import redact_text, register_sensitive_values, scrub_json

MAX_EXPANDED=8*1024**3
MAX_MEMBERS=10000
MAX_FILE=1024**3
AUDIO={'.mp3','.m4a','.aac'}


def safe_extract(archive_path,destination):
    destination=Path(destination)
    with py7zr.SevenZipFile(archive_path,'r') as archive:
        if archive.needs_password():raise ArchiveError('encrypted_archive')
        if len(archive.files)>MAX_MEMBERS:raise ArchiveError('resource_limit')
        total=0;names=set()
        for member in archive.files:
            name=member.filename
            parts=PurePosixPath(name).parts
            canonical=str(PurePosixPath(name))
            if (not parts or canonical!=name.rstrip('/') or name.startswith(('/','\\')) or '\\' in name or ':' in name or
                    any(part in ('..','.') for part in parts) or '\x00' in name or canonical in names or
                    member.is_symlink or member.is_junction or member.is_socket):
                raise ArchiveError('unsafe_archive')
            names.add(canonical)
            size=member.uncompressed or 0
            if size<0 or size>MAX_FILE:raise ArchiveError('resource_limit')
            total+=size
        if total>MAX_EXPANDED or shutil.disk_usage(destination.parent).free<total*2+128*1024**2:
            raise ArchiveError('resource_limit')
        archive.extractall(destination)
    actual=0
    for path in destination.rglob('*'):
        info=path.lstat()
        if path.is_symlink() or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or info.st_nlink>1 and path.is_file():
            raise ArchiveError('unsafe_archive')
        if path.is_file():actual+=info.st_size
    if actual>MAX_EXPANDED:raise ArchiveError('resource_limit')


def verify_manifest(folder, required=False):
    manifest=folder/'manifest.json'
    if not manifest.exists():
        if required:raise ArchiveError('verification_failed')
        return
    if manifest.stat().st_size>4*1024**2:raise ArchiveError('resource_limit')
    try:
        files=json.loads(manifest.read_text())['files']
        actual={str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file() and p!=manifest}
        if set(files)!=actual:raise ArchiveError('verification_failed')
        for name,entry in files.items():
            if name.startswith('/') or '..' in PurePosixPath(name).parts or '\\' in name:raise ArchiveError('unsafe_archive')
            path=folder/name
            if path.stat().st_size!=entry['bytes'] or digest(path)!=entry['sha256']:raise ArchiveError('verification_failed')
    except ArchiveError:raise
    except Exception:raise ArchiveError('verification_failed') from None


def text_fields(path):
    if not path.exists():return {}
    if path.stat().st_size>4*1024**2:raise ArchiveError('resource_limit')
    return {key.strip():value.strip() for key,value in re.findall(r'^([^\r\n:]+)::\s*(.*)$',path.read_text(encoding='utf-8-sig'),re.M)}


def without_notes(value):
    if isinstance(value,dict):return {key:without_notes(item) for key,item in value.items() if not re.search(r'(?i)note|contact|credential|account|owner|user|secret|token|password|address|phone|email',key)}
    if isinstance(value,list):return [without_notes(item) for item in value]
    return value


def natural(path):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r'(\d+)',path.name)]


def sanitize_archive(source,work):
    source=Path(source);work=Path(work);expanded=work/'expanded';expanded.mkdir()
    safe_extract(source,expanded)
    roots=[p.parent for p in expanded.rglob('card.txt')]
    if len(roots)!=1:raise ArchiveError('identity_conflict')
    root=roots[0];verify_manifest(root)
    fields=text_fields(root/'card.txt')
    card={};raw=None;raw_source=None
    for path in sorted(root.glob('*.json')):
        if path.name=='manifest.json':continue
        if path.stat().st_size>4*1024**2:raise ArchiveError('resource_limit')
        decoded=json.loads(path.read_text(encoding='utf-8-sig'))
        candidate=decoded.get('card') or decoded.get('props',{}).get('pageProps',{}).get('card') or (decoded if decoded.get('cardId') else None)
        if candidate:
            if card:raise ArchiveError('identity_conflict')
            card=candidate;raw=decoded;raw_source=path
    ids=set()
    if card.get('cardId'):ids.add(str(card['cardId']))
    if fields.get('CardID'):ids.add(fields['CardID'])
    filename_id=re.search(r'\[([A-Za-z0-9]{5})\](?:\s*\(\d{4}-\d{2}-\d{2}\))?(?:\s*\(\d+\))?\.7z$',source.name,re.I)
    if filename_id:ids.add(filename_id[1])
    if len(ids)!=1 or not re.fullmatch(r'[A-Za-z0-9]{5}',next(iter(ids),'')):raise ArchiveError('identity_conflict')
    card_id=next(iter(ids))
    if str(card.get('content',{}).get('playbackType','')).lower() in {'radio','live','stream'}:raise ArchiveError('incomplete_archive')
    if raw:register_sensitive_values(raw)
    metadata=card.get('metadata') or {}
    title=redact_text(card.get('title') or fields.get('Title') or 'Untitled')
    author=redact_text(metadata.get('author') or fields.get('Author') or 'Unknown')
    category=redact_text(metadata.get('category') or fields.get('Category') or 'Unknown')
    demo='[DEMO]' in source.name or bool(re.search(r'Discover:\s*\d+\s+tracks?',str(metadata.get('description',''))+' '+title))
    tracks=[track for chapter in card.get('content',{}).get('chapters',[]) for track in chapter.get('tracks',[])]
    if any(str(track.get('type','')).lower() in {'radio','live','stream'} for track in tracks):raise ArchiveError('incomplete_archive')
    audio=sorted((p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in AUDIO),key=natural)
    track_text=root/'tracks.txt'
    blocks=[]
    if track_text.exists():
        if track_text.stat().st_size>4*1024**2:raise ArchiveError('resource_limit')
        blocks=re.findall(r'(?ms)^TrackNumber::.*?(?=^TrackNumber::|\Z)',track_text.read_text(encoding='utf-8-sig'))
    expected=len(tracks) or len(blocks)
    if not audio or len(audio)>2000 or not expected or len(audio)!=expected:raise ArchiveError('incomplete_archive')
    if blocks and len(blocks)!=len(audio):raise ArchiveError('incomplete_archive')
    clean=work/'clean';clean.mkdir();folder=clean/safe_filename(title,limit=120);folder.mkdir()
    out_audio=folder/'audio_files';out_audio.mkdir();images=folder/'images';images.mkdir()
    image_names=set()
    for image in (p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in {'.png','.jpg','.jpeg','.gif','.webp'}):
        if image.stat().st_size>16*1024**2:raise ArchiveError('resource_limit')
        name=safe_filename(redact_text(image.stem),limit=150)+'.png'
        if name in image_names:raise ArchiveError('identity_conflict')
        image_names.add(name)
        with Image.open(image) as bitmap:
            if bitmap.width*bitmap.height>25000000:raise ArchiveError('resource_limit')
            clean_image=bitmap.convert('RGBA');clean_image.info.clear();clean_image.save(images/name,format='PNG')
    covers=list(images.glob('*cover*.png'));cover=covers[0].read_bytes() if covers else None
    lines=[];padding=len(str(len(audio)));duration=0
    for number,path in enumerate(audio,1):
        info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(path)],timeout=30,stderr=subprocess.DEVNULL))
        if float(info.get('format',{}).get('duration',0))<=0:raise ArchiveError('incomplete_archive')
        duration+=float(info['format']['duration'])
        track=tracks[number-1] if tracks else dict(re.findall(r'^([^\r\n:]+)::\s*(.*)$',blocks[number-1],re.M))
        track_title=redact_text(track.get('title') or track.get('Title') or path.stem)
        name=safe_filename(f'{number:0{padding}d} - {track_title}',limit=160)+path.suffix.lower()
        output=out_audio/name;shutil.copyfile(path,output)
        strip_source_tags(output);tag_audio(output,track_title,author,title,category,number,len(audio),cover)
        lines.append(f'TrackNumber:: {number:0{padding}d}\nTitle:: {track_title}\nDuration:: {info["format"]["duration"]}')
    (folder/'tracks.txt').write_text('\n\n'.join(lines)+'\n',encoding='utf-8')
    allowed={'Title':title,'Author':author,'CardID':card_id,'Category':category}
    for key in ['Description','Languages','Version','CreatedAt','UpdatedAt']:
        if key in fields:allowed[key]=redact_text(fields[key])
    (folder/'card.txt').write_text('\n'.join(f'{key}:: {value}' for key,value in allowed.items())+'\n',encoding='utf-8')
    public=without_notes(scrub_json(card)) if card else {'cardId':card_id,'title':title,'metadata':{'author':author,'category':category},'content':{'chapters':[]}}
    (folder/'playlist.json').write_text(json.dumps(public,indent=2),encoding='utf-8')
    files={str(p.relative_to(folder)):{'sha256':digest(p),'bytes':p.stat().st_size} for p in sorted(folder.rglob('*')) if p.is_file()}
    (folder/'manifest.json').write_text(json.dumps({'version':1,'cardId':card_id,'files':files},indent=2),encoding='utf-8')
    for path in folder.rglob('*'):os.utime(path,(0,0),follow_symlinks=False)
    os.utime(folder,(0,0))
    stem=redact_text(source.name[:-3])
    if f'[{card_id}]' not in stem:stem+=f'[{card_id}]'
    if demo and '[DEMO]' not in stem:stem=stem.replace(f'[{card_id}]',f'[DEMO][{card_id}]')
    filename=safe_filename(stem,limit=230)+'.7z'
    archive=Path(create_verified_archive(folder,work/filename))
    verification=work/'verify';verification.mkdir();safe_extract(archive,verification)
    verify_manifest(verification/folder.name,required=True)
    raw_path=None
    if raw:
        raw_path=work/'original-playlist.json';shutil.copyfile(raw_source,raw_path);raw_path.chmod(0o600)
    with open(archive,'rb') as handle:md5=hashlib.file_digest(handle,'md5').hexdigest()
    return {'path':str(archive),'rawMetadata':str(raw_path) if raw_path else None,'cardId':card_id,
            'variant':'demo' if demo else 'full','sha256':digest(archive),'md5':md5,'bytes':archive.stat().st_size,
            'sourceSha256':digest(source),'title':title,'tracks':len(audio),
            'index':{'title':title[:240],'tracks':len(audio),'author':author[:500],'category':category[:100],'duration':duration}}


def inspect_archive(source,work):
    result=Path(work)/'inspection.json'
    command=[sys.executable,str(Path(__file__).resolve()),'--inspect',str(source),str(work)]
    try:
        completed=subprocess.run(command,timeout=1200,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if not result.exists():raise ArchiveError('unsafe_archive')
        data=json.loads(result.read_text())
        if completed.returncode or data.get('error'):raise ArchiveError(data.get('error','processing_failed'))
        return data
    except subprocess.TimeoutExpired:raise ArchiveError('resource_limit') from None


if __name__=='__main__':
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE,(MAX_EXPANDED,MAX_EXPANDED))
    if sys.platform=='linux':resource.setrlimit(resource.RLIMIT_AS,(2*1024**3,2*1024**3))
    resource.setrlimit(resource.RLIMIT_CPU,(1100,1100))
    result=Path(sys.argv[3])/'inspection.json'
    try:
        output=sanitize_archive(sys.argv[2],sys.argv[3]);code=0
    except Exception as error:
        output={'error':error.code if isinstance(error,ArchiveError) else 'unsafe_archive'};code=1
    result.write_text(json.dumps(output),encoding='utf-8');result.chmod(0o600)
    raise SystemExit(code)
