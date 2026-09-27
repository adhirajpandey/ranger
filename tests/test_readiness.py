import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ranger.agent import Docker
from ranger.config import configure_readiness


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


def workload(port, **readiness):
    item = {"compose_file": "/unused", "project": "test", "service": "app", "port": port}
    item.update(readiness)
    configure_readiness(item)
    return item


def docker_state(monkeypatch, running=True, health="none"):
    docker = Docker(probe_timeout=0.03)

    def run(args, timeout=None):
        if args[0] == "inspect":
            return json.dumps({"Running": running, "Health": {"Status": health}})
        return "container-id"

    monkeypatch.setattr(docker, "run", run)
    return docker


@pytest.mark.parametrize(
    "path,statuses,state,status",
    [
        ("/", [200], "ready", 200),
        ("/empty", [200], "ready", 200),
        ("/401", [401], "ready", 401),
        ("/503", [200], "unready", 503),
        ("/302", [302], "ready", 302),
        ("/302", [200], "unready", 302),
        ("/slow", [200], "unready", None),
    ],
)
def test_http_probe(monkeypatch, origin, path, statuses, state, status):
    port, calls = origin
    item = workload(port, readiness_path=path, expected_status=statuses)
    result = docker_state(monkeypatch).observe(item, "test-node")
    assert result["state"] == state
    assert result["http_status"] == status
    assert result["node"] == "test-node"
    assert calls == [path]


@pytest.mark.parametrize(
    "running,health,state", [(False, "none", "stopped"), (True, "unhealthy", "unready")]
)
def test_container_gates_readiness(monkeypatch, origin, running, health, state):
    port, calls = origin
    result = docker_state(monkeypatch, running, health).observe(workload(port), "node")
    assert result["state"] == state
    if not running:
        assert not calls


def test_refused_connection(monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        item = workload(sock.getsockname()[1])
        result = docker_state(monkeypatch).observe(item, "node")
    assert result["state"] == "unready"
    assert result["http_status"] is None


def test_missing_container_is_stopped(monkeypatch):
    docker = Docker()
    monkeypatch.setattr(docker, "run", lambda args, timeout=None: "")
    assert docker.observe(workload(1), "node")["state"] == "stopped"
