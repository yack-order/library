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

The free Gofile account supplies download-page links. Delivery fails closed if
upload responses do not provide a file URL usable for independent checksum
readback. Account/API behavior must be verified by a separately authorized canary
before activation. No Premium-only direct-link API is called.

The package remuxes audio without re-encoding to remove source tags and chapters,
then adds scrubbed MP3/MP4 tags. Raw ADTS AAC keeps native audio and text metadata;
it cannot embed MP4 tags. No raw JSON, media, source URLs, tokens or gated Gofile
links are uploaded to Actions artifacts, caches, logs, or job summaries.
