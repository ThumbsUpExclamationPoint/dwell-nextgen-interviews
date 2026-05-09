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
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
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
# whisper.cpp's server (brew install whisper-cpp) exposes /inference. The
# pfrankov/whisper-server fork exposes /v1/audio/transcriptions. Matt is
# running the brew flavor, so we point at /inference here. Keep both URLs
# documented so future-us knows the trade-off.
WHISPER_URL = "http://127.0.0.1:12017/inference"
DRIVE_TOKEN_PATH = Path.home() / ".config" / "dwell-drive-mcp" / "token.json"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
AUDIO_EXTS = (".webm", ".m4a", ".mp3", ".mpeg", ".wav", ".ogg")
TMP_DIR = Path("/tmp/nextgen-reviewer")

# Apps Script web app endpoint that handles audio uploads (from the page)
# AND transcript appends (from this script). See apps-script/Code.gs.
APPS_SCRIPT_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbwR5S1t1Fm-R56bHShMraOn5BOEC5EgR4htd7fOuTT8UDdtQdJZ2_cqrfXRKISBS6pG/exec"
)

# Claude API — Anthropic. Cleanup uses Haiku (fast, cheap, mechanical task);
# synthesis uses Opus (best judgment for stitching themes across reviewers
# in Jenny's voice). Key lives at ~/.config/dwell/anthropic_key.
ANTHROPIC_KEY_PATH = Path.home() / ".config" / "dwell" / "anthropic_key"
ANTHROPIC_API_URL  = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION  = "2023-06-01"
CLEANUP_MODEL      = "claude-haiku-4-5"
SYNTHESIS_MODEL    = "claude-opus-4-6"

# First line of every .txt sidecar marks whether the transcript was
# successfully appended to the candidate's Reviewer Reflections Doc.
# Two possible states:
#   "# pending-append"          → transcribe succeeded, append failed (retry next run)
#   "# appended-to-doc: <ISO>"  → fully done
APPEND_OK_PREFIX = "# appended-to-doc: "
APPEND_PENDING_MARKER = "# pending-append"


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
            fields="nextPageToken, files(id, name, mimeType, size, createdTime)",
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


def update_text_file(service, file_id: str, text: str) -> None:
    """Replace a plain-text file's contents."""
    media = MediaIoBaseUpload(io.BytesIO(text.encode("utf-8")), mimetype="text/plain")
    service.files().update(fileId=file_id, media_body=media).execute()


def find_txt_sidecar(children: list[dict], audio_basename: str) -> dict | None:
    """Return the .txt sibling of an audio file (by basename), if any."""
    target = audio_basename + ".txt"
    for c in children:
        if c["name"] == target:
            return c
    return None


def read_drive_text(service, file_id: str) -> str:
    """Read a plain-text Drive file's content."""
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=1 << 20)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Whisper
# ---------------------------------------------------------------------------

def transcribe_local_whisper(audio_path: Path) -> str:
    """POST to the local Whisper server, return transcript text.

    whisper.cpp's /inference can't decode webm/Opus directly — it expects
    PCM WAV. So we convert via ffmpeg first (16kHz mono, whisper.cpp's
    native sample rate), then send the WAV.
    """
    wav_path = ensure_wav(audio_path)
    cleanup_wav = (wav_path != audio_path)
    try:
        with wav_path.open("rb") as f:
            resp = requests.post(
                WHISPER_URL,
                files={"file": (wav_path.name, f, "audio/wav")},
                data={
                    "response_format": "json",
                    "temperature": "0",
                },
                timeout=600,
            )
    finally:
        if cleanup_wav:
            try:
                wav_path.unlink(missing_ok=True)
            except Exception:
                pass

    if not resp.ok:
        raise RuntimeError(f"Whisper {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    # /inference returns {"text": "..."} but older builds return just a
    # string in the body. Handle both shapes.
    if isinstance(payload, dict):
        return (payload.get("text") or "").strip()
    return str(payload).strip()


def ensure_wav(audio_path: Path) -> Path:
    """Return a path to a 16kHz mono WAV version of the audio. If the
    source is already a WAV, returns the source unchanged. Otherwise
    runs ffmpeg to convert to a sibling .wav file."""
    if audio_path.suffix.lower() == ".wav":
        return audio_path
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found — install with: brew install ffmpeg")
    wav_path = audio_path.with_suffix(".wav")
    subprocess.run(
        [
            ffmpeg, "-y",
            "-i", str(audio_path),
            "-ar", "16000",  # 16kHz
            "-ac", "1",      # mono
            "-c:a", "pcm_s16le",
            "-loglevel", "error",
            str(wav_path),
        ],
        check=True,
    )
    return wav_path


def find_ffmpeg() -> str | None:
    """Locate the ffmpeg binary, tolerating LaunchAgent's stripped PATH.

    LaunchAgents on macOS run with a minimal PATH (/usr/bin:/bin:...),
    which doesn't include Homebrew's bin dir. shutil.which() returns
    None in that context even when ffmpeg is installed. Fall back to
    the two standard Homebrew locations so this works under both the
    user's interactive shell and the LaunchAgent.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if Path(candidate).exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Claude — cleanup + synthesis
# ---------------------------------------------------------------------------

def load_anthropic_key() -> str | None:
    """Read the Anthropic API key from ~/.config/dwell/anthropic_key,
    falling back to the ANTHROPIC_API_KEY env var. Returns None if neither
    is set — the caller decides whether to fail or skip."""
    if ANTHROPIC_KEY_PATH.exists():
        key = ANTHROPIC_KEY_PATH.read_text().strip()
        if key:
            return key
    return os.environ.get("ANTHROPIC_API_KEY") or None


def call_claude(api_key: str, model: str, system: str, user: str,
                max_tokens: int = 4096) -> str:
    """Single-turn Anthropic Messages API call. Returns the assistant's
    text. Raises RuntimeError on non-2xx response."""
    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=120,
    )
    if not resp.ok:
        raise RuntimeError(f"Anthropic {resp.status_code}: {resp.text[:400]}")
    payload = resp.json()
    parts = payload.get("content", [])
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return text.strip()


CLEANUP_SYSTEM = (
    "You clean up Whisper-generated voice transcripts. Your only job is to "
    "fix mechanical errors so the text reads as natural prose:\n"
    "  • Add missing punctuation and capitalization.\n"
    "  • Fix obvious homophones and misheard words from context.\n"
    "  • Remove pure filler ('um', 'uh', 'like', 'you know') unless it's load-bearing.\n"
    "  • Repair sentence boundaries Whisper smashed together.\n"
    "Do NOT paraphrase, summarize, reorganize, or add content. Preserve the speaker's\n"
    "voice, hedges, repetitions for emphasis, and order of thought. If the speaker\n"
    "self-corrects mid-sentence, keep the corrected version.\n"
    "Return only the cleaned text — no preamble, no quotes, no markdown."
)


def cleanup_transcript(api_key: str, raw: str) -> str:
    """Send a raw Whisper transcript through Claude Haiku for cleanup.
    Returns the cleaned text. On failure, returns the raw input."""
    if not raw.strip():
        return raw
    try:
        return call_claude(api_key, CLEANUP_MODEL, CLEANUP_SYSTEM, raw,
                           max_tokens=2048)
    except Exception as e:
        print(f"      cleanup failed, using raw transcript: {e}", flush=True)
        return raw


SYNTHESIS_SYSTEM = """You are Jenny, Chief of Staff to Matt Stephan at Dwell Church of the Peninsula. You are writing a synthesis of voice reflections from the search team about a candidate for Next Gen Director.

Voice rules:
- Dry, not formulaic. Short sentences over long ones. No throat-clearing, no recap of the method, no "great question," no "here's a summary."
- Lead with the finding, not the method.
- Name the actual risk or next move in plain language.
- Wry is OK. A bit is not. Drop dryness for genuinely heavy content.
- One sincere note per section is plenty; two is sycophancy.
- No exclamation points outside quotes.
- Don't sign — this is an internal tracking note, not correspondence.

Output format — markdown-ish, parsed by an Apps Script that recognizes:
- "## " prefix → Heading 2
- "[ ... ]" wrapped line → italic-grey meta line
- "- " prefix → bullet point
- everything else → body paragraph

Use exactly this structure:

[Auto-synthesized — last updated <DATE PROVIDED IN USER MESSAGE>. Regenerated each pickup. Edit below the marker if you want changes preserved.]

## Themes & convergences

(bullets — what reviewers landed on independently, with attribution: "(3 of 5 reviewers)" or similar where it strengthens the point. Quote a tight phrase if one is striking.)

## Concerns & open questions

(bullets — risks named, conflicts between reviewers, things they wanted to know more about. Be honest about disagreement; don't smooth it over.)

## Recommendation lean

(short paragraph — where the team is leaning and how strong the consensus is. If it's split, say split. If it's unanimous, say so.)

## Worth re-listening to

(bullets — 2-4 quotes that punch above their weight, with reviewer-name attribution if reviewers identified themselves in their recording. Cite the source filename in parens.)

If there's only one reflection so far, say so plainly in a single body paragraph and skip the four-section structure. Don't fabricate themes from a single voice.
"""


def synthesize_reflections(api_key: str, candidate_name: str,
                           reflections: list[dict], generated_at: str) -> str:
    """reflections: list of {filename, timestamp, transcript}. Returns
    the synthesis as markdown-ish text matching SYNTHESIS_SYSTEM's spec."""
    if not reflections:
        return ""

    blocks = []
    for r in reflections:
        ts = r.get("timestamp", "(unknown time)")
        fn = r.get("filename", "(unknown file)")
        blocks.append(f"### Reflection — {ts}\nSource: {fn}\n\n{r['transcript']}")
    body = "\n\n---\n\n".join(blocks)

    user = (
        f"Candidate: {candidate_name}\n"
        f"Synthesis date (use this in the meta line): {generated_at}\n"
        f"Number of reflections to synthesize: {len(reflections)}\n\n"
        f"--- REFLECTIONS BELOW ---\n\n{body}"
    )
    return call_claude(api_key, SYNTHESIS_MODEL, SYNTHESIS_SYSTEM, user,
                       max_tokens=4096)


def update_synthesis_in_doc(*, candidate_id: str, candidate_name: str,
                            synthesis_md: str, generated_at: str) -> None:
    """POST the new synthesis to Apps Script's /update_synthesis route."""
    resp = requests.post(
        APPS_SCRIPT_URL,
        data={
            "action": "update_synthesis",
            "candidate_id": candidate_id,
            "candidate_name": candidate_name,
            "synthesis_md": synthesis_md,
            "generated_at": generated_at,
        },
        timeout=60,
    )
    body = resp.text.strip()
    if not resp.ok or not body.startswith("ok"):
        raise RuntimeError(f"update_synthesis failed ({resp.status_code}): {body[:300]}")


def append_transcript_to_doc(*, candidate_id: str, candidate_name: str,
                             transcript: str, source_filename: str,
                             timestamp: str) -> None:
    """POST to the Apps Script /append route. Raises on non-ok response."""
    resp = requests.post(
        APPS_SCRIPT_URL,
        data={
            "action": "append_transcript",
            "candidate_id": candidate_id,
            "candidate_name": candidate_name,
            "transcript": transcript,
            "source_filename": source_filename,
            "timestamp": timestamp,
        },
        timeout=60,
        # Apps Script web apps do a 302 → script.googleusercontent.com.
        # requests follows redirects by default, which is what we want.
    )
    body = resp.text.strip()
    if not resp.ok or not body.startswith("ok"):
        raise RuntimeError(f"append failed ({resp.status_code}): {body[:300]}")


def slug_from_filename(name: str, fallback_folder_name: str) -> str:
    """Filenames are formatted as `<timestamp>__<slug>.<ext>`. Pull the slug."""
    base = re.sub(r"\.[^.]+$", "", name)
    if "__" in base:
        return base.rsplit("__", 1)[1]
    return slugify(fallback_folder_name)


# ---------------------------------------------------------------------------
# Core workflow
# ---------------------------------------------------------------------------

def is_audio(name: str) -> bool:
    return name.lower().endswith(AUDIO_EXTS)

def basename_no_ext(name: str) -> str:
    return re.sub(r"\.[^.]+$", "", name)


def process_candidate_folder(service, candidate_folder: dict,
                             dry_run: bool, anthropic_key: str | None) -> dict:
    """Process every audio file in one candidate's recordings folder.

    Per audio:
      1. Whisper transcript (raw)
      2. Claude Haiku cleanup (cleaned)
      3. Append cleaned transcript to candidate's Reviewer Reflections Doc
      4. Write a .txt sidecar in Drive whose first line is a state marker:
         "# appended-to-doc: <ISO>" → fully done, skip future runs
         "# pending-append"         → transcribed but append failed; retry
                                       only the append (no re-transcription)

    After all audio for this candidate is processed, if any new transcript
    was appended, regenerate the synthesis section at the top of the Doc
    by reading every .txt sidecar in Drive (the source of truth for cleaned
    transcripts) and sending them to Claude Opus.

    Returns {"name", "new", "appended", "synthesized", "skipped", "errors"}.
    """
    children = list_children(service, candidate_folder["id"])
    audio_files = [c for c in children if is_audio(c["name"])]
    txt_by_base: dict[str, dict] = {}
    for c in children:
        n = c["name"]
        if n.lower().endswith(".txt"):
            txt_by_base[basename_no_ext(n)] = c

    new_count = 0
    appended_count = 0
    synthesized = 0
    skipped = 0
    errors: list[str] = []
    candidate_id_seen: str | None = None

    for audio in audio_files:
        base = basename_no_ext(audio["name"])
        candidate_id = slug_from_filename(audio["name"], candidate_folder["name"])
        candidate_id_seen = candidate_id
        timestamp = audio.get("createdTime") or datetime_from_filename(audio["name"])

        existing_txt = txt_by_base.get(base)
        if existing_txt and not dry_run:
            content = read_drive_text(service, existing_txt["id"])
            first_line = content.splitlines()[0] if content else ""
            if first_line.startswith(APPEND_OK_PREFIX):
                skipped += 1
                continue
            if first_line.startswith(APPEND_PENDING_MARKER):
                # Transcript exists; only need to retry the append.
                print(f"  → {audio['name']} (append-retry)", flush=True)
                transcript = "\n".join(content.splitlines()[1:]).strip()
                try:
                    append_transcript_to_doc(
                        candidate_id=candidate_id,
                        candidate_name=candidate_folder["name"],
                        transcript=transcript,
                        source_filename=audio["name"],
                        timestamp=timestamp,
                    )
                    update_text_file(service, existing_txt["id"],
                                     mark_appended(transcript))
                    appended_count += 1
                    print("      appended on retry", flush=True)
                except Exception as e:
                    errors.append(f"{audio['name']}: {e}")
                    print(f"      append-retry FAILED: {e}", flush=True)
                continue
            # Sidecar exists but lacks any marker — old format. Treat as done.
            skipped += 1
            continue

        print(f"  → {audio['name']}", flush=True)
        if dry_run:
            new_count += 1
            continue

        local = TMP_DIR / candidate_folder["name"] / audio["name"]
        try:
            download_file(service, audio["id"], local)
            raw_transcript = transcribe_local_whisper(local)
            if not raw_transcript:
                raw_transcript = "(empty transcript — Whisper returned no text)"
            print(f"      transcribed ({len(raw_transcript)} chars)", flush=True)

            # Cleanup pass — Haiku fixes punctuation, fillers, homophones
            # without paraphrasing or summarizing.
            if anthropic_key:
                cleaned = cleanup_transcript(anthropic_key, raw_transcript)
                if cleaned != raw_transcript:
                    print(f"      cleaned    ({len(cleaned)} chars)", flush=True)
            else:
                cleaned = raw_transcript

            # Append cleaned transcript to the doc. On failure, write
            # pending-marker .txt so we retry the append next run without
            # re-transcribing or re-cleaning.
            try:
                append_transcript_to_doc(
                    candidate_id=candidate_id,
                    candidate_name=candidate_folder["name"],
                    transcript=cleaned,
                    source_filename=audio["name"],
                    timestamp=timestamp,
                )
                upload_text(service, candidate_folder["id"], f"{base}.txt",
                            mark_appended(cleaned))
                new_count += 1
                appended_count += 1
                print("      appended to doc", flush=True)
            except Exception as e:
                upload_text(service, candidate_folder["id"], f"{base}.txt",
                            mark_pending(cleaned))
                errors.append(f"{audio['name']}: append failed: {e}")
                print(f"      transcribed but append FAILED: {e}", flush=True)
        except Exception as e:
            errors.append(f"{audio['name']}: {e}")
            print(f"      FAILED: {e}", flush=True)
        finally:
            try:
                local.unlink(missing_ok=True)
            except Exception:
                pass

    # Synthesis pass — only run if we appended something new this round
    # AND we have an Anthropic key. Reads every .txt sidecar (source of
    # truth for cleaned transcripts) and asks Claude Opus to write a
    # Jenny-voice synthesis. Sends the result to /update_synthesis which
    # rewrites the synthesis section at the top of the Doc.
    if appended_count > 0 and anthropic_key and candidate_id_seen and not dry_run:
        try:
            reflections = gather_reflections_for_synthesis(
                service, candidate_folder, audio_files
            )
            generated_at = datetime.now(timezone.utc).isoformat()
            print(f"      synthesizing {len(reflections)} reflection(s)…", flush=True)
            synthesis_md = synthesize_reflections(
                anthropic_key, candidate_folder["name"], reflections, generated_at
            )
            update_synthesis_in_doc(
                candidate_id=candidate_id_seen,
                candidate_name=candidate_folder["name"],
                synthesis_md=synthesis_md,
                generated_at=generated_at,
            )
            synthesized += 1
            print(f"      synthesis updated", flush=True)
        except Exception as e:
            errors.append(f"synthesis: {e}")
            print(f"      SYNTHESIS FAILED: {e}", flush=True)

    return {
        "name": candidate_folder["name"],
        "new": new_count,
        "appended": appended_count,
        "synthesized": synthesized,
        "skipped": skipped,
        "errors": errors,
    }


def gather_reflections_for_synthesis(service, candidate_folder: dict,
                                     audio_files: list[dict]) -> list[dict]:
    """Pull every .txt sidecar in the candidate's recordings folder, strip
    the marker line off, and return [{filename, timestamp, transcript}, ...]
    sorted by submission timestamp. Used as input to the synthesis LLM."""
    children = list_children(service, candidate_folder["id"])
    txt_by_base = {
        basename_no_ext(c["name"]): c for c in children
        if c["name"].lower().endswith(".txt")
    }
    audio_by_base = {basename_no_ext(a["name"]): a for a in audio_files}

    reflections: list[dict] = []
    for base, txt_meta in txt_by_base.items():
        body = read_drive_text(service, txt_meta["id"])
        lines = body.splitlines()
        # Drop the marker line if present
        if lines and (lines[0].startswith(APPEND_OK_PREFIX)
                      or lines[0].startswith(APPEND_PENDING_MARKER)):
            transcript = "\n".join(lines[1:]).strip()
        else:
            transcript = body.strip()
        if not transcript:
            continue
        audio = audio_by_base.get(base)
        ts = (audio or {}).get("createdTime") or datetime_from_filename(base + ".webm")
        reflections.append({
            "filename": base + ".webm",
            "timestamp": ts,
            "transcript": transcript,
        })
    reflections.sort(key=lambda r: r["timestamp"])
    return reflections


def mark_appended(transcript: str) -> str:
    return f"{APPEND_OK_PREFIX}{datetime.now(timezone.utc).isoformat()}\n{transcript}"


def mark_pending(transcript: str) -> str:
    return f"{APPEND_PENDING_MARKER}\n{transcript}"


def datetime_from_filename(name: str) -> str:
    """Audio filenames look like '2026-05-08T19-30-12-345Z__slug.webm'.
    Convert the timestamp portion back to a real ISO 8601 string. Falls
    back to current time if it can't parse."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z)__", name)
    if not m:
        return datetime.now(timezone.utc).isoformat()
    raw = m.group(1)
    # Restore : in time portion and . before milliseconds.
    iso = re.sub(r"T(\d{2})-(\d{2})-(\d{2})-(\d{3})Z", r"T\1:\2:\3.\4Z", raw)
    return iso


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't download, transcribe, or upload — just report.")
    ap.add_argument("--candidate", default=None,
                    help="Only process this candidate slug (e.g. chris-miller).")
    args = ap.parse_args()

    # Pre-flight: Whisper alive?
    # We don't care which endpoint responds — any HTTP response (even 404)
    # proves the server is running. whisper.cpp's built-in server doesn't
    # expose /v1/models but DOES expose /v1/audio/transcriptions, which is
    # the only endpoint we actually use. So we treat "any response" as up.
    if not args.dry_run:
        try:
            requests.get("http://127.0.0.1:12017/", timeout=2)
        except requests.RequestException as e:
            die(f"Can't reach Whisper at 127.0.0.1:12017 — start whisper-server first. ({e})")

    print("Loading Drive service…", flush=True)
    svc = load_drive_service()

    # Anthropic key — optional. If missing, the script still does Whisper +
    # append, just skips cleanup and synthesis.
    anthropic_key = load_anthropic_key()
    if anthropic_key:
        print("✓ Anthropic key loaded — cleanup + synthesis enabled.", flush=True)
    else:
        print("⚠ No Anthropic key found — skipping cleanup + synthesis. "
              "Stash one at ~/.config/dwell/anthropic_key.", flush=True)

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
        summaries.append(
            process_candidate_folder(svc, folder, args.dry_run, anthropic_key)
        )

    # Report
    print("\n" + "=" * 60)
    total_new       = sum(s["new"] for s in summaries)
    total_appended  = sum(s.get("appended", 0) for s in summaries)
    total_synthd    = sum(s.get("synthesized", 0) for s in summaries)
    total_err       = sum(len(s["errors"]) for s in summaries)
    print(f"  {total_new} new transcript{'' if total_new == 1 else 's'}, "
          f"{total_appended} doc append{'' if total_appended == 1 else 's'}, "
          f"{total_synthd} synthesis update{'' if total_synthd == 1 else 's'}, "
          f"{total_err} error{'' if total_err == 1 else 's'}"
          f"{' (DRY RUN)' if args.dry_run else ''}")
    for s in summaries:
        if s["new"] or s.get("appended") or s.get("synthesized") or s["errors"]:
            line = f"  • {s['name']}: {s['new']} new"
            if s.get("appended"):
                line += f", {s['appended']} appended"
            if s.get("synthesized"):
                line += f", synthesis updated"
            if s["errors"]:
                line += f", {len(s['errors'])} err"
            print(line)
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
