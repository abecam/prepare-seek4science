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
   to attach the new Investigation to (DEST_PROJECT_ID below).

4. Content blobs (the actual file bytes of Data files / SOPs / Models) are
   fetched from the source and re-uploaded to the destination. Large
   files will take time and disk space (they are streamed through a temp
   file, not fully loaded in memory).

5. This script does not attempt to migrate: permissions/sharing policies,
   people/contributor associations (assets are simply created under the
   API user's account on the destination), custom metadata types that
   don't exist on the destination, or licenses that aren't configured on
   the destination. These will need manual follow-up.

6. Always test with DRY_RUN = True first, and test against a scratch/dev
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

DEFAULT_CONFIG = {
    "SOURCE_BASE_URL": "https://fairdomhub.org",
    "SOURCE_AUTH": None,
    "DEST_BASE_URL": "http://localhost:3033/",
    "DEST_AUTH": ("name", 'password'), # or use an API token, see below
    # If your destination SEEK instance uses API tokens instead of basic auth:
    # DEST_HEADERS_EXTRA = {"Authorization": "Bearer <token>"}
    "DEST_HEADERS_EXTRA": {},
    "INVESTIGATION_ID": 658,
    "DEST_PROJECT_ID": 1,
    "DRY_RUN": False,
    "DOWNLOAD_DIR": "./_seek_migration_blobs",
}

JSONAPI_HEADERS = {
    "Content-Type": "application/vnd.api+json",
    "Accept": "application/vnd.api+json",
}


def load_config():
    """Load settings from config.json, excluding JSONAPI_HEADERS from the file."""
    config_path = Path(__file__).with_name("config.json")
    config = dict(DEFAULT_CONFIG)

    if config_path.exists():
        with config_path.open(encoding="utf-8") as handle:
            loaded_config = json.load(handle)
        for key, default_value in DEFAULT_CONFIG.items():
            if key not in loaded_config:
                continue
            value = loaded_config[key]
            if key in {"SOURCE_AUTH", "DEST_AUTH"} and isinstance(value, list):
                value = tuple(value)
            config[key] = value

    return config


CONFIG = load_config()
SOURCE_BASE_URL = CONFIG["SOURCE_BASE_URL"]
SOURCE_AUTH = CONFIG["SOURCE_AUTH"]  # e.g. ("username", "password") if the investigation is
                                     # private; leave as None for public resources

DEST_BASE_URL = CONFIG["DEST_BASE_URL"]
DEST_AUTH = CONFIG["DEST_AUTH"]  
DEST_HEADERS_EXTRA = CONFIG["DEST_HEADERS_EXTRA"]

INVESTIGATION_ID = CONFIG["INVESTIGATION_ID"]  # the investigation to migrate
DEST_PROJECT_ID = CONFIG["DEST_PROJECT_ID"]  # an existing project id on the destination
                                              # that the new investigation will belong to

DRY_RUN = CONFIG["DRY_RUN"]  # if True, no POSTs are sent to DEST; the
                             # script just prints what it *would* do

DOWNLOAD_DIR = Path(CONFIG["DOWNLOAD_DIR"])
DOWNLOAD_DIR.mkdir(exist_ok=True)

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

    def get(self, path_or_url):
        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}{path_or_url}"
        resp = self.session.get(url)
        resp.raise_for_status()
        return resp.json()

    def get_binary(self, url, dest_path):
        with self.session.get(url, stream=True) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
        return dest_path

    def post(self, path, payload, max_retries=6, base_delay=1.5):
        url = f"{self.base_url}{path}"
        if DRY_RUN:
            print(f"[DRY RUN] POST {url}")
            print(json.dumps(payload, indent=2)[:2000])
            return {"data": {"id": f"DRYRUN-{path}", "type": payload['data']['type']}}

        for attempt in range(1, max_retries + 1):
            resp = self.session.post(url, json=payload)
            if resp.ok:
                return resp.json()

            transient = resp.status_code in (500, 503) and (
                "database is locked" in resp.text.lower()
                or "busyexception" in resp.text.lower()
            )
            if transient and attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                print(
                    f"  POST {url} hit a transient DB lock (attempt {attempt}/{max_retries}), "
                    f"retrying in {delay:.1f}s ..."
                )
                time.sleep(delay)
                continue

            print(f"POST {url} failed ({resp.status_code}): {resp.text[:1000]}")
            resp.raise_for_status()

    def put(self, path_or_url, data, headers=None):
        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}{path_or_url}"
        response = self.session.put(url, data=data, headers=headers)
        response.raise_for_status()
        return response


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
    to the (already-created) destination assay.

    SEEK's upload flow is two-step: first create the resource with placeholder
    content blobs, then PUT the file bytes to the content blob upload links
    that the create response returns.

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

    print(f"  Registered {resource_type} {new_id} with {len(content_blobs_payload)} placeholder blob(s).")

    if DRY_RUN or not blob_paths:
        return new_id

    created_content_blobs = result.get("data", {}).get("attributes", {}).get("content_blobs", [])
    if not created_content_blobs:
        print(f"  WARNING: no content blob upload links were returned for {resource_type} {new_id}.")
        return new_id

    for idx, blob in enumerate(blob_paths):
        if idx >= len(created_content_blobs):
            break

        blob_meta = created_content_blobs[idx]
        blob_url = blob_meta.get("link")
        if not blob_url:
            continue

        content_type = blob.get("meta", {}).get("content_type", "application/octet-stream")
        with open(blob["path"], "rb") as handle:
            blob_bytes = handle.read()

        print(f"  Uploading blob {idx + 1}/{len(blob_paths)} for {resource_type} {new_id} -> {blob_url}")
        dest.put(blob_url, data=blob_bytes, headers={"Content-Type": content_type})

    return new_id


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def main():
    source = SeekClient(SOURCE_BASE_URL, auth=SOURCE_AUTH)
    dest = SeekClient(DEST_BASE_URL, auth=DEST_AUTH, extra_headers=DEST_HEADERS_EXTRA)

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
        time.sleep(0.8)  # give SQLite a beat between writes

        for assay_id in study_assay_ids[study["id"]]:
            assay = assays_by_id[assay_id]
            new_assay_id = create_assay(dest, assay, new_study_id)
            id_map["assays"][assay["id"]] = new_assay_id
            print(f"    Assay {assay['id']} -> new id {new_assay_id}")
            time.sleep(0.8)

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
                    time.sleep(0.8)

            # be polite to the source server
            time.sleep(0.2)

    print("\nDone." if not DRY_RUN else "\nDry run complete - no data was written to the destination.")
    print("id_map:", json.dumps(id_map, indent=2))


if __name__ == "__main__":
    main()