# Dwell Next Gen Director — reviewer reflection hub

A static webpage where the search team records voice reflections on
candidates for Dwell Church's Next Gen Director role. Recordings drop
into Google Drive, where Jenny transcribes them via Matt's local Whisper
server and synthesizes the highlights into each candidate's notes doc.

## Architecture

```
                            ┌────────────────────────────┐
                            │  Search team reviewer       │
                            │  (browser, mic permission)  │
                            └──────────────┬──────────────┘
                                           │
                                           │  HTTPS
                                           │  (multipart form, base64 audio)
                                           ▼
                            ┌────────────────────────────┐
                            │  Google Apps Script         │
                            │  doPost(e) → Drive          │
                            └──────────────┬──────────────┘
                                           │
                                           ▼
                  Google Drive — "Reviewer Recordings/<candidate>/<file>.webm"
                                           │
                                           │  Jenny runs nextgen-reviewer-pickup
                                           ▼
                            ┌────────────────────────────┐
                            │  Local Whisper server       │
                            │  (127.0.0.1:12017)          │
                            └──────────────┬──────────────┘
                                           │
                                           ▼
                  Drive — sibling .txt transcript next to each audio file
                                           │
                                           ▼
                  Jenny synthesizes into the candidate notes doc
```

## Repo layout

```
dwell-nextgen-interviews/
├── index.html                  # the static recording page (GitHub Pages root)
├── apps-script/
│   └── Code.gs                 # backend: receives uploads, writes to Drive
├── jenny-skill/
│   ├── SKILL.md                # Jenny's transcription pickup skill
│   └── scripts/
│       └── process_recordings.py
└── DEPLOY.md                   # one-time setup steps for Matt
```

## Hosting

- **Page**: GitHub Pages, served from `main` branch root.
- **Backend**: Google Apps Script web app deployed as "Anyone can access,
  runs as Matt." Free, no service account needed, inherits Matt's Drive
  write permissions.
- **Transcription**: Matt's existing local Whisper server (whisper-cpp on
  port 12017). Jenny runs the pickup script on demand.

## Key Drive folder IDs

| Purpose                      | Folder ID |
|------------------------------|-----------|
| Search root (candidate materials) | `1NWsguOnC_ISBPZpe7azxJxlw6QiOMdbQ` |
| Reviewer Recordings root          | `1AznoGEFFBOVc0sOu1wjqWmT0-GDU4-Tb` |

Per-candidate recording subfolders are listed inline in `apps-script/Code.gs`
and `index.html`. Both must stay in sync — if you add or rename a candidate,
update both the `CANDIDATES` array (in `index.html`) and the
`CANDIDATE_FOLDERS` map (in `Code.gs`).

## Privacy

- `<meta name="robots" content="noindex, nofollow">` keeps the page out of
  search engines.
- Soft password gate (set in `index.html` → `CONFIG.PASSWORD`). Not real
  security — it only stops casual link-shares from being usable.
- Recordings go to a private Drive folder; only Matt and people he's
  explicitly shared with can see them.

## See also

- [DEPLOY.md](DEPLOY.md) — one-time setup steps Matt runs in his browser.
- [`jenny-skill/SKILL.md`](jenny-skill/SKILL.md) — how Jenny processes
  reflections after they land in Drive.
