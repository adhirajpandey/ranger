import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from habitat_failover.agent import Docker
from habitat_failover.readiness import configure_readiness


@pytest.mark.parametrize(
    "policy",
    [
        {"health_path": "/healthz"},
        {"readiness_path": "//other/"},
        {"readiness_path": "/?query"},
        {"readiness_path": "/#fragment"},
        {"readiness_path": "https://other/"},
        {"readiness_path": "/\n"},
        {"expected_status": []},
        {"expected_status": [True]},
        {"expected_status": [199]},
        {"expected_status": [600]},
        {"expected_status": "200"},
    ],
)
def test_invalid_policy(policy):
    with pytest.raises(ValueError):
        configure_readiness(policy)


def test_policy_defaults_and_custom_endpoint():
    policy = {}
    configure_readiness(policy)
    assert policy == {"readiness_path": "/", "expected_status": [200]}
    policy = {"readiness_path": "/healthz", "expected_status": [200, 302, 401]}
    configure_readiness(policy)


@pytest.fixture
def origin():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            calls.append(self.path)
            if self.path == "/slow":
                time.sleep(0.1)
            status = int(self.path.strip("/")) if self.path.strip("/").isdigit() else 200
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/200")
            self.end_headers()
            try:
                if self.path != "/empty":
                    self.wfile.write(b"<html>ordinary page</html>")
            except BrokenPipeError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def docker_state(monkeypatch, running=True, health="none"):
    docker = Docker(probe_timeout=0.03)

    def run(args, timeout=None):
        if args[0] == "inspect":
            return json.dumps({"Running": running, "Health": {"Status": health}})
        return "container-id"

    monkeypatch.setattr(docker, "run", run)
    return docker


@pytest.mark.parametrize(
    "path,statuses,ready,status",
    [
        ("/", [200], True, 200),
        ("/empty", [200], True, 200),
        ("/401", [401], True, 401),
        ("/503", [200], False, 503),
        ("/302", [302], True, 302),
        ("/302", [200], False, 302),
        ("/slow", [200], False, None),
    ],
)
def test_http_probe(monkeypatch, origin, path, statuses, ready, status):
    port, calls = origin
    item = {
        "compose_file": "/unused",
        "project": "test",
        "service": "app",
        "port": port,
        "readiness_path": path,
        "expected_status": statuses,
    }
    result = docker_state(monkeypatch).observe(item, "test-node")
    assert result["ready"] is ready
    assert result["http_status"] == status
    assert result["node"] == "test-node"
    assert "release" not in result
    assert calls == [path]


@pytest.mark.parametrize("running,health", [(False, "none"), (True, "unhealthy")])
def test_container_gates_readiness(monkeypatch, origin, running, health):
    port, calls = origin
    item = {"compose_file": "/unused", "project": "test", "service": "app", "port": port}
    result = docker_state(monkeypatch, running, health).observe(item, "node")
    assert not result["ready"]
    if not running:
        assert not calls


def test_refused_connection(monkeypatch):
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        item = {
            "compose_file": "/unused",
            "project": "test",
            "service": "app",
            "port": sock.getsockname()[1],
        }
        result = docker_state(monkeypatch).observe(item, "node")
    assert not result["ready"]
    assert result["http_status"] is None
