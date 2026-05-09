# Handoff — what's done, what's pending

_Last updated: 2026-05-08_

## Done (autonomous)

- ✅ Drive folder structure: `Reviewer Recordings/<11 candidates>/`
  ([open](https://drive.google.com/drive/folders/1AznoGEFFBOVc0sOu1wjqWmT0-GDU4-Tb))
- ✅ Static reviewer page (`index.html`) — soft password gate, mic recording per
  candidate, base64 upload, mobile-friendly
- ✅ Apps Script backend (`apps-script/Code.gs`) — receives uploads, decodes
  base64, drops file in correct candidate folder
- ✅ Jenny pickup-and-transcribe skill (`jenny-skill/`) — finds new audio in
  Drive, transcribes via local Whisper, uploads sibling `.txt` for Jenny to
  consolidate into each candidate's notes doc
- ✅ Local git repo built and committed; remote URL pre-configured with
  Matt's GitHub token

## Pending — Matt's 5-min part

These three steps need a real human in front of github.com and
script.google.com. Walk-through is in [DEPLOY.md](DEPLOY.md).

1. **Create empty GitHub repo** at <https://github.com/new>:
   - Name: `dwell-nextgen-interviews`
   - Owner: `thumbsupexclamationpoint`
   - Public, **uninitialized** (no README, no .gitignore, no license)
2. **Push the code** — easiest path: just tell Obi (or Jenny) the repo
   exists. Obi already has a clean local clone with everything committed
   and the remote pre-configured with Matt's token; one `git push` from the
   sandbox sends the whole tree. (The `.git` folder in this user-facing
   project has stuck locks from sandbox permission quirks — ignore it,
   don't rely on it for the push.)
3. **Enable GitHub Pages**: repo Settings → Pages → Source: deploy from
   branch → main, root → Save.
4. **Deploy the Apps Script** ([DEPLOY.md step 2](DEPLOY.md#step-2--deploy-the-apps-script-backend-3-minutes)).
5. **Send Obi (or Jenny) the `/exec` URL** — Obi wires it into `index.html`
   and pushes the update.

## SMS-friendly text Jenny can send Matt

> Hub is built. 5 min of your time when you're back:
> 1. Create empty repo `thumbsupexclamationpoint/dwell-nextgen-interviews`
>    on github.com (public, no README/license)
> 2. Deploy Apps Script — paste `apps-script/Code.gs` at script.google.com,
>    deploy as web app (Execute: Me, Access: Anyone), copy the `/exec` URL
> 3. Reply with the `/exec` URL — I'll wire it into the page
> Files: ~/Documents/Claude/Projects/Building Next Gen Hub/
> Walkthrough: that folder's DEPLOY.md
