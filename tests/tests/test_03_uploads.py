"""File upload endpoint tests."""

import os
import re
from datetime import UTC, datetime

from helpers.test_data import make_valid_zip, make_zip_with_path_traversal


def test_upload_valid_zip(api, test_client):
    _, _, headers = test_client
    zip_bytes = make_valid_zip()
    resp = api.post("/v1/uploads", headers=headers,
                    files={"file": ("test.zip", zip_bytes, "application/zip")})
    assert resp.status_code == 201
    body = resp.json()
    assert "upload_id" in body
    assert "expires_at" in body
    assert "size_bytes" in body


def test_upload_valid_zip_multiple_files(api, test_client):
    _, _, headers = test_client
    files = {f"file{i}.txt": f"content {i}" for i in range(5)}
    zip_bytes = make_valid_zip(files)
    resp = api.post("/v1/uploads", headers=headers,
                    files={"file": ("multi.zip", zip_bytes, "application/zip")})
    assert resp.status_code == 201


def test_upload_response_fields(api, test_client):
    _, _, headers = test_client
    zip_bytes = make_valid_zip()
    resp = api.post("/v1/uploads", headers=headers,
                    files={"file": ("test.zip", zip_bytes, "application/zip")})
    assert resp.status_code == 201
    body = resp.json()

    # UUID format
    uuid_pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    assert uuid_pattern.match(body["upload_id"])

    assert body["size_bytes"] > 0

    # expires_at should be in the future
    raw = body["expires_at"]
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    expires = datetime.fromisoformat(raw)
    if expires.tzinfo is None:
        # Server returns naive UTC timestamps — compare against UTC
        assert expires > datetime.utcnow()
    else:
        assert expires > datetime.now(UTC)


def test_upload_not_zip_returns_400(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/uploads", headers=headers,
                    files={"file": ("readme.txt", b"just text", "text/plain")})
    assert resp.status_code == 400
    assert resp.json().get("error_code") == "INVALID_ARCHIVE"


def test_upload_empty_file_returns_400(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/uploads", headers=headers,
                    files={"file": ("empty.zip", b"", "application/zip")})
    assert resp.status_code == 400
    assert resp.json().get("error_code") == "INVALID_ARCHIVE"


def test_upload_random_bytes_returns_400(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/uploads", headers=headers,
                    files={"file": ("random.zip", os.urandom(1024), "application/zip")})
    assert resp.status_code == 400
    assert resp.json().get("error_code") == "INVALID_ARCHIVE"


def test_upload_zip_path_traversal_returns_400(api, test_client):
    _, _, headers = test_client
    zip_bytes = make_zip_with_path_traversal()
    resp = api.post("/v1/uploads", headers=headers,
                    files={"file": ("traversal.zip", zip_bytes, "application/zip")})
    assert resp.status_code == 400
    assert resp.json().get("error_code") == "PATH_TRAVERSAL"


def test_upload_no_file_field_returns_422(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/uploads", headers=headers)
    assert resp.status_code == 422


def test_upload_wrong_field_name_returns_422(api, test_client):
    _, _, headers = test_client
    zip_bytes = make_valid_zip()
    resp = api.post("/v1/uploads", headers=headers,
                    files={"archive": ("test.zip", zip_bytes, "application/zip")})
    assert resp.status_code == 422


def test_upload_corrupt_zip_returns_400(api, test_client):
    _, _, headers = test_client
    # Starts with PK magic bytes but truncated
    corrupt = b"PK\x03\x04" + b"\x00" * 20
    resp = api.post("/v1/uploads", headers=headers,
                    files={"file": ("corrupt.zip", corrupt, "application/zip")})
    assert resp.status_code == 400
    assert resp.json().get("error_code") == "INVALID_ARCHIVE"
