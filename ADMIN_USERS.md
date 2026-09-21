# Admin users and page access

Open **Users** in the main admin sidebar (`/admin/users`). Enter the user's name,
username, email and password, select their pages, and create the account. The user
signs in through the existing login page and lands on their first permitted page.
For example, selecting only Orders and Services gives them those two sidebar links
on desktop and mobile. Logout is always available.

Use **Edit** to change account details, page access, or set a new password. Leave
the password fields empty to preserve the current password. **Disable** blocks
login and invalidates existing sessions; **Enable** allows a fresh login.
Password resets also invalidate existing sessions.

Page access includes the page's existing operations (such as changing orders or
editing services), not just viewing it. Provider selection is an exception: only
full administrators can choose or change service providers. Managed users cannot
submit provider changes, including when creating services; their new services
use the existing default provider (Portal-02). Page permissions are checked from
the database on every admin request. Direct URLs and action endpoints for other
pages return 403. Users and Admin Profile are reserved for full administrators.
Existing full administrators retain their access.

## Implementation

- `admin_access.py`: shared page catalog, landing selection, sidebar helpers and
  central request guard. Register new assignable pages here with their blueprint.
  A managed account is denied unrecognized endpoints by default. Any cross-page
  API dependency must be explicitly mapped in the guard.
- `admin_users.py`: validated creation, updates, password hashing, account status
  and CSRF protection for user management.
- `templates/admin_users.html`: creation/edit form and account list.
- New accounts use the existing `users` collection and `role: admin` for
  compatibility with existing page guards. `managed_admin: true` and
  `allowed_pages` identify restricted accounts; an empty or malformed permission
  list grants no pages. Never remove both fields to revoke access: disable the
  account instead.
- `access_version` invalidates old sessions after a password reset or a status
  change. Records and sessions without this field default to zero.

No database migration is required. Deploy the application files together and
restart the app. No real user accounts are created by deployment.

## Verification

Install test dependencies with `python -m pip install -r requirements-test.txt`.
Run `python -m unittest discover -s tests -p test_admin_users.py` for the isolated
in-memory account and access-control tests. They do not connect to the application
database.
