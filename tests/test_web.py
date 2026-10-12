"""Tests for the configuration memory web panel."""

import json
import urllib.error
import urllib.request

import pytest

import cardio2e_modules.cardio2e_memory as mem
from cardio2e_modules.cardio2e_web import start_web_server


@pytest.fixture
def panel(tmp_path):
    def start(allowed=()):
        service = mem.MemoryService(str(tmp_path))
        server = start_web_server(service, 0, allowed)
        servers.append(server)
        return service, "http://127.0.0.1:%d/" % server.server_address[1]
    servers = []
    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers, r.read()


def _post(url, data=b"{}"):
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_page_uses_relative_urls(panel):
    _, base = panel()
    status, _, body = _get(base)
    assert status == 200
    page = body.decode()
    assert "fetch('api/status')" in page and "'/api/" not in page
    assert str(mem.MEMORY_SIZE) in page


def test_status_lists_backups(panel, tmp_path):
    (tmp_path / "cardio2e-20261012-120000.bin").write_bytes(bytes(mem.MEMORY_SIZE))
    _, base = panel()
    _, _, body = _get(base + "api/status")
    status = json.loads(body)
    assert status["connected"] is False
    assert [b["name"] for b in status["backups"]] == ["cardio2e-20261012-120000.bin"]


def test_download_and_traversal(panel, tmp_path):
    (tmp_path / "cardio2e-1.bin").write_bytes(b"abc")
    _, base = panel()
    status, headers, body = _get(base + "backups/cardio2e-1.bin")
    assert status == 200 and body == b"abc"
    assert "attachment" in headers["Content-Disposition"]
    for bad in ("backups/..%2Fsecret.bin", "backups/missing.bin"):
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(base + bad)
        assert e.value.code == 404


def test_forbidden_client(panel):
    _, base = panel(allowed=("172.30.32.2",))
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base + "api/status")
    assert e.value.code == 403


def test_backup_when_disconnected_is_conflict(panel):
    _, base = panel()
    code, body = _post(base + "api/backup")
    assert code == 409 and "not connected" in body["error"]


def test_upload_wrong_size_is_rejected(panel):
    _, base = panel()
    code, body = _post(base + "api/upload?filename=x.bin", b"123")
    assert code == 409 and "bytes" in body["error"]


def test_cancel_without_job_is_conflict(panel):
    _, base = panel()
    code, body = _post(base + "api/cancel")
    assert code == 409 and "no job" in body["error"]


def test_delete_backup(panel, tmp_path):
    (tmp_path / "cardio2e-1.bin").write_bytes(b"x")
    _, base = panel()
    code, _ = _post(base + "api/delete", b'{"name": "cardio2e-1.bin"}')
    assert code == 200 and not (tmp_path / "cardio2e-1.bin").exists()
    code, body = _post(base + "api/delete", b'{"name": "../etc/passwd"}')
    assert code == 409 and "no such backup" in body["error"]
