Bundle Portal MTN setup for Sirhans
=================================

This integration adds API v2 routes for **MTN Normal**, **MTN Express**, **AT iShare**,
and **Telecel/Vodafone**.
The existing generic BundlePortal option and other networks retain their prior integration.

1. Set `BUNDLE_PORTAL_KEY` to the Sirhans Bundle Portal API key on your server.
   The existing `BUNDLEPORTAL_API_KEY` environment variable is also accepted.
2. Register Sirhans' public HTTPS callback URL with Bundle Portal using
   `set_webhook`: `https://<sirhans-domain>/webhooks/bundleportal`.
   Save the returned secret as `BUNDLE_PORTAL_WEBHOOK_SECRET` (or
   `BUNDLEPORTAL_WEBHOOK_SECRET`). Restart the application after configuring it.
3. In Admin Services, choose **Bundle Portal MTN**, **Bundle Portal MTN2**, or
   **Bundle Portal MTN3** independently for MTN Normal and MTN Express.
   For AT iShare, select **Bundle Portal AT iShare**. It uses the `airteltigo`
   catalogue and API network. AT Bigtime is a separate product and cannot use
   the iShare route. The same webhook and credentials serve all these routes.
   For Telecel or Vodafone services, select **Bundle Portal Telecel**. Both
   use Bundle Portal's `telecel` catalogue and API network.
   Keep the service type set to API. OFF services remain manual.
4. Ensure each service's advertised sizes exist in the selected route's catalogue.
   MTN, MTN2 and MTN3 have separate catalogues. Decimal GB sizes are preserved.

Dashboard, public index and store checkouts save the chosen route and a stable
provider order reference. The worker checks that route's catalogue and verifies
the recipient before making a purchase. Duplicate worker calls do not resubmit
an accepted order. Uncertain provider responses remain processing for review.

Completed, failed, cancelled and refunded results arrive through signed callbacks.
Completed becomes delivered. Provider failures/cancellations/refunds become
failed with a local refund-review flag; a provider refund does not imply that
the customer's local wallet has already been credited. New v2 MTN orders are
excluded from status polling. Received callbacks are stored in `bundleportal_events`.
Every three minutes, the existing status scheduler replays stored callbacks for
unfinished v2 items and repairs interrupted overall order updates. Items without
a callback keep their current status; finalised items are not checked again.
Unpaid USSD orders are excluded. Without incoming callbacks, the job cannot
discover a new provider status.

Use a separate Bundle Portal account/API key for Sirhans when nagonu already
has a webhook registered on its account. The documented `set_webhook` action
replaces the account's callback URL; registering Sirhans on the same account
would redirect nagonu's callbacks too. A shared account needs a common callback
that can route events between applications.

No credentials, webhook registration or paid orders are provisioned by these code changes.

Run isolated integration checks from this directory:

    python -m unittest discover -s tests -p test_bundle_portal_v2_mtn.py
