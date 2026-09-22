import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from habitat_failover.agent import Agent, Docker, handler_for

TOKEN = "test-token-" * 4


def eventually(predicate):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("condition did not converge")


class FakeDocker:
    running = False

    def available(self):
        pass

    def observe(self, item, node):
        return {"running": self.running, "ready": self.running, "node": node}

    def change(self, item, action):
        self.running = action == "start"


def exercise_agent(agent):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(agent, TOKEN))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, method="GET", token=TOKEN):
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}{path}",
            method=method,
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)

    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            request("/health", token="bad")
        assert error.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as error:
            request("/workloads/not-allowed/start", "POST")
        assert error.value.code == 404
        assert request("/health")["docker"]
        for action, desired in (("start", True), ("start", True), ("stop", False), ("stop", False)):
            request(f"/workloads/test/{action}", "POST")
            eventually(
                lambda: (
                    request("/workloads/test/health").get("running") == desired
                    and request("/workloads/test/health")["operation"]["pending"] is None
                )
            )
            eventually(lambda: request("/workloads/test/health")["ready"] == desired)
        assert not request("/workloads")["test"]["running"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_agent_contract():
    exercise_agent(Agent({"node": "test-node", "workloads": {"test": {}}}, FakeDocker()))


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("RUN_DOCKER_TESTS") != "1", reason="opt-in Docker fixture")
def test_real_compose_agent():
    item = {
        "compose_file": str(Path(__file__).parent / "fixture/compose.yaml"),
        "project": f"habitat-failover-test-{os.getpid()}",
        "service": "app",
        "port": 16740,
        "health_path": "/healthz",
        "pull": "missing",
    }
    docker = Docker()
    try:
        exercise_agent(Agent({"node": "test-node", "workloads": {"test": item}}, docker))
    finally:
        docker.run([*docker.compose(item), "down", "--remove-orphans"])
