/**
 * Dwell Next Gen Director — reviewer reflection ingest
 *
 * Apps Script web app that receives base64-encoded audio uploads from the
 * static reviewer page (https://thumbsupexclamationpoint.github.io/dwell-nextgen-interviews/),
 * decodes them, and saves the audio file to the matching candidate's
 * subfolder under "Reviewer Recordings/" in Google Drive.
 *
 * Deploy:
 *   1. Open https://script.google.com → New project → paste this file as Code.gs
 *   2. Deploy → New deployment → type: Web app
 *      Execute as: Me (matt@dwellpeninsula.com)
 *      Who has access: Anyone
 *   3. Copy the /macros/s/.../exec URL into index.html → CONFIG.APPS_SCRIPT_URL
 *
 * The script runs as Matt's Google account, so it inherits write access to
 * any Drive folder Matt owns or has been shared on. No service-account
 * setup needed.
 */

// -----------------------------------------------------------------------
// Configuration
// -----------------------------------------------------------------------
//
// Source of truth for the candidate list = subfolders of SEARCH_ROOT_ID.
// Each subfolder is one candidate (folder name = display name, slug
// derived deterministically from the name). Recordings live as parallel
// subfolders under REVIEWER_RECORDINGS_ROOT_ID; missing recording folders
// are auto-created on first upload.
//
// Workflow for adding/removing candidates:
//   • Add:    create a new folder in SEARCH_ROOT_ID, drop materials in.
//   • Remove: right-click the folder → Move to Trash.
// The hub picks up the change on next page refresh (cache TTL = 5 min).
// -----------------------------------------------------------------------

const SEARCH_ROOT_ID              = "1NWsguOnC_ISBPZpe7azxJxlw6QiOMdbQ";
const REVIEWER_RECORDINGS_ROOT_ID = "1AznoGEFFBOVc0sOu1wjqWmT0-GDU4-Tb";

// Folders inside SEARCH_ROOT_ID with these names are NOT candidates —
// they're organizational. Add to this list if you ever stash drafts,
// archives, etc. inside the search root.
const SKIP_FOLDER_NAMES = ["Reviewer Recordings"];

// The single Doc filename used per candidate. If a Doc with this name
// doesn't exist in the candidate's materials folder, the /append route
// creates one; otherwise it appends to the existing Doc.
const REFLECTIONS_DOC_NAME = "Reviewer Reflections";

// PropertiesService cache TTL for the slug→folders resolution map.
// 5 minutes balances "page loads are fast" against "new candidates
// appear quickly without a hard refresh."
const CANDIDATE_CACHE_TTL_SEC = 300;
const CANDIDATE_CACHE_KEY     = "candidates_v1";

// Optional: also write a row to a Drive sheet for an audit trail. Set this
// to the file ID of a Google Sheet to enable, or leave empty to skip.
const AUDIT_SHEET_ID = "";

// -----------------------------------------------------------------------
// HTTP handlers
// -----------------------------------------------------------------------

/**
 * Run this function manually (▶ Run button) once after pasting in this
 * version of the code. It exists so Apps Script's permission scanner
 * sees every API we touch (Drive AND DocumentApp) and includes both
 * scopes in the authorization prompt. Without this, the /append route
 * fails at runtime with "You do not have permission to call
 * DocumentApp.create" because the deployed scopes were captured before
 * DocumentApp was added.
 *
 * Steps:
 *   1. Function dropdown (top toolbar) → select "authorize"
 *   2. Click ▶ Run
 *   3. Approve the auth dialog (it will ask for Drive + Documents)
 *   4. Then redeploy: Deploy → Manage deployments → ✏️ Edit →
 *      Version: New version → Deploy
 */
function authorize() {
  // Touch every API the doPost routes need so the auth scanner
  // catches them all at once.
  DriveApp.getRootFolder();
  const tempDoc = DocumentApp.create("__nextgen-authorize-test__");
  DriveApp.getFileById(tempDoc.getId()).setTrashed(true);
  console.log("Authorization complete. Now redeploy as a new version.");
}

/**
 * GET — two routes:
 *
 *   ?action=candidates&callback=fnName
 *     → JSONP. Returns the live candidate list discovered from
 *       SEARCH_ROOT_ID. The page uses this on load to render cards
 *       dynamically. JSONP because Apps Script web apps don't set CORS
 *       headers and the page needs to *read* the response.
 *
 *   (no params)
 *     → plain-text health check.
 */
function doGet(e) {
  const p = (e && e.parameter) || {};

  if (p.action === "candidates") {
    const list = listCandidates();
    if (p.callback) {
      const callback = String(p.callback).replace(/[^A-Za-z0-9_$.]/g, "");
      return ContentService
        .createTextOutput(callback + "(" + JSON.stringify(list) + ")")
        .setMimeType(ContentService.MimeType.JAVASCRIPT);
    }
    return ContentService
      .createTextOutput(JSON.stringify(list))
      .setMimeType(ContentService.MimeType.JSON);
  }

  return ContentService
    .createTextOutput("Dwell Next Gen reviewer ingest — alive.\n" +
                      "GET ?action=candidates&callback=fn for the live list.\n" +
                      "POST audio_b64, candidate_id, mime, filename to upload.")
    .setMimeType(ContentService.MimeType.TEXT);
}

/**
 * POST — two routes, distinguished by which fields are present:
 *
 * Route A (audio upload, called by the reviewer page):
 *   candidate_id    : slug, must be a key in CANDIDATE_FOLDERS
 *   candidate_name  : display name (logged for debugging only)
 *   filename        : suggested file name (e.g. 2026-05-08T19-30-12-345Z__chris-miller.webm)
 *   mime            : MIME type of the audio
 *   audio_b64       : base64-encoded audio bytes
 *
 * Route B (transcript append, called by Jenny's pickup script after
 *  local Whisper finishes — adds a new section to the candidate's
 *  "Reviewer Reflections" Google Doc):
 *   action          : "append_transcript"  (required; disambiguates routes)
 *   candidate_id    : slug, must be a key in CANDIDATE_MATERIAL_FOLDERS
 *   candidate_name  : display name (used as Doc title prefix when creating)
 *   transcript      : plain text transcript
 *   source_filename : the audio's filename (cited in the Doc section header)
 *   timestamp       : ISO timestamp of when the recording was uploaded
 *
 * Returns plain text "ok: <id>" on success, "err: <reason>" on failure.
 */
function doPost(e) {
  try {
    const p = (e && e.parameter) || {};
    if (p.action === "update_synthesis") {
      return handleUpdateSynthesis(p);
    }
    if (p.action === "append_transcript" || p.transcript) {
      return handleAppendTranscript(p);
    }
    return handleAudioUpload(p);
  } catch (err) {
    console.error(err);
    return textResponse("err: " + (err && err.message ? err.message : err));
  }
}

function handleAudioUpload(p) {
  const candidateId = p.candidate_id;
  const audioB64    = p.audio_b64;
  const filename    = p.filename || ("recording-" + Date.now() + ".webm");
  const mime        = p.mime || "audio/webm";
  const candidateName = p.candidate_name || candidateId || "(unknown)";

  if (!candidateId)  return textResponse("err: missing candidate_id");
  if (!audioB64)     return textResponse("err: missing audio_b64");

  const candidate = resolveCandidate(candidateId);
  if (!candidate) return textResponse("err: unknown candidate_id " + candidateId);

  const bytes = Utilities.base64Decode(audioB64);
  const blob  = Utilities.newBlob(bytes, mime, filename);
  const folder = DriveApp.getFolderById(candidate.recordingsId);
  const file = folder.createFile(blob);

  if (AUDIT_SHEET_ID) {
    try {
      const sheet = SpreadsheetApp.openById(AUDIT_SHEET_ID).getSheets()[0];
      sheet.appendRow([
        new Date(), candidateId, candidateName, filename, mime,
        bytes.length, file.getUrl(),
      ]);
    } catch (err) {
      console.warn("audit sheet write failed: " + err);
    }
  }

  console.log("saved " + filename + " (" + bytes.length + " bytes) → " + file.getUrl());
  return textResponse("ok: " + file.getId());
}

/**
 * Find or create the candidate's "Reviewer Reflections" Doc and append
 * a new transcript section. Each section is structured as:
 *
 *     Reflection — <human-readable date>           (Heading 2)
 *     Source: <source_filename>                    (italic, secondary)
 *     <transcript>                                 (body paragraph)
 *     [blank line]
 *
 * The Doc is created lazily if missing (good for first-run candidates).
 * Multiple appends to the same Doc accumulate naturally — every reflection
 * lands at the bottom in chronological order of pickup.
 */
function handleAppendTranscript(p) {
  const candidateId   = p.candidate_id;
  const transcript    = (p.transcript || "").trim();
  const sourceFilename = p.source_filename || "(unknown)";
  const timestamp     = p.timestamp || new Date().toISOString();
  const candidateName = p.candidate_name || candidateId || "(unknown)";

  if (!candidateId)  return textResponse("err: missing candidate_id");
  if (!transcript)   return textResponse("err: missing transcript");

  const candidate = resolveCandidate(candidateId);
  if (!candidate) return textResponse("err: unknown candidate_id " + candidateId);

  const doc = findOrCreateReflectionsDoc(candidate.materialsId, candidateName);
  const body = doc.getBody();

  // Format the human-readable date in Pacific time. Apps Script's
  // formatDate honors the script's timezone; we set the format string
  // to read like "Friday, May 8, 2026 — 2:30 PM PT".
  const tz = "America/Los_Angeles";
  const headerDate = Utilities.formatDate(new Date(timestamp), tz, "EEEE, MMMM d, yyyy — h:mm a 'PT'");

  body.appendParagraph("Reflection — " + headerDate)
      .setHeading(DocumentApp.ParagraphHeading.HEADING2);

  const sourcePara = body.appendParagraph("Source: " + sourceFilename);
  sourcePara.editAsText().setItalic(true).setForegroundColor("#6b7280");
  sourcePara.setSpacingAfter(6);

  body.appendParagraph(transcript);
  body.appendParagraph(""); // visual breathing room between sections

  console.log("appended transcript for " + candidateId +
              " (" + transcript.length + " chars) → " + doc.getUrl());
  return textResponse("ok: " + doc.getId());
}

/**
 * Look in the candidate's materials folder for a Google Doc named
 * REFLECTIONS_DOC_NAME. Return it if found; otherwise create a fresh
 * Doc inside that folder and return it.
 */
function findOrCreateReflectionsDoc(materialsFolderId, candidateName) {
  const folder = DriveApp.getFolderById(materialsFolderId);
  const matches = folder.getFilesByName(REFLECTIONS_DOC_NAME);
  if (matches.hasNext()) {
    return DocumentApp.openById(matches.next().getId());
  }
  // Create new. DocumentApp.create() puts it in My Drive root, so we
  // have to move it into the candidate's materials folder.
  const doc = DocumentApp.create(REFLECTIONS_DOC_NAME);
  const file = DriveApp.getFileById(doc.getId());
  folder.addFile(file);
  DriveApp.getRootFolder().removeFile(file);

  // Give it a useful preamble so the Doc isn't empty before the first
  // section appears.
  const body = doc.getBody();
  body.clear();
  body.appendParagraph(candidateName + " — Reviewer Reflections")
      .setHeading(DocumentApp.ParagraphHeading.TITLE);
  body.appendParagraph(
    "Voice reflections from the search team, transcribed automatically. " +
    "Each entry shows the date the reviewer submitted and the source audio " +
    "file (in Drive's Reviewer Recordings folder)."
  ).setItalic(true);
  body.appendParagraph("");
  return doc;
}

/**
 * Replace the synthesis section at the top of the candidate's Reviewer
 * Reflections Doc. Preserves: (1) the doc title at index 0, (2) every
 * "Reflection — ..." section below. Deletes anything between the title
 * and the first reflection, then inserts the new synthesis content.
 *
 * Expected fields:
 *   action          : "update_synthesis"
 *   candidate_id    : slug, must be a key in CANDIDATE_MATERIAL_FOLDERS
 *   candidate_name  : display name (used as Doc title prefix when creating)
 *   synthesis_md    : the new synthesis content as a markdown-ish string
 *                     (parsed below: "## " → Heading 2, "- " → bullet,
 *                     "[...]" wrapper line → italic-grey meta line)
 *   generated_at    : ISO timestamp of when this synthesis was generated
 */
function handleUpdateSynthesis(p) {
  const candidateId   = p.candidate_id;
  const candidateName = p.candidate_name || candidateId || "(unknown)";
  const synthesisMd   = (p.synthesis_md || "").trim();
  const generatedAt   = p.generated_at || new Date().toISOString();

  if (!candidateId)  return textResponse("err: missing candidate_id");
  if (!synthesisMd)  return textResponse("err: missing synthesis_md");

  const candidate = resolveCandidate(candidateId);
  if (!candidate) return textResponse("err: unknown candidate_id " + candidateId);

  const doc = findOrCreateReflectionsDoc(candidate.materialsId, candidateName);
  const body = doc.getBody();

  // Find the index of the first reflection — it's the boundary between
  // synthesis (above) and reflections (below).
  let firstReflectionIdx = -1;
  const numChildren = body.getNumChildren();
  for (let i = 0; i < numChildren; i++) {
    const child = body.getChild(i);
    if (child.getType() !== DocumentApp.ElementType.PARAGRAPH) continue;
    const para = child.asParagraph();
    if (para.getHeading() === DocumentApp.ParagraphHeading.HEADING2 &&
        para.getText().indexOf("Reflection —") === 0) {
      firstReflectionIdx = i;
      break;
    }
  }

  // Delete everything between the title (idx 0) and the first reflection.
  // If no reflections yet, delete everything after the title.
  const deleteEnd = firstReflectionIdx >= 0
    ? firstReflectionIdx - 1
    : numChildren - 1;
  for (let i = deleteEnd; i >= 1; i--) {
    body.removeChild(body.getChild(i));
  }

  // Parse the markdown-ish synthesis into paragraph specs and insert
  // them at idx 1, 2, 3, ... (each insert pushes existing content down).
  const lines = synthesisMd.split(/\r?\n/);
  let insertIdx = 1;
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    let para;
    if (line === "") {
      para = body.insertParagraph(insertIdx, "");
    } else if (/^##\s+/.test(line)) {
      para = body.insertParagraph(insertIdx, line.replace(/^##\s+/, ""));
      para.setHeading(DocumentApp.ParagraphHeading.HEADING2);
    } else if (/^\[.*\]$/.test(line)) {
      // Meta line in brackets — italic, grey, smaller feel.
      para = body.insertParagraph(insertIdx, line.replace(/^\[|\]$/g, ""));
      para.editAsText().setItalic(true).setForegroundColor("#6b7280");
    } else if (/^[-•]\s+/.test(line)) {
      // Bullet
      const text = line.replace(/^[-•]\s+/, "");
      const item = body.insertListItem(insertIdx, text);
      item.setGlyphType(DocumentApp.GlyphType.BULLET);
      para = item;
    } else {
      para = body.insertParagraph(insertIdx, line);
    }
    insertIdx++;
  }

  // Spacer paragraph between synthesis and the first reflection,
  // for visual separation.
  body.insertParagraph(insertIdx, "");

  console.log("synthesis updated for " + candidateId +
              " (" + lines.length + " lines) → " + doc.getUrl());
  return textResponse("ok: " + doc.getId());
}

// -----------------------------------------------------------------------
// Live candidate discovery — Drive folder = source of truth
// -----------------------------------------------------------------------
//
// listCandidates() walks SEARCH_ROOT_ID and returns the public-facing
// list (slug + display name + materials Drive URL). resolveCandidate(slug)
// returns the same data PLUS the recordings folder ID, auto-creating the
// recordings subfolder if it doesn't exist yet. Both are cached in
// PropertiesService for CANDIDATE_CACHE_TTL_SEC seconds; mutations call
// invalidateCandidateCache() to force a re-walk.

function listCandidates() {
  const cached = readCandidateCache();
  if (cached) {
    // The cached value is the resolution map (full {slug, name, materialsId,
    // recordingsId}). Project to the public shape.
    return Object.values(cached).map(c => ({
      slug: c.slug,
      name: c.name,
      materials_url: "https://drive.google.com/drive/folders/" + c.materialsId,
    })).sort((a, b) => a.name.localeCompare(b.name));
  }
  // No cache → walk Drive, populate cache, return the list.
  const map = walkAndCacheCandidates();
  return Object.values(map).map(c => ({
    slug: c.slug,
    name: c.name,
    materials_url: "https://drive.google.com/drive/folders/" + c.materialsId,
  })).sort((a, b) => a.name.localeCompare(b.name));
}

function resolveCandidate(slug) {
  if (!slug) return null;
  const cached = readCandidateCache();
  if (cached && cached[slug]) return cached[slug];

  // Cache miss — walk Drive fresh.
  const map = walkAndCacheCandidates();
  return map[slug] || null;
}

function walkAndCacheCandidates() {
  const searchRoot = DriveApp.getFolderById(SEARCH_ROOT_ID);
  const recordingsRoot = DriveApp.getFolderById(REVIEWER_RECORDINGS_ROOT_ID);

  // Index existing recording subfolders by name so we don't have to
  // search Drive once per candidate.
  const recordingsByName = {};
  const recItr = recordingsRoot.getFolders();
  while (recItr.hasNext()) {
    const f = recItr.next();
    recordingsByName[f.getName()] = f;
  }

  const map = {};
  const itr = searchRoot.getFolders();
  while (itr.hasNext()) {
    const folder = itr.next();
    const name = folder.getName();
    if (SKIP_FOLDER_NAMES.indexOf(name) >= 0) continue;

    const slug = slugify(name);
    if (!slug) continue;

    // Find or create the matching recording subfolder.
    let recFolder = recordingsByName[name];
    if (!recFolder) {
      recFolder = recordingsRoot.createFolder(name);
      recordingsByName[name] = recFolder;
    }

    map[slug] = {
      slug: slug,
      name: name,
      materialsId: folder.getId(),
      recordingsId: recFolder.getId(),
    };
  }

  writeCandidateCache(map);
  return map;
}

function readCandidateCache() {
  try {
    const props = PropertiesService.getScriptProperties();
    const raw = props.getProperty(CANDIDATE_CACHE_KEY);
    if (!raw) return null;
    const wrapped = JSON.parse(raw);
    if (!wrapped || !wrapped.expires_at) return null;
    if (Date.now() > wrapped.expires_at) return null;
    return wrapped.map;
  } catch (e) {
    return null;
  }
}

function writeCandidateCache(map) {
  try {
    const props = PropertiesService.getScriptProperties();
    props.setProperty(CANDIDATE_CACHE_KEY, JSON.stringify({
      expires_at: Date.now() + CANDIDATE_CACHE_TTL_SEC * 1000,
      map: map,
    }));
  } catch (e) {
    console.warn("candidate cache write failed: " + e);
  }
}

/**
 * Manually clear the candidate cache. Useful if you've just added or
 * removed a folder and don't want to wait up to 5 minutes for the cache
 * to expire. Run this from the function dropdown → ▶ Run.
 */
function refreshCandidates() {
  const props = PropertiesService.getScriptProperties();
  props.deleteProperty(CANDIDATE_CACHE_KEY);
  const map = walkAndCacheCandidates();
  console.log("Refreshed cache with " + Object.keys(map).length +
              " candidate(s): " + Object.keys(map).sort().join(", "));
  return map;
}

/**
 * Slugify a candidate display name to a URL-safe identifier. Must
 * match the slugify() algorithm in index.html so client and server
 * agree on the same slug for a given name.
 */
function slugify(name) {
  return String(name)
    .toLowerCase()
    .replace(/["']/g, "")        // strip quotes (e.g. Kenneth "Kenny" Cook)
    .replace(/[^\w\s-]/g, "")    // strip remaining punctuation
    .replace(/[\s_]+/g, "-")     // whitespace/underscores → hyphen
    .replace(/^-+|-+$/g, "");    // trim leading/trailing hyphens
}

function textResponse(msg) {
  return ContentService
    .createTextOutput(msg)
    .setMimeType(ContentService.MimeType.TEXT);
}
