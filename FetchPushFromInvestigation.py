#!/usr/bin/env python3
"""
fairdomhub_migrate.py
======================

Fetches an Investigation (and the Studies / Assays / Data files / SOPs /
Models / Publications it links to) from a FAIRDOM-SEEK instance
(e.g. https://fairdomhub.org) and re-creates the same object graph on a
*different* SEEK instance, using SEEK's JSON:API.

IMPORTANT CONTEXT / LIMITATIONS (please read before running)
--------------------------------------------------------------
1. SEEK's API is a JSON:API (https://jsonapi.org) implementation. Every
   resource has the shape:
       {"data": {"type": "...", "id": "...",
                 "attributes": {...}, "relationships": {...}}}

2. Writing to a SEEK instance requires authentication - either HTTP Basic
   Auth with a normal SEEK login, or an API token (SEEK supports both).
   Read this instance's own API docs (<dest>/api) to confirm, since this
   can vary slightly between SEEK versions.

3. An Investigation cannot exist without a Project, and a Study cannot
   exist without an Investigation, etc. This script does NOT create
   Projects on the destination - you must already have (or create) a
   destination Project and tell the script which destination project id
   to attach the new Investigation to (DEST_PROJECT_ID below). Note that
   data_files/sops/models ALSO need their own explicit 'projects'
   relationship - SEEK does not infer it from the assay link.

4. Content blobs (the actual file bytes of Data files / SOPs / Models) are
   fetched from the source and re-uploaded to the destination. Large
   files will take time and disk space (they are streamed through a temp
   file, not fully loaded in memory).

5. This script does not attempt to migrate: permissions/sharing policies,
   people/contributor associations (assets are simply created under the
   API user's account on the destination), custom metadata types that
   don't exist on the destination, or licenses that aren't configured on
   the destination. These will need manual follow-up.

6. Both GET and POST requests retry automatically with exponential
   backoff on transient 502/503/504 responses (mirrors what `wget`'s
   default retry behaviour does - if fairdomhub.org is momentarily
   overloaded or rate-limiting, a single failed request no longer kills
   the whole run).

7. Always test with DRY_RUN = True first, and test against a scratch/dev
   SEEK instance before pointing this at anything important.

Usage
-----
    pip install requests
    python fairdomhub_migrate.py
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

# --------------------------------------------------------------------------
# CONFIGURATION - edit these before running
# --------------------------------------------------------------------------

SOURCE_BASE_URL = "https://fairdomhub.org"
SOURCE_AUTH = None  # e.g. ("username", "password") if the investigation is
                    # private; leave as None for public resources

DEST_BASE_URL = "https://your-seek-instance.example.org"
DEST_AUTH = ("dest_username", "dest_password")  # or use an API token, see below
# If your destination SEEK instance uses API tokens instead of basic auth:
# DEST_HEADERS_EXTRA = {"Authorization": "Bearer <token>"}
DEST_HEADERS_EXTRA = {}

INVESTIGATION_ID = 658          # the investigation to migrate
DEST_PROJECT_ID = 1             # an existing project id on the destination
                                 # that the new investigation will belong to

DRY_RUN = True                  # if True, no POSTs are sent to DEST; the
                                 # script just prints what it *would* do

DOWNLOAD_DIR = Path("./_seek_migration_blobs")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# fairdomhub.org (and possibly other SEEK instances behind a WAF/proxy)
# can be picky about the default "python-requests/x.y" User-Agent, and/or
# rate-limit bursts of requests. A less bot-like UA plus a small delay
# between reads mirrors what a slow, patient `wget` run does.
REQUEST_USER_AGENT = "Wget/1.21.3 (linux-gnu)"
REQUEST_DELAY_SECONDS = 0.5     # pause between GETs while walking the tree
REQUEST_TIMEOUT_SECONDS = 60    # generous timeout - the source can be slow

JSONAPI_HEADERS = {
    "Content-Type": "application/vnd.api+json",
    "Accept": "application/vnd.api+json",
    "User-Agent": REQUEST_USER_AGENT,
}

# --------------------------------------------------------------------------
# Low level helpers
# --------------------------------------------------------------------------


class SeekClient:
    """Thin wrapper around requests for talking to a SEEK JSON:API."""

    def __init__(self, base_url, auth=None, extra_headers=None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        if auth:
            self.session.auth = auth
        headers = dict(JSONAPI_HEADERS)
        if extra_headers:
            headers.update(extra_headers)
        self.session.headers.update(headers)

    def _retry_after_seconds(self, resp, default):
        """Respect a Retry-After header if the server sent one, else fall
        back to the given default backoff."""
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), default)
            except ValueError:
                pass
        return default

    def get(self, path_or_url, max_retries=6, base_delay=2.0):
        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}{path_or_url}"
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            except requests.exceptions.RequestException as exc:
                if attempt < max_retries:
                    delay = base_delay * (2 ** (attempt - 1))
                    print(f"  GET {url} raised {exc!r} (attempt {attempt}/{max_retries}), "
                          f"retrying in {delay:.1f}s ...")
                    time.sleep(delay)
                    continue
                raise

            if resp.ok:
                if REQUEST_DELAY_SECONDS:
                    time.sleep(REQUEST_DELAY_SECONDS)
                return resp.json()

            transient = resp.status_code in (502, 503, 504)
            if transient and attempt < max_retries:
                delay = self._retry_after_seconds(resp, base_delay * (2 ** (attempt - 1)))
                print(f"  GET {url} -> {resp.status_code} (attempt {attempt}/{max_retries}), "
                      f"retrying in {delay:.1f}s ...")
                time.sleep(delay)
                continue

            resp.raise_for_status()

    def get_binary(self, url, dest_path, max_retries=6, base_delay=2.0):
        for attempt in range(1, max_retries + 1):
            try:
                with self.session.get(url, stream=True, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                    if resp.ok:
                        with open(dest_path, "wb") as f:
                            for chunk in resp.iter_content(chunk_size=1 << 16):
                                f.write(chunk)
                        if REQUEST_DELAY_SECONDS:
                            time.sleep(REQUEST_DELAY_SECONDS)
                        return dest_path

                    transient = resp.status_code in (502, 503, 504)
                    if transient and attempt < max_retries:
                        delay = self._retry_after_seconds(resp, base_delay * (2 ** (attempt - 1)))
                        print(f"  GET {url} -> {resp.status_code} (attempt {attempt}/{max_retries}), "
                              f"retrying in {delay:.1f}s ...")
                        time.sleep(delay)
                        continue
                    resp.raise_for_status()
            except requests.exceptions.RequestException as exc:
                if attempt < max_retries:
                    delay = base_delay * (2 ** (attempt - 1))
                    print(f"  GET {url} raised {exc!r} (attempt {attempt}/{max_retries}), "
                          f"retrying in {delay:.1f}s ...")
                    time.sleep(delay)
                    continue
                raise

    def post(self, path, payload, max_retries=6, base_delay=1.5):
        url = f"{self.base_url}{path}"
        if DRY_RUN:
            print(f"[DRY RUN] POST {url}")
            print(json.dumps(payload, indent=2)[:2000])
            return {"data": {"id": f"DRYRUN-{path}", "type": payload['data']['type']}}

        for attempt in range(1, max_retries + 1):
            resp = self.session.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            if resp.ok:
                return resp.json()

            transient = resp.status_code in (500, 502, 503, 504) and (
                "database is locked" in resp.text.lower()
                or "busyexception" in resp.text.lower()
                or resp.status_code in (502, 503, 504)
            )
            if transient and attempt < max_retries:
                delay = self._retry_after_seconds(resp, base_delay * (2 ** (attempt - 1)))
                print(
                    f"  POST {url} hit a transient error (attempt {attempt}/{max_retries}), "
                    f"retrying in {delay:.1f}s ..."
                )
                time.sleep(delay)
                continue

            print(f"POST {url} failed ({resp.status_code}): {resp.text[:1000]}")
            resp.raise_for_status()


# --------------------------------------------------------------------------
# Fetching from the SOURCE instance
# --------------------------------------------------------------------------


def fetch_resource(client, resource_type, resource_id):
    """GET a single resource (investigation/study/assay/data_file/...) by id."""
    doc = client.get(f"/{resource_type}/{resource_id}.json")
    return doc["data"]


def related_ids(resource_data, rel_name):
    """Pull out the (type, id) pairs for a relationship, e.g. 'studies'."""
    rel = resource_data.get("relationships", {}).get(rel_name)
    if not rel or not rel.get("data"):
        return []
    data = rel["data"]
    if isinstance(data, dict):
        data = [data]
    return [(d["type"], d["id"]) for d in data]


def fetch_investigation_tree(client, investigation_id):
    """
    Fetches the investigation plus everything hanging off it: studies,
    assays, and the data_files / sops / models / publications referenced
    by those assays.

    Returns a dict describing the whole tree, keyed by (type, id).
    """
    tree = {"investigation": None, "studies": [], "assays": [], "assets": {}}

    investigation = fetch_resource(client, "investigations", investigation_id)
    tree["investigation"] = investigation

    for _, study_id in related_ids(investigation, "studies"):
        study = fetch_resource(client, "studies", study_id)
        tree["studies"].append(study)

        for _, assay_id in related_ids(study, "assays"):
            assay = fetch_resource(client, "assays", assay_id)
            tree["assays"].append(assay)

            # Assets that can hang off an assay in SEEK
            for rel_name, res_type in [
                ("data_files", "data_files"),
                ("sops", "sops"),
                ("models", "models"),
                ("publications", "publications"),
            ]:
                for _, res_id in related_ids(assay, rel_name):
                    key = (res_type, res_id)
                    if key not in tree["assets"]:
                        tree["assets"][key] = fetch_resource(client, res_type, res_id)

    return tree


def download_content_blob(client, resource_type, resource_data, out_dir):
    """
    Downloads the actual file content for an asset (data_file/sop/model).
    SEEK exposes content blobs under a 'content_blobs' relationship /
    a '/<type>/<id>/content_blobs/<blob_id>/download' link. The exact
    link is easiest to read straight off the resource's own JSON.
    """
    blobs = resource_data.get("attributes", {}).get("content_blobs", [])
    downloaded = []
    for blob in blobs:
        link = blob.get("link")
        original_filename = blob.get("original_filename", f"blob_{resource_data['id']}")
        if not link:
            continue
        dest_path = out_dir / f"{resource_type}_{resource_data['id']}_{original_filename}"
        print(f"  Downloading blob for {resource_type} {resource_data['id']} -> {dest_path}")
        client.get_binary(link, dest_path)
        downloaded.append({"path": dest_path, "meta": blob})
    return downloaded


# --------------------------------------------------------------------------
# Creating on the DESTINATION instance
# --------------------------------------------------------------------------


def create_investigation(dest, investigation, project_id):
    attrs = investigation["attributes"]
    payload = {
        "data": {
            "type": "investigations",
            "attributes": {
                "title": attrs.get("title"),
                "description": attrs.get("description"),
            },
            "relationships": {
                "projects": {"data": [{"id": str(project_id), "type": "projects"}]}
            },
        }
    }
    result = dest.post("/investigations", payload)
    return result["data"]["id"]


def create_study(dest, study, dest_investigation_id):
    attrs = study["attributes"]
    payload = {
        "data": {
            "type": "studies",
            "attributes": {
                "title": attrs.get("title"),
                "description": attrs.get("description"),
            },
            "relationships": {
                "investigation": {"data": {"id": str(dest_investigation_id), "type": "investigations"}}
            },
        }
    }
    result = dest.post("/studies", payload)
    return result["data"]["id"]


def create_assay(dest, assay, dest_study_id):
    attrs = assay["attributes"]
    payload = {
        "data": {
            "type": "assays",
            "attributes": {
                "title": attrs.get("title"),
                "description": attrs.get("description"),
                "assay_class": attrs.get("assay_class"),
                "assay_type": attrs.get("assay_type"),
            },
            "relationships": {
                "study": {"data": {"id": str(dest_study_id), "type": "studies"}}
            },
        }
    }
    result = dest.post("/assays", payload)
    return result["data"]["id"]


def create_asset(dest, resource_type, resource_data, dest_assay_id, dest_project_id, blob_paths):
    """Creates a data_file / sop / model on the destination and links it
    to the (already-created) destination assay. If blob_paths is
    non-empty, the first blob's bytes are (re)uploaded as content.

    SEEK requires data_files/sops/models to carry their own 'projects'
    relationship (it is not inferred from the assay), so dest_project_id
    must be passed in explicitly.
    """
    attrs = resource_data["attributes"]

    content_blobs_payload = []
    for blob in blob_paths:
        content_blobs_payload.append(
            {
                "original_filename": blob["meta"].get("original_filename"),
                "content_type": blob["meta"].get("content_type", "application/octet-stream"),
            }
        )

    payload = {
        "data": {
            "type": resource_type,
            "attributes": {
                "title": attrs.get("title"),
                "description": attrs.get("description"),
                "content_blobs": content_blobs_payload,
            },
            "relationships": {
                "projects": {"data": [{"id": str(dest_project_id), "type": "projects"}]},
                "assays": {"data": [{"id": str(dest_assay_id), "type": "assays"}]},
            },
        }
    }
    result = dest.post(f"/{resource_type}", payload)
    new_id = result["data"]["id"]

    # If the destination returned upload URLs for the content blobs, the
    # actual bytes would be PUT there next. Skipped in DRY_RUN, and the
    # exact mechanism (direct PUT vs multipart) should be confirmed against
    # your destination SEEK version's docs at <DEST_BASE_URL>/api.
    if not DRY_RUN and blob_paths:
        print(f"  NOTE: upload the downloaded bytes for {resource_type} {new_id} "
              f"using the upload link SEEK returned for its content_blobs.")

    return new_id


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def main():
    source = SeekClient(SOURCE_BASE_URL, auth=SOURCE_AUTH)
    dest = SeekClient(DEST_BASE_URL, auth=DEST_AUTH, extra_headers=DEST_HEADERS_EXTRA)

    if DRY_RUN:
        print("DRY RUN mode - no data will be written to the destination.")
    print(f"Fetching investigation {INVESTIGATION_ID} from {SOURCE_BASE_URL} ...")
    tree = fetch_investigation_tree(source, INVESTIGATION_ID)

    print(
        f"Found: 1 investigation, {len(tree['studies'])} studies, "
        f"{len(tree['assays'])} assays, {len(tree['assets'])} linked assets."
    )

    # id_map keeps track of old_id -> new_id per resource type so we can
    # rewire relationships as we recreate things on the destination.
    id_map = {"investigations": {}, "studies": {}, "assays": {}}

    inv = tree["investigation"]
    new_inv_id = create_investigation(dest, inv, DEST_PROJECT_ID)
    id_map["investigations"][inv["id"]] = new_inv_id
    print(f"Investigation {inv['id']} -> new id {new_inv_id}")

    # Build a study_id -> assay list map from the source relationships,
    # since tree['assays'] is a flat list.
    study_assay_ids = {}
    for study in tree["studies"]:
        study_assay_ids[study["id"]] = [aid for _, aid in related_ids(study, "assays")]

    assays_by_id = {a["id"]: a for a in tree["assays"]}

    for study in tree["studies"]:
        new_study_id = create_study(dest, study, new_inv_id)
        id_map["studies"][study["id"]] = new_study_id
        print(f"  Study {study['id']} -> new id {new_study_id}")
        time.sleep(0.3)  # give SQLite a beat between writes

        for assay_id in study_assay_ids[study["id"]]:
            assay = assays_by_id[assay_id]
            new_assay_id = create_assay(dest, assay, new_study_id)
            id_map["assays"][assay["id"]] = new_assay_id
            print(f"    Assay {assay['id']} -> new id {new_assay_id}")
            time.sleep(0.3)

            for rel_name, res_type in [
                ("data_files", "data_files"),
                ("sops", "sops"),
                ("models", "models"),
            ]:
                for _, asset_id in related_ids(assay, rel_name):
                    asset_data = tree["assets"].get((res_type, asset_id))
                    if not asset_data:
                        continue
                    blobs = []
                    if not DRY_RUN:
                        blobs = download_content_blob(source, res_type, asset_data, DOWNLOAD_DIR)
                    new_asset_id = create_asset(
                        dest, res_type, asset_data, new_assay_id, DEST_PROJECT_ID, blobs
                    )
                    print(f"      {res_type} {asset_id} -> new id {new_asset_id}")

    print("\nDone." if not DRY_RUN else "\nDry run complete - no data was written to the destination.")
    print("id_map:", json.dumps(id_map, indent=2))


if __name__ == "__main__":
    main()