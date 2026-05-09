#!/usr/bin/env python3
"""
Process new Next Gen Director reviewer reflections.

Walks the "Reviewer Recordings" folder tree in Drive, finds audio files
that don't yet have a sibling .txt transcript, downloads each, sends it
to Matt's local Whisper server, and uploads the resulting transcript as
a .txt next to the audio.

Idempotent — already-transcribed files are skipped automatically because
their .txt sibling exists in Drive.

Reuses the OAuth credentials installed by dwell-drive-mcp at
~/.config/dwell-drive-mcp/token.json (auto-refreshes via refresh_token).

Whisper endpoint: http://127.0.0.1:12017/v1/audio/transcriptions
Compatible with whisper-server (pfrankov) and any OpenAI-API-compatible
local Whisper server. Start it before running this script.

Usage:
    python3 process_recordings.py            # process everything new
    python3 process_recordings.py --dry-run  # preview only
    python3 process_recordings.py --candidate chris-miller   # one folder
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REVIEWER_RECORDINGS_PARENT_ID = "1AznoGEFFBOVc0sOu1wjqWmT0-GDU4-Tb"
WHISPER_URL = "http://127.0.0.1:12017/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-1"  # whisper-server ignores this, OpenAI-compat needs it
DRIVE_TOKEN_PATH = Path.home() / ".config" / "dwell-drive-mcp" / "token.json"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
AUDIO_EXTS = (".webm", ".m4a", ".mp3", ".mpeg", ".wav", ".ogg")
TMP_DIR = Path("/tmp/nextgen-reviewer")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def load_drive_service():
    """Load Drive credentials from disk, refresh if needed, return service."""
    if not DRIVE_TOKEN_PATH.exists():
        die(f"No Drive token at {DRIVE_TOKEN_PATH}. "
            f"Run dwell-drive-mcp once to authenticate.")
    creds = Credentials.from_authorized_user_file(str(DRIVE_TOKEN_PATH), DRIVE_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            DRIVE_TOKEN_PATH.write_text(creds.to_json())
        else:
            die("Drive credentials are invalid and can't be refreshed. "
                "Re-authenticate via dwell-drive-mcp.")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------

def list_children(service, folder_id: str, page_size: int = 200) -> list[dict]:
    """Return all non-trashed children of a Drive folder."""
    out: list[dict] = []
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType, size)",
            pageSize=page_size,
            pageToken=page_token,
        ).execute()
        out.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def download_file(service, file_id: str, dest_path: Path) -> None:
    """Stream a Drive file's content to dest_path."""
    request = service.files().get_media(fileId=file_id)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with dest_path.open("wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=1 << 20)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def upload_text(service, parent_id: str, name: str, text: str) -> dict:
    """Create a plain-text file in a Drive folder."""
    body = {"name": name, "parents": [parent_id], "mimeType": "text/plain"}
    media = MediaIoBaseUpload(io.BytesIO(text.encode("utf-8")), mimetype="text/plain")
    return service.files().create(body=body, media_body=media, fields="id, name").execute()


# ---------------------------------------------------------------------------
# Whisper
# ---------------------------------------------------------------------------

def transcribe_local_whisper(audio_path: Path) -> str:
    """POST to the local Whisper server, return transcript text."""
    with audio_path.open("rb") as f:
        resp = requests.post(
            WHISPER_URL,
            files={"file": (audio_path.name, f, "audio/webm")},
            data={"model": WHISPER_MODEL, "response_format": "json"},
            timeout=600,  # transcription can take a while for long recordings
        )
    if not resp.ok:
        raise RuntimeError(f"Whisper {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    text = payload.get("text", "")
    return text.strip()


# ---------------------------------------------------------------------------
# Core workflow
# ---------------------------------------------------------------------------

def is_audio(name: str) -> bool:
    return name.lower().endswith(AUDIO_EXTS)

def basename_no_ext(name: str) -> str:
    return re.sub(r"\.[^.]+$", "", name)


def process_candidate_folder(service, candidate_folder: dict, dry_run: bool) -> dict:
    """Transcribe every new audio file in one candidate's recordings folder.

    Returns {"name": str, "new": int, "skipped": int, "errors": list[str]}.
    """
    children = list_children(service, candidate_folder["id"])
    audio_files = [c for c in children if is_audio(c["name"])]
    txt_basenames = {basename_no_ext(c["name"]) for c in children
                     if c["name"].lower().endswith(".txt")}

    new_count = 0
    skipped = 0
    errors: list[str] = []

    for audio in audio_files:
        base = basename_no_ext(audio["name"])
        if base in txt_basenames:
            skipped += 1
            continue

        print(f"  → {audio['name']}", flush=True)
        if dry_run:
            new_count += 1
            continue

        try:
            local = TMP_DIR / candidate_folder["name"] / audio["name"]
            download_file(service, audio["id"], local)
            transcript = transcribe_local_whisper(local)
            if not transcript:
                transcript = "(empty transcript — Whisper returned no text)"
            upload_text(service, candidate_folder["id"], f"{base}.txt", transcript)
            new_count += 1
            print(f"      transcribed ({len(transcript)} chars)", flush=True)
        except Exception as e:
            errors.append(f"{audio['name']}: {e}")
            print(f"      FAILED: {e}", flush=True)
        finally:
            try:
                local.unlink(missing_ok=True)
            except Exception:
                pass

    return {
        "name": candidate_folder["name"],
        "new": new_count,
        "skipped": skipped,
        "errors": errors,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't download, transcribe, or upload — just report.")
    ap.add_argument("--candidate", default=None,
                    help="Only process this candidate slug (e.g. chris-miller).")
    args = ap.parse_args()

    # Pre-flight: Whisper alive?
    if not args.dry_run:
        try:
            r = requests.get("http://127.0.0.1:12017/v1/models", timeout=2)
            if not r.ok:
                die(f"Whisper server returned {r.status_code}. Start it before running.")
        except requests.RequestException as e:
            die(f"Can't reach Whisper at 127.0.0.1:12017 — start whisper-server first. ({e})")

    print("Loading Drive service…", flush=True)
    svc = load_drive_service()

    print("Listing candidate folders…", flush=True)
    candidate_folders = [
        c for c in list_children(svc, REVIEWER_RECORDINGS_PARENT_ID)
        if c["mimeType"] == "application/vnd.google-apps.folder"
    ]
    if args.candidate:
        candidate_folders = [c for c in candidate_folders
                             if slugify(c["name"]) == args.candidate]
        if not candidate_folders:
            die(f"No candidate folder matched slug {args.candidate}")

    summaries = []
    for folder in sorted(candidate_folders, key=lambda c: c["name"]):
        print(f"\n{folder['name']}", flush=True)
        summaries.append(process_candidate_folder(svc, folder, args.dry_run))

    # Report
    print("\n" + "=" * 60)
    total_new = sum(s["new"] for s in summaries)
    total_err = sum(len(s["errors"]) for s in summaries)
    print(f"  {total_new} new transcript{'' if total_new == 1 else 's'}, "
          f"{total_err} error{'' if total_err == 1 else 's'}"
          f"{' (DRY RUN)' if args.dry_run else ''}")
    for s in summaries:
        if s["new"] or s["errors"]:
            print(f"  • {s['name']}: {s['new']} new"
                  + (f", {len(s['errors'])} err" if s["errors"] else ""))
            for err in s["errors"]:
                print(f"      ✗ {err}")
    print("=" * 60)


def slugify(name: str) -> str:
    """Match the slug logic the page uses (best-effort)."""
    s = name.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s


def die(msg: str):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
