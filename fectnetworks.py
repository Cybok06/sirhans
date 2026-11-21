"""Fetch DataKazina networks using checkout.py's configuration.

Run: python fectnetworks.py
Read the config defaults without importing checkout and connecting to MongoDB.
Environment overrides match checkout.py. No purchases are made.
"""

import ast
import os
from pathlib import Path
import sys

import requests


def checkout_config():
    source = Path(__file__).with_name("checkout.py").read_text(encoding="utf-8")
    defaults = {}
    names = {"DATAKAZINA_BASE_URL", "DATAKAZINA_API_KEY", "DATAKAZINA_TIMEOUT"}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in names:
                value = node.value
                if target.id == "DATAKAZINA_TIMEOUT":
                    value = value.args[0]
                defaults[target.id] = ast.literal_eval(value.args[1])
    return {name: os.getenv(name, defaults[name]) for name in names}


def main():
    config = checkout_config()
    key = config["DATAKAZINA_API_KEY"].strip().strip('"').strip("'")
    if not key:
        print("DataKazina API key is missing.", file=sys.stderr)
        return 1
    url = config["DATAKAZINA_BASE_URL"].rstrip("/") + "/fetch-networks"
    try:
        response = requests.get(
            url,
            headers={"x-api-key": key, "Accept": "application/json"},
            timeout=int(config["DATAKAZINA_TIMEOUT"]),
        )
        response.raise_for_status()
        payload = response.json()
        networks = payload if isinstance(payload, list) else payload.get("data")
        if not isinstance(networks, list) or not all(isinstance(row, dict) for row in networks):
            raise ValueError("Unexpected networks response")
    except (requests.RequestException, ValueError, AttributeError) as exc:
        print(f"Could not fetch networks: {exc}", file=sys.stderr)
        return 1
    print(f"Fetched networks from {url}\n")
    print(f"{'ID':<6}Network")
    for network in networks:
        print(f"{str(network.get('id', '')):<6}{network.get('name', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
