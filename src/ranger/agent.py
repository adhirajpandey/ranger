"""Authenticated, allowlisted Compose control. Installed as the ranger-agent command."""

import argparse
import hmac
import http.client
import json
import logging
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ranger.config import validate_agent_config

log = logging.getLogger("ranger.agent")
# Loopback only: the controller reaches the agent through the host's tunnel.
ADDRESS = ("127.0.0.1", 6720)


class Docker:
    def __init__(self, command_timeout=100, probe_timeout=5):
        self.command_timeout = command_timeout
        self.probe_timeout = probe_timeout

    def run(self, args, timeout=None):
        result = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout or self.command_timeout,
            check=False,
        )
        if result.returncode:
            # Docker diagnostics can contain registry credentials or environment values,
            # so they stay in the host journal and the controller receives only the code.
            log.error(
                "docker %s exited with code %d: %s",
                " ".join(args),
                result.returncode,
                result.stderr.strip()[-2000:],
            )
            raise RuntimeError(f"docker command exited with code {result.returncode}")
        return result.stdout

    @staticmethod
    def compose(item):
        return ["compose", "-f", item["compose_file"], "-p", item["project"]]

    def available(self):
        self.run(["info", "--format", "{{.ServerVersion}}"], self.probe_timeout)

    def observe(self, item, node):
        """Report the service as stopped, unready, or ready."""
        ids = self.run(
            [*self.compose(item), "ps", "--all", "--quiet", item["service"]],
            self.probe_timeout,
        ).split()
        result = {"node": node, "state": "stopped", "http_status": None}
        if not ids:
            return result
        if len(ids) != 1:
            raise RuntimeError("expected one container per service")
        container = json.loads(
            self.run(
                ["inspect", "--format", "{{json .State}}", ids[0]],
                self.probe_timeout,
            )
        )
        result["docker_health"] = container.get("Health", {}).get("Status", "none")
        if not container["Running"]:
            return result
        result["state"] = "unready"
        # http.client follows no redirects and uses no proxy, so the status is the origin's.
        connection = http.client.HTTPConnection(
            "127.0.0.1", item["port"], timeout=self.probe_timeout
        )
        try:
            connection.request("GET", item["readiness_path"], headers={"Cache-Control": "no-cache"})
            result["http_status"] = connection.getresponse().status
        except (OSError, http.client.HTTPException):
            return result
        finally:
            connection.close()
        if result["http_status"] in item["expected_status"] and result["docker_health"] in {
            "none",
            "healthy",
        }:
            result["state"] = "ready"
        return result

    def change(self, item, action):
        if action == "start":
            args = ["up", "-d", "--no-build", "--no-deps", "--pull", item["pull"]]
        else:
            args = ["stop", "--timeout", "10"]
        self.run([*self.compose(item), *args, item["service"]])


class Agent:
    def __init__(self, config, docker=None):
        self.config = config
        self.docker = docker or Docker()
        self.operations = {name: {"pending": None, "error": None} for name in config["workloads"]}
        self.guard = threading.Lock()

    def health(self):
        try:
            self.docker.available()
            available = True
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            available = False
        return {"node": self.config["node"], "docker": available}

    def observe(self, name):
        item = self.config["workloads"][name]
        try:
            result = self.docker.observe(item, self.config["node"])
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            result = {
                "node": self.config["node"],
                "state": "error",
                "http_status": None,
                "error": str(exc) or type(exc).__name__,
            }
        with self.guard:
            return {**result, "operation": dict(self.operations[name])}

    def change(self, name, action):
        """Run start or stop in the background. Compose skips an already satisfied action."""
        with self.guard:
            pending = self.operations[name]["pending"]
            if pending:
                return 202 if pending == action else 409
            self.operations[name] = {"pending": action, "error": None}

        def execute():
            error = None
            try:
                log.info("%s %s", action, name)
                self.docker.change(self.config["workloads"][name], action)
                log.info("%s %s completed", action, name)
            except Exception as exc:
                log.error("%s %s failed: %s", action, name, exc)
                error = str(exc) or type(exc).__name__
            with self.guard:
                self.operations[name] = {"pending": None, "error": error}

        threading.Thread(target=execute, daemon=True).start()
        return 202


def handler_for(agent, token):
    if len(token) < 32:
        raise ValueError("AGENT_TOKEN must contain at least 32 characters")

    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def log_message(self, *_):
            pass

        def reply(self, status, body):
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def dispatch(self):
            auth = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(auth, f"Bearer {token}".encode()):
                log.warning("rejected unauthenticated %s %s", self.command, self.path[:100])
                return self.reply(401, {"error": "unauthorized"})
            path = urlsplit(self.path).path
            if self.command == "GET" and path == "/health":
                return self.reply(200, agent.health())
            parts = path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "workloads" or parts[1] not in agent.operations:
                return self.reply(404, {"error": "unknown workload or route"})
            name, action = parts[1:]
            if self.command == "GET" and action == "health":
                return self.reply(200, agent.observe(name))
            if self.command == "POST" and action in {"start", "stop"}:
                return self.reply(agent.change(name, action), {"action": action})
            return self.reply(405, {"error": "method not allowed"})

        do_GET = dispatch
        do_POST = dispatch

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/ranger-agent/agent.json")
    args = parser.parse_args()
    # systemd adds timestamps when it writes stderr to the journal.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = validate_agent_config(json.loads(Path(args.config).read_text()))
    agent = Agent(config)
    server = ThreadingHTTPServer(ADDRESS, handler_for(agent, os.environ["AGENT_TOKEN"]))
    log.info(
        "agent for node %s listening on %s:%d with workloads %s",
        config["node"],
        *ADDRESS,
        ", ".join(config["workloads"]),
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
