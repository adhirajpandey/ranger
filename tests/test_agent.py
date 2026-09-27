import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ranger.agent import Agent, Docker, handler_for

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
        return {"node": node, "state": "ready" if self.running else "stopped"}

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
        steps = (("start", "ready"), ("start", "ready"), ("stop", "stopped"), ("stop", "stopped"))
        for action, desired in steps:
            request(f"/workloads/test/{action}", "POST")
            eventually(
                lambda: (
                    request("/workloads/test/health")["state"] == desired
                    and request("/workloads/test/health")["operation"]["pending"] is None
                )
            )
        with pytest.raises(urllib.error.HTTPError) as error:
            request("/workloads", "GET")
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_agent_contract():
    exercise_agent(Agent({"node": "test-node", "workloads": {"test": {}}}, FakeDocker()))


def test_conflicting_action_is_rejected_while_one_is_pending():
    release = threading.Event()

    class SlowDocker(FakeDocker):
        def change(self, item, action):
            release.wait(10)
            super().change(item, action)

    agent = Agent({"node": "test-node", "workloads": {"test": {}}}, SlowDocker())
    assert agent.change("test", "start") == 202
    assert agent.change("test", "start") == 202
    assert agent.change("test", "stop") == 409
    release.set()
    eventually(lambda: agent.observe("test")["operation"]["pending"] is None)
    assert agent.observe("test")["state"] == "ready"


@pytest.mark.skipif(os.environ.get("RUN_DOCKER_TESTS") != "1", reason="opt-in Docker fixture")
def test_real_compose_agent():
    item = {
        "compose_file": str(Path(__file__).parent / "fixture/compose.yaml"),
        "project": f"ranger-test-{os.getpid()}",
        "service": "app",
        "port": 16740,
        "readiness_path": "/",
        "expected_status": [200],
        "pull": "missing",
    }
    docker = Docker()
    try:
        exercise_agent(Agent({"node": "test-node", "workloads": {"test": item}}, docker))
    finally:
        docker.run([*docker.compose(item), "down", "--remove-orphans"])
