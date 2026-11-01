# Private Drive archives and temporary Gofile delivery

Public deployment target: `yack-order/library/downloader`. Adapted from
YOTO-json-extractor-v2 by Alberto Fernández; retain EXTRACTOR-LICENSE and the
upstream archive helper notices. The independent GUI checkout is unchanged.

Both workflows have `workflow_dispatch` only, run trusted main code on standard
Ubuntu GitHub-hosted runners, and accept only an opaque job ID. Publishing code
does not process any playlist or Drive file. Configure only
`TOYO_ARCHIVE_RUNNER_TOKEN` in Actions; Google refresh tokens/client secrets and
the Gofile account credential stay in Toyo. Active leases receive short-lived
Google access tokens, refreshed server-side on authorization expiry.

`runner.py` builds future archives, verifies extraction/manifests, uploads
resumably to private Drive, verifies a full SHA-256 download readback, and commits
the receipt. It does not upload a new build to Gofile. Legacy receipt completion
remains compatible, but new source responses require configured Drive storage.

`storage_runner.py` runs explicitly requested inventory/inspection/import/handover
jobs and separately enabled on-demand delivery jobs. Source import reads and
destination import writes use the temporary current-account connections. New
builds and cold-to-hot transfers use the different runtime connection exclusively.
No job silently falls back to an import account.

Imports inventory only the selected folder tree, skip shortcuts, checkpoint
pages, and inspect archives in a resource-limited subprocess. Paths, duplicate
members, symlinks/junctions/sockets, encryption, corruption, identity conflicts,
unreadable audio, mismatched track counts, and manifest failures are rejected.
Current inspection limits: 10,000 members, 1 GiB/member, 8 GiB expanded, 2 GiB
subprocess address space on Linux, 20 minutes wall time, and available runner
disk space. Larger/unsupported candidates remain for review; sources are never
modified. Sanitize copies, remux without transcoding, regenerate safe metadata
and manifests, verify extraction, and upload/read back before indexing. Identical
sanitized content produces deterministic bytes and reuses its Drive object.

Free Gofile delivery uses only its ordinary public page and free upload endpoint.
Page results are present/missing/unknown. A loading shell, challenge, access
restriction, rate limit, or changed layout is unknown and never causes a
replacement. An explicit missing-content page must be observed twice before
replacing an existing copy. No Premium listing/direct-link APIs, fake retention
downloads, account creation, or challenge bypass is used. Returned upload size/MD5
must match the verified cold file; persist the receipt before publishing. This is
provider acknowledgement, not independent Gofile SHA-256 download readback.

No media, private source JSON, Google or Gofile locations, tokens, browser traces,
screenshots, caches, job summaries or workflow artifacts are published. All media
workspaces are temporary and removed on normal success/failure; GitHub destroys
the disposable runner after cancellation. Public logs contain controlled status
codes only. Offline tests use synthetic fixtures and mocked providers:

```sh
python -m unittest discover -s downloader -p 'test_*.py'
```
