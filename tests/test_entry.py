"""Exercise the actual Worker transport with a minimal runtime boundary."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def transport(monkeypatch):
    fetch = AsyncMock()
    js = ModuleType("js")
    js.AbortSignal = SimpleNamespace(timeout=lambda milliseconds: milliseconds)
    js.Object = SimpleNamespace(fromEntries=None)
    js.Request = SimpleNamespace(new=lambda url, options: (url, options))
    js.fetch = fetch
    ffi = ModuleType("pyodide.ffi")
    ffi.to_js = lambda options, **kwargs: options
    workers = ModuleType("workers")
    for name in ("DurableObject", "Request", "Response", "WorkerEntrypoint"):
        setattr(workers, name, type(name, (), {}))
    for name, module in (("js", js), ("pyodide.ffi", ffi), ("workers", workers)):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "entry_test", Path(__file__).parents[1] / "src/entry.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cluster = object.__new__(module.Cluster)
    cluster.config = {"policy": {"probe_timeout": 5}}
    return cluster, fetch


@pytest.mark.parametrize("status", [200, 302, 401, 503])
async def test_status_transport_never_parses_body(transport, status):
    cluster, fetch = transport
    response = SimpleNamespace(
        status=status,
        body=SimpleNamespace(cancel=AsyncMock()),
        text=AsyncMock(side_effect=AssertionError("body must not be parsed")),
    )
    fetch.return_value = response
    assert await cluster.request("https://example.com/", status_only=True) == {
        "http_status": status
    }
    options = fetch.call_args.args[0][1]
    assert options["redirect"] == "manual"
    assert options["signal"] == 5000
    response.body.cancel.assert_awaited_once()


async def test_management_transport_retains_json_and_success_requirement(transport):
    cluster, fetch = transport
    fetch.return_value = SimpleNamespace(status=200, text=AsyncMock(return_value='{"ok": true}'))
    assert await cluster.request("https://example.com/api", body={"content": "target"}) == {
        "ok": True
    }
    options = fetch.call_args.args[0][1]
    assert options["redirect"] == "error"
    assert json.loads(options["body"]) == {"content": "target"}
    fetch.return_value.status = 401
    with pytest.raises(RuntimeError):
        await cluster.request("https://example.com/api")
