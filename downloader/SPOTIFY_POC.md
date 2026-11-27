# Spotify playlist website proof of concept

Manually run **Spotify playlist website proof of concept** in this repository's
Actions tab, on `main`, with a public Spotify playlist URL and `both`, `spotdown`,
or `spotidown`. The example playlist is `7jlh29r2ASMKfqZKgEAuHz`. This workflow
does not import anything into Yoto or change Toyo production data.

The runner follows the normal websites in Chromium, using separate contexts:

* Spotdown.org: paste the playlist URL, wait for metadata, click **Download Now
  (All Songs)** and then **Download All Songs**. The first button opens a popup;
  the runner closes it. The site starts up to five individual downloads. The
  runner waits for files to finish saving, validates the audio with ffprobe, and
  builds an ordered ZIP with a per-track manifest. Missing tracks and ambiguous
  same-title matches are reported rather than silently treated as complete.
* SpotiDown.cc: paste the playlist URL, click **Download playlist**, choose MP3,
  and click **Download all as ZIP**. Keep the context open until the file is
  saved. Preserve the native ZIP, validate its audio entries, and list provider
  report files. Native metadata and audio sourcing remain provider-controlled.

Each provider has a 120-minute browser deadline. The whole job is capped at 270
minutes. A failure of the first provider does not prevent the second test.
Persistent challenges, rate limits, and unavailable backends are reported;
there is no CAPTCHA solving, stealth configuration, session import, direct
endpoint replay, or repeated restart of blocked sessions.

## Gofile delivery

Configure the repository secret `GOFILE_TOKEN` for the intended existing account.
The runner uses `POST https://upload.gofile.io/uploadfile`, checks JSON status,
MD5 and size, then downloads through the normal Gofile page and checks SHA-256.
Uploads with uncertain responses are not automatically repeated.

The run summary and JSON diagnostics include Gofile page links, file sizes,
hashes and separate provider outcomes. **This repository and its diagnostics are
public; these links can be used by anyone who sees them.** Audio is uploaded to
Gofile only, never GitHub artifacts. Diagnostics exclude tokens, cookies, signed
download links, and raw network traces. Screenshots mask input fields.

Gofile free-tier upload returns a landing page, not a durable direct file URL.
Direct-link creation and API listings require Premium under the current API
documentation. A later worker must use a supported retrieval mechanism; this
  proof of concept verifies the normal browser download. A failed readback is
reported separately from a successful upload, preserving its receipt.

## Verification

Run `python -m unittest discover -s downloader -p 'test_spotify_poc.py'` with the
pinned downloader requirements and ffmpeg installed. Live downloads are manual
only. Overall success requires every selected provider to deliver all expected
tracks and a verified Gofile readback. Partial results are still uploaded when
they contain valid audio, with ambiguous matches retained as unassigned files.
Metadata, filenames, codec and duration do not
prove the recording's identity or its source quality.

## Documentation reviewed

Reviewed November 27, 2026; website behavior and endpoints may change.

* https://spotdown.org/playlist — paste link and use Download All; individual
  song selection is also available.
* https://spotdown.org/faq — no Spotify login required; downloads are files in
  the browser's download location.
* https://spotdown.org/terms-of-service — personal, noncommercial use; the site's
  own materials have a restricted license. This runner does not copy site code.
* https://spotidown.cc/spotify-playlist-downloader/ — native ordered ZIP,
  skipped-track report, and keep the tab open during conversion.
* https://spotidown.cc/faq/ — public catalogue input and JavaScript required;
  no Spotify credentials required. Quality claims are provider claims, not
  independently verified facts.
* https://spotidown.cc/terms-of-service/ — explicitly prohibits automated or
  scripted bulk access and uploading downloaded files elsewhere. The user has
  requested this one-off interoperability test; that is not operator permission
  or a representation that these restrictions are waived.
* https://gofile.io/api — multipart uploads, JSON error statuses and Premium
  requirements for direct-link/listing APIs.
* https://playwright.dev/python/docs/downloads — save downloads before closing
  their context.
