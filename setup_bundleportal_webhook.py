"""Register Sirhans' webhook and securely save the one-time signing secret.

Run: python setup_bundleportal_webhook.py
After setting the saved secret on the web server: python setup_bundleportal_webhook.py --test
This script never places an order or imports the application's database.
"""
import argparse
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

API_URL = 'https://api.bundleportal.com/v2'
DEFAULT_URL = 'https://sirhans.com/webhooks/bundleportal'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_json(url, payload=None, headers=None):
    body = json.dumps(payload, separators=(',', ':')).encode() if payload is not None else None
    request = Request(url, data=body, headers={'Accept': 'application/json', **(headers or {})},
                      method='POST' if body is not None else 'GET')
    try:
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise RuntimeError(f'URL redirects to {exc.headers.get("Location", "another URL")}. Use the direct HTTPS URL instead.') from None
        try:
            data = json.loads(exc.read())
        except (ValueError, UnicodeError):
            data = {}
        return exc.code, data
    except (URLError, TimeoutError, ValueError, UnicodeError):
        raise RuntimeError('No valid response was received. Check connectivity and use --status before repeating registration.') from None


def api_call(key, action, **fields):
    status, payload = http_json(API_URL, {'action': action, **fields},
                                {'x-api-key': key, 'Content-Type': 'application/json'})
    if status < 200 or status >= 300 or not isinstance(payload, dict) or payload.get('success') is not True:
        code = payload.get('code', 'request_failed') if isinstance(payload, dict) else 'invalid_response'
        raise RuntimeError(f'BundlePortal {action} failed (HTTP {status}, {code}).')
    return payload.get('data') or {}


def receiver_info(url):
    status, payload = http_json(url)
    if status != 200 or not isinstance(payload, dict) or payload.get('provider') != 'bundleportal' or payload.get('api_version') != 'v2':
        raise RuntimeError('Deploy the updated webhook receiver first. The target URL does not return the expected BundlePortal readiness response.')
    return payload


def read_secret(path):
    secret = os.getenv('BUNDLE_PORTAL_WEBHOOK_SECRET') or os.getenv('BUNDLEPORTAL_WEBHOOK_SECRET')
    if secret:
        return secret.strip()
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.startswith('BUNDLE_PORTAL_WEBHOOK_SECRET='):
                return line.partition('=')[2].strip()
    raise RuntimeError('No signing secret is available. Use the saved secret file or BUNDLE_PORTAL_WEBHOOK_SECRET.')


def test_receiver(url, secret):
    payload = {'event': 'webhook.test'}
    raw = json.dumps(payload, separators=(',', ':')).encode()
    signature = 'sha256=' + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    status, data = http_json(url, payload, {'Content-Type': 'application/json', 'X-BundlePortal-Signature': signature})
    if status != 200 or not isinstance(data, dict) or data.get('test') is not True or data.get('success') is not True:
        raise RuntimeError(f'Signed receiver test failed (HTTP {status}). Set the saved signing secret on the deployed server and restart it.')
    print('Signed webhook test passed. No order was created or modified.')


def register(key, url, output, rotate=False):
    """Reserve a writable private file before requesting the one-time secret."""
    if output.exists() and not rotate:
        raise RuntimeError(f'{output} already exists. Keep its secret, or use --rotate to explicitly replace the registration.')
    if not output.parent.is_dir():
        raise RuntimeError('The output directory does not exist.')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent,
                prefix=output.name + '.pending-', delete=False) as handle:
            temporary = Path(handle.name)
            data = api_call(key, 'set_webhook', webhook_url=url)
            secret = data.get('webhook_secret') if isinstance(data, dict) else None
            if not isinstance(secret, str) or not secret or '\n' in secret or '\r' in secret:
                raise RuntimeError('Registration did not return its signing secret. Use --status to inspect registration; --rotate explicitly requests a new secret.')
            handle.write(f'BUNDLE_PORTAL_WEBHOOK_SECRET={secret}\nBUNDLE_PORTAL_WEBHOOK_URL={url}\n')
            handle.flush()
            os.fsync(handle.fileno())
        if output.exists():
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            output.rename(output.with_name(output.name + '.previous-' + stamp))
        os.replace(temporary, output)
        temporary = None
    except OSError:
        if temporary and temporary.exists() and temporary.stat().st_size:
            raise RuntimeError(f'The secret is saved in {temporary}, but could not be moved to {output}. Keep that file.') from None
        raise RuntimeError('Could not prepare the private output file. Check its folder permissions.') from None
    finally:
        if temporary and temporary.exists() and temporary.stat().st_size == 0:
            temporary.unlink()
    print(f'Webhook registered: {url}')
    print(f'One-time signing secret saved to: {output}')
    print('Set BUNDLE_PORTAL_WEBHOOK_SECRET from that file in your deployed server environment, then restart/redeploy.')
    print('Then run: python setup_bundleportal_webhook.py --test')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--url', default=DEFAULT_URL, help='Direct public HTTPS webhook URL')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / '.env.bundleportal-webhook', help='Private signing-secret file')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--status', action='store_true', help='Inspect registration; no changes')
    action.add_argument('--test', action='store_true', help='Send a signed configuration test; no orders')
    action.add_argument('--rotate', action='store_true', help='Replace an existing registration and rotate its signing secret')
    args = parser.parse_args(argv)
    parsed = urlparse(args.url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        parser.error('The webhook URL must be a direct public HTTPS URL without credentials or a fragment.')
    try:
        if args.test:
            test_receiver(args.url, read_secret(args.output))
            return 0
        info = receiver_info(args.url)
        key = (os.getenv('BUNDLE_PORTAL_KEY') or os.getenv('BUNDLEPORTAL_API_KEY') or '').strip()
        if not key:
            key = getpass.getpass('BundlePortal API key (hidden): ').strip()
        if not key:
            raise RuntimeError('An API key is required.')
        existing = api_call(key, 'get_webhook')
        registered_url = existing.get('webhook_url') if isinstance(existing, dict) else None
        if args.status:
            print(f'Registered URL: {registered_url or "Not registered"}')
            print(f'Server signing secret configured: {bool(info.get("configured"))}')
            return 0
        if registered_url and not args.rotate:
            print(f'Webhook already registered: {registered_url}')
            print('Its secret cannot be read back. Keep the existing secret, or run with --rotate to register this URL and issue a new secret.')
            return 0 if registered_url == args.url and info.get('configured') else 1
        register(key, args.url, args.output.resolve(), rotate=args.rotate)
        return 0
    except (RuntimeError, KeyboardInterrupt, EOFError) as exc:
        print(str(exc) or 'Setup cancelled.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
