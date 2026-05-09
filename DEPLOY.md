# Deployment — one-time setup

Three short steps Matt does in his browser. Total time: ~5 minutes.

## Step 1 — Create the GitHub repo (30 seconds)

Go to <https://github.com/new> and create a repo with these settings:

- **Repository name**: `dwell-nextgen-interviews`
- **Owner**: `thumbsupexclamationpoint` (the same account as `dwell-clip-reviews`)
- **Visibility**: Public
- **Initialize with**: nothing (no README, no .gitignore, no license — leave all unchecked)

Click **Create repository**. That's it — leave the page open or close it.
Tell Jenny / Obi when it's created and the rest of the repo will be pushed
automatically.

### 1a — Enable GitHub Pages

After the repo is created and Obi has pushed the files:

1. In the repo, go to **Settings** → **Pages**.
2. Under **Build and deployment**:
   - **Source**: `Deploy from a branch`
   - **Branch**: `main` / `/ (root)`
3. Click **Save**.

Within ~30 seconds, the site is live at:
`https://thumbsupexclamationpoint.github.io/dwell-nextgen-interviews/`

## Step 2 — Deploy the Apps Script backend (3 minutes)

1. Go to <https://script.google.com/home>.
2. Click **New project**.
3. Replace the contents of `Code.gs` with the contents of
   [`apps-script/Code.gs`](apps-script/Code.gs) from this repo.
4. Save (⌘+S). Name the project something like
   "Dwell NextGen Reviewer Ingest".
5. Click **Deploy** → **New deployment**.
6. Click the gear icon → **Web app**.
7. Set:
   - **Description**: `nextgen reviewer ingest v1`
   - **Execute as**: `Me (matt@dwellpeninsula.com)`
   - **Who has access**: `Anyone`
8. Click **Deploy**.
9. The first time, you'll be asked to authorize. Click through:
   **Authorize access** → choose `matt@dwellpeninsula.com` → if you see a
   "Google hasn't verified this app" warning, click **Advanced** →
   **Go to Dwell NextGen Reviewer Ingest (unsafe)** → **Allow**.
   (The "unsafe" warning is normal for personal Apps Script — it's running
   as you, not a third party.)
10. Copy the **Web app URL** that ends in `/exec`. It looks like:
    ```
    https://script.google.com/macros/s/AKfycbz.../exec
    ```

**Sanity check**: paste the URL into a new browser tab. You should see the
plain text "Dwell Next Gen reviewer ingest — alive." If you do, the deploy
worked.

## Step 3 — Wire the URL into the page (1 minute)

In `index.html`, find this line near the top of the `<script>` block:

```js
APPS_SCRIPT_URL: "REPLACE_ME_WITH_APPS_SCRIPT_URL",
```

Paste your `/exec` URL between the quotes. Commit and push. GitHub Pages
will redeploy in ~30 seconds.

(If Obi pushed the initial repo for you, ask him to make this change too —
he can edit `index.html` and push the update.)

## Step 4 — Set the password (optional, recommended)

The default password is `dwell-nextgen-2026`. Change it in `index.html`:

```js
PASSWORD: "your-new-password-here",
```

Anything memorable works. The "gate" is just a courtesy — its purpose is to
keep the URL from being usable if it gets shared casually. Real security
isn't required for these recordings.

## Step 5 — Share the link

Once Pages is live, the URL is:

```
https://thumbsupexclamationpoint.github.io/dwell-nextgen-interviews/
```

Share that with the search team along with the password. They'll see the
gate, enter the password, see the candidate list, hit record, talk for
2–5 minutes per candidate, and submit.

---

## Verifying everything works

Quickest end-to-end test:

1. Open the live URL in your browser.
2. Enter the password.
3. Pick any candidate (Chris Miller is a fine test target).
4. Click **Start recording**, allow the mic, say "test test test", click
   **Stop recording**.
5. Click **Submit recording**. Wait for the green "Submitted — thank you"
   status.
6. Open the candidate's Drive subfolder under
   [Reviewer Recordings → Chris Miller](https://drive.google.com/drive/folders/1l14KnQt2uDQz_SfZF1j1W29BsLt5t4Gz).
7. You should see a `.webm` file with a timestamp + slug name.

If it shows up, the page → Apps Script → Drive path is working.

To test transcription, run the Jenny pickup skill:

```bash
python3 ~/Documents/Claude/Projects/Building\ Next\ Gen\ Hub/jenny-skill/scripts/process_recordings.py
```

A `.txt` should appear next to your test `.webm`.

---

## Maintenance

- **New candidate**: add an entry to `CANDIDATES` in `index.html` AND
  `CANDIDATE_FOLDERS` in `apps-script/Code.gs`. Push. Re-deploy the Apps
  Script (Deploy → Manage deployments → edit current → save with new
  version — this updates the existing URL, no need to share a new one).
- **Remove a candidate**: comment out their entry in both files. Their
  Drive folder stays where it is.
- **Rotate password**: change `CONFIG.PASSWORD` in `index.html`, push.
- **Stop accepting submissions**: easiest is to set `CONFIG.PASSWORD` to
  something nobody knows, or delete the Apps Script deployment.
