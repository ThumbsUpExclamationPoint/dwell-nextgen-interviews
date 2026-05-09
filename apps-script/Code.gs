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
// Configuration — slug used by the page maps to the Drive folder ID where
// that candidate's recordings should be saved.
// -----------------------------------------------------------------------
const CANDIDATE_FOLDERS = {
  "chris-miller":       "1l14KnQt2uDQz_SfZF1j1W29BsLt5t4Gz",
  "daniel-kim":         "1fJsGvzsXERhu4ntTzuSQmTkXOvU1IIo6",
  "donoven-rice":       "1tNzghn9_yWaElEFMC18p4LzAUbXJhxy9",
  "jared-gallardo":     "1MG6AJxV0j2XTP9MLSHnIVIPVyNWn8D9F",
  "jonas-bond":         "1skaYal-jLmxXOS0anNu3EWQQWEGJVwxS",
  "kenneth-kenny-cook": "1dhCTc4uJ8EZaTJUjLcApgvOj5rkPdBns",
  "kyal-mcmillan":      "15i_kzQo5N6mfRQSHsyBIaSFk2HdzWlGM",
  "nicholas-feyma":     "1ljDW-tDFcabJQ-pu453YW5C5nFwTw61p",
  "peter-gabra":        "1TCfsKBcGJdWBJmtp45I7wgorgIu7e1pW",
  "saleem-bhayani":     "1yJvGobbqALSfJ_7p3zlUTKjvgwsXNcfs",
  "tiffany-dowdy":      "12u_TkGJMMJ-JVCN9RjyfY8bn1Qxi3AhH",
};

// Optional: also write a row to a Drive sheet for an audit trail. Set this
// to the file ID of a Google Sheet to enable, or leave empty to skip.
const AUDIT_SHEET_ID = "";

// -----------------------------------------------------------------------
// HTTP handlers
// -----------------------------------------------------------------------

/**
 * GET — simple health check so Matt can sanity-check the deploy URL by
 * pasting it in a browser. Returns plain text.
 */
function doGet(e) {
  return ContentService
    .createTextOutput("Dwell Next Gen reviewer ingest — alive.\n" +
                      "POST audio_b64, candidate_id, mime, filename to upload.")
    .setMimeType(ContentService.MimeType.TEXT);
}

/**
 * POST — receives the upload. Expected fields (all in e.parameter):
 *   candidate_id    : slug, must be a key in CANDIDATE_FOLDERS
 *   candidate_name  : display name (logged for debugging only)
 *   filename        : suggested file name (e.g. 2026-05-08T19-30-12-345Z__chris-miller.webm)
 *   mime            : MIME type of the audio
 *   audio_b64       : base64-encoded audio bytes
 *
 * Returns plain text "ok" on success, "err: <reason>" on failure.
 * The page uses fetch with mode:"no-cors" so it can't read the response —
 * the response text is for manual debugging via curl.
 */
function doPost(e) {
  try {
    const p = (e && e.parameter) || {};
    const candidateId = p.candidate_id;
    const audioB64    = p.audio_b64;
    const filename    = p.filename || ("recording-" + Date.now() + ".webm");
    const mime        = p.mime || "audio/webm";
    const candidateName = p.candidate_name || candidateId || "(unknown)";

    if (!candidateId)  return textResponse("err: missing candidate_id");
    if (!audioB64)     return textResponse("err: missing audio_b64");

    const folderId = CANDIDATE_FOLDERS[candidateId];
    if (!folderId) return textResponse("err: unknown candidate_id " + candidateId);

    // Decode base64 → bytes → Drive Blob → file in the candidate's folder.
    const bytes = Utilities.base64Decode(audioB64);
    const blob  = Utilities.newBlob(bytes, mime, filename);
    const folder = DriveApp.getFolderById(folderId);
    const file = folder.createFile(blob);

    // Optional audit trail.
    if (AUDIT_SHEET_ID) {
      try {
        const sheet = SpreadsheetApp.openById(AUDIT_SHEET_ID).getSheets()[0];
        sheet.appendRow([
          new Date(),
          candidateId,
          candidateName,
          filename,
          mime,
          bytes.length,
          file.getUrl(),
        ]);
      } catch (err) {
        // Audit failure shouldn't block a successful upload — log and move on.
        console.warn("audit sheet write failed: " + err);
      }
    }

    console.log("saved " + filename + " (" + bytes.length + " bytes) → " + file.getUrl());
    return textResponse("ok: " + file.getId());
  } catch (err) {
    console.error(err);
    return textResponse("err: " + (err && err.message ? err.message : err));
  }
}

function textResponse(msg) {
  return ContentService
    .createTextOutput(msg)
    .setMimeType(ContentService.MimeType.TEXT);
}
