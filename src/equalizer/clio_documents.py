"""
clio_documents.py — Uploads a saved Equalizer worksheet's PDF to Clio as a
Document inside the matter's existing "Evidence" folder (matter/Evidence/,
per firm standard folder template — assumed to already exist on every
matter per Ted's 2026-08-12 decision; this fails loud rather than silently
creating one in the wrong place if it's missing).

A worksheet stays editable after being saved, and Save to Clio can be
clicked again any time (Ted, 2026-08-14: "nothing is ever final" in this
line of work) — re-saving passes the worksheet's already-known
clio_document_id in, which POSTs a new Document *version* under that same
document (parent.type="Document") instead of creating a duplicate file
under the folder. Confirmed live 2026-08-14: this returns the same document
id back with version_number incremented, i.e. real Clio-native versioning,
viewable as version history in Clio's own UI.

Upload is a three-step flow — POST /documents.json to create the Document
record, PUT the raw file bytes to the presigned put_url that response
returns, then PATCH the document to mark fully_uploaded — shapes taken from
this project's own reference/openapi.json as a first pass only, per the
standing correction in reference_clio_api.md: that spec is not trusted as
the final answer for behavior.

Live-tested end to end 2026-08-14 (matter AMOS, CHRISTINE) — confirmed
working create -> PUT -> PATCH, after two real bugs found along the way:

1. latest_document_version came back as just {id, version_number} on
   create — same gotcha this project has hit before with phone_numbers/
   custom_rate: a nested field only returns a stub unless its subfields are
   explicitly requested. Fixed with an explicit fields= on the create POST.
2. The PUT to the presigned put_url kept failing with S3's
   SignatureDoesNotMatch until BOTH `Content-Type: application/pdf` and
   `x-amz-server-side-encryption: AES256` were sent together — confirmed by
   decoding the SignatureDoesNotMatch error's own CanonicalRequest, which
   named X-Amz-SignedHeaders=content-type;host;x-amz-server-side-encryption.
   Sending neither, or only one of the two, both fail the same way; the
   presigned URL doesn't expose what value it was signed with, so AES256
   is a confirmed-working value found by testing, not something documented
   by Clio — worth re-checking if this ever starts failing again, in case
   Clio's default bucket encryption setting changes.

This module still makes no attempt to clean up a create that isn't followed
by a successful upload, so a failed save (or the diagnostic testing that
found the bugs above) can leave empty/test-content documents behind in a
matter's Evidence folder — worth deleting those by hand in Clio's UI rather
than adding unverified DELETE-on-failure logic on top of a path that only
just started working.

Clio's own DELETE /documents/{id}.json is a soft delete — confirmed live
2026-08-14 (found via reference/openapi.json, then verified against a real
document Ted trashed himself): it sets `deleted_at` rather than actually
removing the record, and the document stays fetchable via GET (still
200, not 404) for 30 days before Clio purges it for good. Left unhandled,
this is exactly how the dashboard's local `clio_document_id` link can go
stale without anything erroring — the worksheet still shows "Saved to
Clio" and clicking Save to Clio again would try to add a version to a
trashed document. `is_document_trashed()` below checks `deleted_at`
(treating an outright 404 — the post-30-day-purge case — as trashed too);
`upload_pdf()` uses it to fall back to creating a fresh document in the
Evidence folder instead of versioning a document that's gone.
"""

import logging
import os

import requests

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
FOLDERS_ENDPOINT = f"{BASE_URL}/api/v4/folders.json"
DOCUMENTS_ENDPOINT = f"{BASE_URL}/api/v4/documents.json"

EVIDENCE_FOLDER_NAME = "Evidence"


def find_evidence_folder_id(session: requests.Session, matter_id: int) -> int:
    resp = session.get(FOLDERS_ENDPOINT, params={
        "matter_id": matter_id, "query": EVIDENCE_FOLDER_NAME, "fields": "id,name",
    })
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to look up Evidence folder for matter {matter_id}: {resp.status_code} {resp.text[:200]}")

    matches = [f for f in resp.json().get("data", []) if (f.get("name") or "").strip().lower() == EVIDENCE_FOLDER_NAME.lower()]
    if not matches:
        raise RuntimeError(
            f"No 'Evidence' folder found on matter {matter_id} — this tool expects every matter to already "
            "have one (firm standard folder template). Create it in Clio first, then save again."
        )
    return matches[0]["id"]


def is_document_trashed(session: requests.Session, document_id: int) -> bool:
    """True if the document has been moved to Clio's Trash (deleted_at set)
    or no longer exists at all (404 — permanently purged after Clio's
    30-day trash retention). Either way, it's not safe to add a new version
    to it."""
    resp = session.get(f"{BASE_URL}/api/v4/documents/{document_id}.json", params={"fields": "id,deleted_at"})
    if resp.status_code == 404:
        return True
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to check status of document {document_id}: {resp.status_code} {resp.text[:200]}")
    return resp.json()["data"].get("deleted_at") is not None


def upload_pdf(
    session: requests.Session, matter_id: int, filename: str, pdf_bytes: bytes,
    existing_document_id: int | None = None,
) -> int:
    """Returns the Clio document id — a new one on first save (or if the
    previous save's document has since been trashed/deleted directly in
    Clio), or the same existing_document_id (now with an incremented
    version) on an ordinary re-save."""
    if existing_document_id and not is_document_trashed(session, existing_document_id):
        parent = {"id": existing_document_id, "type": "Document"}
    else:
        parent = {"id": find_evidence_folder_id(session, matter_id), "type": "Folder"}

    create_payload = {
        "data": {
            "name": filename,
            "filename": filename,
            "parent": parent,
            "content_type": "application/pdf",
        }
    }
    # Explicit subfield selection on latest_document_version — confirmed
    # live 2026-08-14 that without this, Clio returns only {id,
    # version_number} and omits uuid/put_url entirely (same gotcha as
    # phone_numbers/custom_rate elsewhere in this project).
    params = {"fields": "id,latest_document_version{id,uuid,put_url,version_number}"}
    logging.info("POST %s matter=%s payload=%s params=%s", DOCUMENTS_ENDPOINT, matter_id, create_payload, params)
    resp = session.post(DOCUMENTS_ENDPOINT, json=create_payload, params=params)
    logging.info("Response: %s %s", resp.status_code, resp.text[:500])
    if resp.status_code != 201:
        raise RuntimeError(f"Failed to create document record for matter {matter_id}: {resp.status_code} {resp.text[:300]}")

    doc = resp.json()["data"]
    document_id = doc["id"]
    version = doc.get("latest_document_version") or {}
    put_url = version.get("put_url")
    upload_uuid = version.get("uuid")
    if not put_url or not upload_uuid:
        raise RuntimeError(f"Document {document_id} created but no put_url/uuid returned — cannot upload content: {doc}")

    # Both headers are required — the put_url is presigned with
    # X-Amz-SignedHeaders=content-type;host;x-amz-server-side-encryption, so
    # a request missing either (or sending a different Content-Type) fails
    # S3's signature check. See module docstring for how this was found.
    put_resp = requests.put(put_url, data=pdf_bytes, headers={
        "Content-Type": "application/pdf",
        "x-amz-server-side-encryption": "AES256",
    })
    logging.info("PUT to storage URL for document %s -> %s", document_id, put_resp.status_code)
    if put_resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"Failed to upload PDF content for document {document_id}: {put_resp.status_code} {put_resp.text[:300]}")

    patch_payload = {"data": {"uuid": upload_uuid, "fully_uploaded": True}}
    patch_resp = session.patch(f"{BASE_URL}/api/v4/documents/{document_id}.json", json=patch_payload)
    logging.info("PATCH fully_uploaded for document %s -> %s %s", document_id, patch_resp.status_code, patch_resp.text[:300])
    if patch_resp.status_code != 200:
        raise RuntimeError(f"Uploaded content but failed to mark document {document_id} fully uploaded: {patch_resp.status_code} {patch_resp.text[:300]}")

    return document_id


def build_session() -> requests.Session:
    token = os.getenv("CLIO_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("CLIO_ACCESS_TOKEN not set in .env")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return session
