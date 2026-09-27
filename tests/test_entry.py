"""Exercise the actual Worker transport with a minimal runtime boundary."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
    for name in ("DurableObject", "Request", "WorkerEntrypoint"):
        setattr(workers, name, type(name, (), {}))
    workers.Response = lambda body, status=200, headers=None: SimpleNamespace(
        body=body, status=status, headers=headers
    )
    for name, module in (("js", js), ("pyodide.ffi", ffi), ("workers", workers)):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "entry_test", Path(__file__).parents[1] / "src/entry.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cluster = object.__new__(module.Cluster)
    cluster.config = {"policy": {"probe_timeout": 5}}
    cluster.entry_module = module
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
    assert options["redirect"] == "manual"
    assert json.loads(options["body"]) == {"content": "target"}
    fetch.return_value.status = 401
    with pytest.raises(RuntimeError):
        await cluster.request("https://example.com/api")


async def test_scheduled_run_cycles_the_named_cluster_at_its_scheduled_time(transport):
    cluster, _ = transport
    module = cluster.entry_module
    request = object()
    module.Request = Mock(return_value=request)
    stub = SimpleNamespace(fetch=AsyncMock())
    binding = SimpleNamespace(getByName=Mock(return_value=stub))
    handler = object.__new__(module.Default)
    handler.env = SimpleNamespace(CLUSTER=binding)

    await handler.scheduled(SimpleNamespace(scheduledTime=120_000), handler.env, SimpleNamespace())

    binding.getByName.assert_called_once_with("ranger-v1")
    module.Request.assert_called_once_with(
        "http://internal/cycle", method="POST", body=json.dumps({"now": 120.0})
    )
    stub.fetch.assert_awaited_once_with(request)


async def test_cycle_route_reports_failure(transport):
    cluster, _ = transport
    cluster.reconciler = SimpleNamespace(cycle=AsyncMock())
    request = SimpleNamespace(
        method="POST",
        url="http://internal/cycle",
        text=AsyncMock(return_value=json.dumps({"now": 120.0})),
    )
    assert (await cluster.fetch(request)).status == 200
    cluster.reconciler.cycle.assert_awaited_once_with(120.0)
    cluster.reconciler.cycle.side_effect = RuntimeError("unexpected")
    assert (await cluster.fetch(request)).status == 500
