"""Render an ignored Wrangler configuration, without calling Cloudflare."""

import argparse
import json
from pathlib import Path

from habitat_failover.controller import validate_config

parser = argparse.ArgumentParser()
parser.add_argument("cluster", type=Path)
parser.add_argument("--bootstrap", action="store_true")
parser.add_argument("--output", type=Path, default=Path("wrangler.local.jsonc"))
args = parser.parse_args()
config = validate_config(json.loads(args.cluster.read_text()))
wrangler = json.loads(Path("wrangler.jsonc").read_text())
services = []
for node in config["nodes"].values():
    service_id = node.pop("vpc_service_id")
    if service_id == "REPLACE_WITH_VPC_SERVICE_ID":
        raise SystemExit("configure the real VPC Service IDs before rendering")
    services.append({"binding": node["agent_binding"], "service_id": service_id})
wrangler["vpc_services"] = services
wrangler["vars"] = {"CLUSTER_CONFIG": json.dumps(config)}
wrangler["triggers"]["crons"] = ["* * * * *"] if args.bootstrap else []
args.output.write_text(json.dumps(wrangler, indent=2) + "\n")
print(args.output)
