"""Render an ignored Wrangler configuration, without calling Cloudflare."""

import argparse
import json
from pathlib import Path

from ranger.config import validate_cluster_config

parser = argparse.ArgumentParser()
parser.add_argument("cluster", type=Path)
parser.add_argument("--output", type=Path, default=Path("wrangler.local.jsonc"))
args = parser.parse_args()
config = validate_cluster_config(json.loads(args.cluster.read_text()))
wrangler = json.loads(Path("wrangler.jsonc").read_text())
wrangler["vpc_services"] = [
    {"binding": node["agent_binding"], "service_id": node["vpc_service_id"]}
    for node in config["nodes"].values()
]
wrangler["vars"] = {"CLUSTER_CONFIG": json.dumps(config)}
if "notifications" in config:
    # The binding can only send to the configured recipient.
    to = config["notifications"]["to"]
    wrangler["send_email"] = [{"name": "EMAIL", "destination_address": to}]
args.output.write_text(json.dumps(wrangler, indent=2) + "\n")
print(args.output)
