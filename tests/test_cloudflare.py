from habitat_failover.cloudflare import CloudflareIO


async def test_dns_uses_only_configured_record_id():
    calls = []

    async def request(url, **kwargs):
        calls.append((url, kwargs))
        return {"success": True, "result": {"content": "target"}}

    io = CloudflareIO({}, request, "token", {})
    spec = {"zone_id": "zone-id", "record_id": "record-id", "hostname": "unused.example.com"}
    await io.dns(spec)
    await io.set_dns(spec, "target")
    assert [url for url, _ in calls] == [
        "https://api.cloudflare.com/client/v4/zones/zone-id/dns_records/record-id"
    ] * 2
    assert calls[1][1]["method"] == "PATCH"
    assert calls[1][1]["body"] == {"content": "target"}
