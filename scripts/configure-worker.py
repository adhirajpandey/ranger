"""Render an ignored Wrangler configuration, without calling Cloudflare."""

import argparse
import json
from pathlib import Path

from ranger.config import cron_schedule, validate_cluster_config

parser = argparse.ArgumentParser()
parser.add_argument("cluster", type=Path)
parser.add_argument("--output", type=Path, default=Path("wrangler.local.jsonc"))
args = parser.parse_args()
config = validate_cluster_config(json.loads(args.cluster.read_text()))
wrangler = json.loads(Path("wrangler.jsonc").read_text())
services = []
for name, node in config["nodes"].items():
    service_id = node.pop("vpc_service_id", None)
    if not service_id or service_id == "REPLACE_WITH_VPC_SERVICE_ID":
        raise SystemExit(f"configure the real VPC Service ID for node {name} before rendering")
    services.append({"binding": node["agent_binding"], "service_id": service_id})
wrangler["vpc_services"] = services
wrangler["vars"] = {"CLUSTER_CONFIG": json.dumps(config)}
wrangler["triggers"]["crons"] = [cron_schedule(config["policy"])]
args.output.write_text(json.dumps(wrangler, indent=2) + "\n")
print(args.output)
