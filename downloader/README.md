# Toyo headless archive worker

Adapted from YOTO-json-extractor-v2 by Alberto Fernández; see EXTRACTOR-LICENSE.
Filename and archive-integrity helpers retain the upstream MIT notice. The GUI
repository remains independent. This worker adds headless packaging, bounded
streaming, metadata enrichment, canonical Toyo scrubbing, job leases and Gofile.

The production workflow has **workflow_dispatch only**. It never runs on a push,
a pull request, or a schedule. Publishing this package does not process cards.
Toyo's enqueue and execution controls both start disabled. Only enable them
after the bulk import and explicitly requested canary verification.

The workflow runs trusted code from main on standard Ubuntu runners. Configure
TOYO_ARCHIVE_RUNNER_TOKEN as a repository Actions secret; keep GOFILE_API in the
Toyo Worker. The authenticated, leased source response passes its upload token
privately to this runner in memory. Never
put playlist inputs, account IDs, or provider links in dispatch parameters.
The sole input is an opaque job_id issued by Toyo. Do not run against pull-request
code with these secrets. Python 3.12, ffmpeg, and requirements.txt are required.

Offline tests: `python -m unittest discover -s downloader -p test_runner.py`.
These tests synthesize audio; they fetch no playlists and upload no files.

R2 stores source JSON, D1 stores catalog/jobs/receipts, GitHub stores scrubbed
metadata, and Gofile stores media. Media workspaces are ephemeral. An interrupted
fetch/package stage starts again on a fresh runner. A verified upload receipt is
checkpointed in Toyo before completion, so completion retries reuse the upload.
An interruption before receipt checkpointing may leave an orphan Gofile upload;
no unverified result is offered as a download.

The free Gofile account supplies download-page links. Before upload the .7z is
extracted and checked against its SHA-256 manifest. The upload response's size and
MD5 must match the local archive. Save this receipt privately in Toyo before its
checkpoint and publication. `verification: upload-receipt` explicitly means provider
acknowledgement, not independent download/SHA-256 readback. Catalog D1 retains the
file ID, folder ID, page, size, local SHA-256, local/provider MD5 and upload time.
Missing/mismatched evidence remains unavailable. Resume using the saved receipt;
legacy receipts without provider checksums are not silently upgraded or reuploaded.

No Premium listing, search or direct-link endpoint is called. Check JSON `status`
in addition to HTTP status; authentication, Premium requirements, quota and rate
limits produce controlled error codes without logging provider bodies or URLs.
Use one existing account token and a separate folder per archive so download pages
do not reveal other cards. Do not mint guest accounts or retry ambiguous uploads.

The free tier currently has a 10,000 stored-content cap, 100 GB traffic per 30 days,
and expiry after 10 days without downloads. Treat these files as temporary copies;
do not use artificial downloads for retention. Source capture remains in private
R2; no media archives, source links, credentials or gated Gofile URLs go into public
logs, summaries, caches or Actions artifacts. No settings or workflow triggers change
with this correction. Official references: https://gofile.io/api,
https://gofile.io/faq and https://gofile.io/premium.
