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


Storage imports accept an optional bounded `job_ids` JSON array and `batch_id`
from Toyo. A runner installs dependencies once and processes files sequentially
in fresh subprocesses and temporary directories. Every file keeps its own D1
lease, checkpoint and result. Failed files do not block the remainder of the
explicit selection. Toyo defaults to four concurrent runners and five files per
runner, with bounded environment tuning. No runner claims unrelated queued work.
Only pip dependency packages are cached, never private inputs or archives.
Imports do not install Chromium; delivery installs it lazily when checking an
existing Gofile page. The workflow stays dispatch-only and does
not activate automatic processing. Pending work after interruption requires an
explicit Toyo Resume; dispatch generations reject stale workflows.


Inspection retention: inspection sanitizes and packages once, uploads into the
configured private Drive destination and verifies the full readback. Toyo keeps
an encrypted provenance-bound receipt and any embedded original JSON separately
in private R2. Inspection alone never registers a catalog archive or exports the
original metadata. Explicit import downloads and verifies the retained sanitized
file, ingests retained JSON, and registers that same Drive file; it never downloads
original media, sanitizes, compresses or uploads again. Missing, changed, obsolete
or superseded results require explicit re-inspection, with no rebuild fallback.
Unimported staged copies remain private until import or explicit owner cleanup.
Existing completed import rechecks reuse their acknowledged Drive file. These
changes enable no automatic processing and run no canaries on deployment.

## Public Yoto Space discovery and versioned archives

`space-crawl.yml` dispatches `space_crawl.py` using an opaque leased discovery job.
It uses a fresh anonymous Chromium context and checkpoints public navigation,
reply expansion and pagination. Blocked or incomplete scans preserve the baseline.
No login, challenge bypass, or member-only crawling is performed.

Jobs marked `sourceKind=space` in the existing archive protocol capture media bytes
before version observation. Audio/image hashes and ordered sanitized metadata
identify a version. The same captured files are packaged; unchanged versions
reuse their verified archive. Later versions use `(v2)`, `(v3)`, etc. suffixes.
Public text and manifests contain no playback URLs; raw media inputs remain only
in the disposable workspace. The private API enforces source-scoped activation,
leases and verified runtime Drive storage. Publishing these files starts no crawl
or archive job. Run the synthetic tests with the command above; browser tests
also require `python -m playwright install chromium`.

## Momma Bear Audio

`momma_bear.py` performs paid-library discovery in an isolated Chromium context.
Its leased source response supplies encrypted-at-rest membership credentials in
memory only. It imports Yoto links or prepares a mapped MYO from numbered MP3s
when the publisher explicitly provides no Yoto link. `momma_resources.py` downloads
publisher documents into archive supplements with safe ZIP extraction and bounded
streaming. Membership cookies are never forwarded to resource hosts.

The `momma-bear.yml` workflow is dispatched only by the configured Toyo crawler.
Setup, canaries, source-scoped activation and the twice-monthly New York schedule
are managed at `/admin/momma-bear`. See `docs/MOMMA_BEAR_CRAWLER.md` in toyo-api.

Media downloads allow at most ten validated HTTPS redirects per attempt so
public podcast feeds can traverse multiple measurement services. Every hop still
requires a public address, preserves the descriptive User-Agent, and keeps the
existing credential-host restrictions. Redirect loops remain bounded.

Script entry points and imported helpers share `archive_errors.ArchiveError`.
Inspection failures retain their controlled codes even when Python executes
`runner.py` as `__main__`. Resource and archive-safety limits stop the affected
job without repeating the same work; the limits themselves remain unchanged.
