"""Shared HTTP readiness policy for agents and the controller."""

from urllib.parse import urlsplit


def configure_readiness(workload):
    if "health_path" in workload:
        raise ValueError("health_path is obsolete; migrate to readiness_path and expected_status")
    path = workload.setdefault("readiness_path", "/")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or any(c.isspace() or ord(c) < 32 for c in path)
        or any(c in path for c in "?#\\")
        or urlsplit(path).netloc
    ):
        raise ValueError("readiness_path must be a local path without query or fragment")
    statuses = workload.setdefault("expected_status", [200])
    if (
        not isinstance(statuses, list)
        or not statuses
        or any(type(status) is not int or not 200 <= status <= 599 for status in statuses)
    ):
        raise ValueError("expected_status must be a nonempty list of HTTP statuses 200..599")
