---
name: nextgen-reviewer-pickup
description: Process new reviewer reflections recorded through the Next Gen Director hub. Downloads each new audio file from the Reviewer Recordings Drive folder, transcribes it via Matt's local Whisper server, and writes a sibling .txt file next to the audio so Jenny can synthesize the transcripts into each candidate's notes doc. Trigger when Matt or Jenny asks to "process new reviewer recordings", "pull reviewer reflections", "transcribe candidate reflections", "run the nextgen pickup", or any variant about turning recorded reflections into transcripts.
type: workflow
---

# Process new Next Gen reviewer reflections

This skill turns audio reflections submitted through the Next Gen interview
hub into text transcripts that Jenny can read and consolidate into each
candidate's notes Google Doc.

## When to run

- On demand whenever Jenny is asked to process new reflections.
- After Matt mentions a wave of new reviewer activity.
- Idempotent — re-running is safe. The script skips any audio that already
  has a `.txt` sibling in Drive.

## What it does

1. Lists every audio file under `Reviewer Recordings/<candidate>/` in Drive.
2. Skips audio that already has a matching `.txt` (already processed).
3. Downloads each new audio to `/tmp/`.
4. Sends it to Matt's local Whisper server at `http://127.0.0.1:12017/v1/audio/transcriptions`.
5. Uploads the returned transcript as `<basename>.txt` next to the audio in Drive.
6. Prints a per-candidate summary: how many were transcribed.

The synthesizing step — taking the transcripts and weaving them into each
candidate's "Reviewer Notes" Google Doc — stays human-in-the-loop. That's
Jenny's judgment call. The skill only saves her the transcribing step.

## Pre-flight checks (run these first)

```bash
# 1. Whisper server up?
curl -s -o /dev/null -w "whisper-server: %{http_code}\n" \
  http://127.0.0.1:12017/v1/models

# 2. Drive credentials present?
ls -la ~/.config/dwell-drive-mcp/token.json
```

If Whisper is down, start it:
```bash
whisper-server --model ~/.cache/whisper/ggml-medium.en.bin --port 12017 --host 127.0.0.1
```

## Run

```bash
python3 ~/Documents/Claude/Projects/Building\ Next\ Gen\ Hub/jenny-skill/scripts/process_recordings.py
```

Add `--dry-run` to see what would be processed without actually transcribing
or uploading anything.

## After it runs

Open each candidate's folder under
[Reviewer Recordings](https://drive.google.com/drive/folders/1AznoGEFFBOVc0sOu1wjqWmT0-GDU4-Tb)
to read the new `.txt` files. When you're ready, weave the relevant points
into that candidate's notes doc with attribution (e.g. "Per Drew's reflection
2026-05-12: …").
