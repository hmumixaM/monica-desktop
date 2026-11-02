# monica-faces

Finds faces in Monica's post photos and avatars on the always-on home Mac, and suggests who they are.
Nothing is decided here: every suggestion waits on `/faces` in Monica for a person to confirm it.

## What leaves this machine

Only face boxes (fractions of the image), a quality score, a cluster key and the suggested person go
to Monica. Embeddings and 112-pixel crops stay in `~/.cache/monica-faces/`. Original images are
decoded in memory and never written to disk. Chat images are never offered by the API.

## How it decides

- Each person's single-face avatars, and every face someone has confirmed, are reference faces.
- So is each poster's own face: the face that recurs across the photos one person posted is taken to be
  theirs when it is in at least 4 of their photos with a clear face, at least a quarter of them, and
  1.5 times as many as the next most frequent face (so a couple's shared account names no one). It is
  skipped when it looks like someone else already known, such as a partner with their own avatar.
  Parents who mostly post their children will get the child suggested as themselves; reject it once
  and that face stops being offered.
- A face is suggested as the person whose closest reference face has cosine similarity of at least
  0.40 (ArcFace `w600k_r50` from InsightFace `buffalo_l`), skipping anyone it was already said not to be.
- Faces close to no one are clustered with HDBSCAN so a recurring person can be named once.
- Monica says who posted a photo when it hands the photo out for scanning; that is kept in the local
  `scans` table. Each match fetches confirmations, rejections and ignores, so decisions take effect
  within the hour.

## Set up on the home Mac

1. In Monica's import center, create a token for source `faces`.
2. Make sure the NAS `.env` has a `faces=` entry in `IMPORT_HMAC_SECRETS`.
3. Write both into a file only you can read:

   ```sh
   mkdir -p ~/.config/monica-faces && chmod 700 ~/.config/monica-faces
   cat > ~/.config/monica-faces/env <<'EOF'
   export MONICA_FACES_SECRET=...
   export MONICA_FACES_TOKEN=...
   EOF
   chmod 600 ~/.config/monica-faces/env
   ```

4. Install the dependencies and the job:

   ```sh
   cd ~/monica-faces && uv sync --frozen
   sed -e "s#__INSTALL__#$PWD#" -e "s#__HOME__#$HOME#" launchd/wiki.drw.monica-faces.plist \
     > ~/Library/LaunchAgents/wiki.drw.monica-faces.plist
   launchctl load ~/Library/LaunchAgents/wiki.drw.monica-faces.plist
   ```

The job runs all the time under `nice` with two threads (`monica-faces watch`): it scans new images,
matches after each hour of scanning, and checks for new images every ten minutes once it has caught up.
Logs go to `~/.cache/monica-faces/watch.log`. It is resumable: scans are acknowledged page by page,
and a run that stops resends what Monica did not get.

Images come 25 to a signed request as a tar, with two requests in flight while the Mac detects faces
in the previous batch; the NAS reads four files of each batch at once. Every signed request commits a
nonce, which takes seconds on the busy NAS disk, so asking per image made the scan ten times slower
than detection. A batch that times out three times is asked for again one image at a time; an image
that still times out is recorded as `buffalo_l:timeout` and skipped. When the first three images of
such a batch all time out the NAS is stuck, so the process exits and launchd starts it again five
minutes later. `MONICA_FACES_BATCH` and `MONICA_FACES_FETCHERS` change the batch size and requests in
flight.

A warm detection takes about 0.4 s on the Intel Mac. Keep the
launchd `ProcessType` at `Standard`: `Background` (and `taskpolicy -b`) also throttles sockets and runs
the whole scan about ten times slower; `nice` alone already yields to everything else on the Mac.

## Running by hand

```sh
. ~/.config/monica-faces/env
uv run monica-faces scan --limit 50   # look at 50 more images
uv run monica-faces match             # recompute and send suggestions
uv run monica-faces watch             # what launchd runs
```

A first full pass is faster on an Apple-silicon Mac with
`MONICA_FACES_PROVIDERS='["CoreMLExecutionProvider","CPUExecutionProvider"]'`; copy
`~/.cache/monica-faces/` to the home Mac afterwards so it keeps the same face ids.
