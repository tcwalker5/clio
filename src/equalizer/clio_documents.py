"""
clio_documents.py — Uploads the finalized Equalizer PDF to Clio as a Document
inside the matter's existing "Evidence" folder (matter/Evidence/, per firm
standard folder template — assumed to already exist on every matter per
Ted's 2026-08-12 decision; this fails loud rather than silently creating one
in the wrong place if it's missing).

Upload is a three-step flow — POST /documents.json to create the Document
record, PUT the raw file bytes to the presigned put_url that response
returns, then PATCH the document to mark fully_uploaded — shapes taken from
clio-rate-import's openapi.json as a first pass only, per this project's
standing correction (reference_clio_api.md, 2026-07-22): that spec is not
trusted as the final answer for behavior. NOT YET LIVE-TESTED as of writing
— the first real call should be watched against Clio's own UI (does the PDF
actually land in Evidence, readable, under the right name) before this runs
unattended for a real case.
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
            "have one (firm standard folder template). Create it in Clio first, then finalize again."
        )
    return matches[0]["id"]


def upload_pdf(session: requests.Session, matter_id: int, filename: str, pdf_bytes: bytes) -> int:
    """Returns the new Clio document id."""
    folder_id = find_evidence_folder_id(session, matter_id)

    create_payload = {
        "data": {
            "name": filename,
            "filename": filename,
            "parent": {"id": folder_id, "type": "Folder"},
            "content_type": "application/pdf",
        }
    }
    logging.info("POST %s matter=%s payload=%s", DOCUMENTS_ENDPOINT, matter_id, create_payload)
    resp = session.post(DOCUMENTS_ENDPOINT, json=create_payload)
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

    put_resp = requests.put(put_url, data=pdf_bytes, headers={"Content-Type": "application/pdf"})
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
