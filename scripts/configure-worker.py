"""Render an ignored Wrangler configuration, without calling Cloudflare."""

import argparse
import json
import re
from pathlib import Path

from ranger.config import cron_schedule, validate_cluster_config

parser = argparse.ArgumentParser()
parser.add_argument("cluster", type=Path)
parser.add_argument("--output", type=Path, default=Path("wrangler.local.jsonc"))
parser.add_argument("--notifications", type=Path, help="private JSON with from and to addresses")
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
if args.notifications:
    addresses = json.loads(args.notifications.read_text())
    if not isinstance(addresses, dict) or set(addresses) != {"from", "to"}:
        raise SystemExit("notifications must contain only from and to")
    atom = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    label = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    address = re.compile(rf"{atom}(?:\.{atom})*@{label}(?:\.{label})+")
    if any(
        not isinstance(value, str) or not address.fullmatch(value) for value in addresses.values()
    ):
        raise SystemExit("notifications require unquoted ASCII from and to email addresses")
    wrangler["send_email"] = [{"name": "FAILURE_EMAIL", "destination_address": addresses["to"]}]
    wrangler["vars"].update(FAILURE_EMAIL_FROM=addresses["from"], FAILURE_EMAIL_TO=addresses["to"])
    wrangler["secrets"]["required"].append("ALERT_TEST_TOKEN")
wrangler["triggers"]["crons"] = [cron_schedule(config["policy"])]
args.output.write_text(json.dumps(wrangler, indent=2) + "\n")
print(args.output)
