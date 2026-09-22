"""Authenticated, allowlisted Compose control. Run with python -m habitat_failover.agent."""

import argparse
import hmac
import json
import os
import re
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*\Z")


def load_config(path):
    config = json.loads(Path(path).read_text())
    if not NAME.fullmatch(config["node"]):
        raise ValueError("invalid node")
    for name, item in config["workloads"].items():
        if not all(NAME.fullmatch(n) for n in (name, item["project"], item["service"])):
            raise ValueError("invalid workload, project or service")
        if not Path(item["compose_file"]).is_absolute():
            raise ValueError("compose_file must be absolute")
        if item.get("env_file") and not Path(item["env_file"]).is_absolute():
            raise ValueError("env_file must be absolute")
        if not 1 <= item["port"] <= 65535:
            raise ValueError("invalid port")
        if not item["health_path"].startswith("/") or "?" in item["health_path"]:
            raise ValueError("invalid health_path")
        if item.get("pull", "missing") not in {"never", "missing", "always"}:
            raise ValueError("invalid pull policy")
    return config


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
            # Docker diagnostics can contain registry credentials or environment values.
            raise RuntimeError(f"docker command exited with code {result.returncode}")
        return result.stdout

    @staticmethod
    def compose(item):
        args = ["compose", "-f", item["compose_file"], "-p", item["project"]]
        if item.get("env_file"):
            args.extend(["--env-file", item["env_file"]])
        return args

    def available(self):
        self.run(["info", "--format", "{{.ServerVersion}}"], self.probe_timeout)

    def observe(self, item, node):
        ids = self.run(
            [*self.compose(item), "ps", "--all", "--quiet", item["service"]],
            self.probe_timeout,
        ).split()
        result = {"running": False, "ready": False, "node": node, "release": None}
        if not ids:
            return result
        if len(ids) != 1:
            raise RuntimeError("v1 expects one container per service")
        state = json.loads(
            self.run(
                ["inspect", "--format", "{{json .State}}", ids[0]],
                self.probe_timeout,
            )
        )
        result["running"] = state["Running"]
        result["docker_health"] = state.get("Health", {}).get("Status", "none")
        if not result["running"]:
            return result
        url = f"http://127.0.0.1:{item['port']}{item['health_path']}"
        request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        try:
            # No proxy or redirects: readiness must come from the configured local origin.
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None

            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=self.probe_timeout) as response:
                health = json.loads(response.read(65536))
                result["ready"] = (
                    response.status == 200
                    and health.get("node") == node
                    and health.get("status") == "ok"
                    and result["docker_health"] in {"none", "healthy"}
                )
                result["release"] = health.get("release")
        except (OSError, ValueError):
            pass
        return result

    def change(self, item, action):
        if action == "start":
            args = [
                "up",
                "-d",
                "--no-build",
                "--no-deps",
                "--pull",
                item.get("pull", "missing"),
                item["service"],
            ]
        else:
            args = ["stop", "--timeout", "10", item["service"]]
        self.run([*self.compose(item), *args])


class Agent:
    def __init__(self, config, docker=None):
        self.config = config
        self.docker = docker or Docker()
        self.locks = {name: threading.Lock() for name in config["workloads"]}
        self.operations = {name: {"pending": None, "error": None} for name in self.locks}
        self.guard = threading.Lock()

    def health(self):
        try:
            self.docker.available()
            available = True
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            available = False
        return {
            "node": self.config["node"],
            "docker": available,
            "config_revision": self.config.get("revision", "unknown"),
        }

    def observe(self, name):
        item = self.config["workloads"][name]
        try:
            result = self.docker.observe(item, self.config["node"])
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            result = {"running": False, "ready": False, "error": type(exc).__name__}
        with self.guard:
            return {**result, "operation": dict(self.operations[name])}

    def change(self, name, action):
        lock = self.locks[name]
        if not lock.acquire(blocking=False):
            with self.guard:
                same_action = self.operations[name]["pending"] == action
            return 202 if same_action else 409
        with self.guard:
            self.operations[name] = {"pending": action, "error": None}

        def execute():
            try:
                item = self.config["workloads"][name]
                observation = self.docker.observe(item, self.config["node"])
                if observation["running"] != (action == "start"):
                    self.docker.change(item, action)
            except Exception as exc:
                with self.guard:
                    self.operations[name]["error"] = type(exc).__name__
            finally:
                with self.guard:
                    self.operations[name]["pending"] = None
                lock.release()

        threading.Thread(target=execute, daemon=True).start()
        return 202


def handler_for(agent, token):
    if len(token) < 32:
        raise ValueError("AGENT_TOKEN must contain at least 32 characters")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

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
                return self.reply(401, {"error": "unauthorized"})
            path = urlsplit(self.path).path
            if self.command == "GET" and path == "/health":
                return self.reply(200, agent.health())
            if self.command == "GET" and path == "/workloads":
                return self.reply(200, {name: agent.observe(name) for name in agent.locks})
            parts = path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "workloads" or parts[1] not in agent.locks:
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
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6720)
    args = parser.parse_args()
    agent = Agent(load_config(args.config))
    server = ThreadingHTTPServer(
        (args.host, args.port), handler_for(agent, os.environ["AGENT_TOKEN"])
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
