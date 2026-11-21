"""
DataKazina API client.

Install dependency:
    pip install requests

Set your API key before running:

Windows PowerShell:
    $env:DATAKAZINA_API_KEY="your_new_api_key"

Linux/macOS:
    export DATAKAZINA_API_KEY="your_new_api_key"
"""

import json
import os
import sys
from typing import Any

import requests

BASE_URL = os.getenv(
    "DATAKAZINA_BASE_URL",
    "https://reseller.dakazinabusinessconsult.com/api/v1",
).rstrip("/")

ENDPOINT = f"{BASE_URL}/fetch-networks"
TIMEOUT_SECONDS = 30


def fetch_networks() -> Any:
    """Fetch all supported DataKazina mobile networks."""
    api_key = "dk_KOucd2evniMWSNXEtYiN9GxhTSZn78gd"

    if not api_key:
        raise RuntimeError(
            "DATAKAZINA_API_KEY is not set. Add it to your environment "
            "before running this file."
        )

    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
    }

    response = requests.get(
        ENDPOINT,
        headers=headers,
        timeout=TIMEOUT_SECONDS,
    )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        message = response.text.strip() or "<empty response body>"
        raise RuntimeError(
            f"DataKazina returned HTTP {response.status_code}: {message}"
        ) from exc

    if not response.content:
        return {
            "success": True,
            "status_code": response.status_code,
            "message": "The API returned an empty response body.",
        }

    try:
        return response.json()
    except requests.JSONDecodeError as exc:
        raise RuntimeError(
            f"The API response was not valid JSON: {response.text}"
        ) from exc


def main() -> None:
    try:
        networks = fetch_networks()
        print(json.dumps(networks, indent=2, ensure_ascii=False))
    except requests.Timeout:
        print("Request timed out. Please try again.", file=sys.stderr)
        raise SystemExit(1)
    except requests.RequestException as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
