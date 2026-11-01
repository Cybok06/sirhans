# BundlePortal webhook setup for Sirhans

1. Deploy this updated application to **sirhans.com** first.
2. In this project folder, run:

   ```powershell
   python setup_bundleportal_webhook.py
   ```

   The script uses `BUNDLE_PORTAL_KEY` or `BUNDLEPORTAL_API_KEY` from your environment, or asks for the key with hidden input. It calls the v2 `set_webhook` action for **https://sirhans.com/webhooks/bundleportal** and saves the one-time signing secret in `.env.bundleportal-webhook`. It does not place orders.

3. Open that private file and copy the `BUNDLE_PORTAL_WEBHOOK_SECRET` value into your hosting service's environment settings. Keep the application's existing API key configured too. Restart/redeploy the application. The server reads environment variables; saving the file on your computer alone does not configure the live server.
4. Verify the public receiver and deployed secret:

   ```powershell
   python setup_bundleportal_webhook.py --test
   ```

   This sends an authenticated configuration probe. It does not create or change an order.

Use `--status` to inspect the current registration without changing it. BundlePortal never returns an existing secret. If a webhook was already registered and its secret is unavailable, use `--rotate`, then deploy the newly saved secret and run `--test` again. Rotation changes the provider's secret immediately, so complete the server update promptly; deliveries are retried by BundlePortal while the secrets differ. Existing local secret files are archived before replacement.

If the domain redirects this path, supply the direct HTTPS URL with `--url`. The registration script rejects redirects and unexpected HTML responses so BundlePortal is not configured against a landing page or an inaccessible endpoint.

## Status handling

- Signature: HMAC-SHA256 over the exact raw request body, using `X-BundlePortal-Signature`.
- Delivery retries are deduplicated using `X-BundlePortal-Delivery`, with a body hash fallback for older callbacks.
- Valid callbacks are saved in MongoDB before the application acknowledges them. Background processing updates the matching order line and parent order. The existing three-minute scheduler recovers interrupted work and callbacks that arrived before their order record.
- Matching uses the provider request order ID, network, and recipient. MTN, MTN2, MTN3, Telecel, AT iShare, and legacy `bundleportal` lines are supported.
- `completed` becomes Delivered. Failed, cancelled, and refunded callbacks retain the provider status and failure reason and mark the line Failed/refund-required. This feature updates statuses; it does not automatically credit customer wallets.
- A settlement timestamp prevents an older refund callback from overwriting a newer outcome. Initial purchase replies cannot overwrite a final webhook result.
- BundlePortal order status is not polled. No live purchase is needed for the configuration test.
