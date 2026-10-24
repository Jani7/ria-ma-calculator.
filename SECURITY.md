# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it responsibly.

**Preferred:** use GitHub's private vulnerability reporting for this repository —
[Report a vulnerability](https://github.com/Jani7/ria-ma-calculator./security/advisories/new).

If that page is unavailable, [open an issue](https://github.com/Jani7/ria-ma-calculator./issues/new)
asking for a private contact channel — please do not include vulnerability details
in the public issue itself.

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact

We will acknowledge receipt within 48 hours and provide a timeline for a fix.

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest  | Yes       |

## Security Practices

- Dependencies are pinned to exact versions and reviewed before updates
- All HTML rendering uses hardcoded templates only (no user-generated HTML)
- Secrets are read from Streamlit secrets management, never committed to the repo

## What data this app handles

By default the calculator is **stateless**: every input lives in your browser
session and is gone when you close the tab. Two optional features change that,
and neither is on unless you use it.

**Share links.** Choosing "Copy share link" encodes the deal inputs you entered
into the URL itself (compressed, not encrypted). Nothing is sent to a database —
but anyone who receives, forwards, or logs that URL can reconstruct the full
deal. Treat a share link like the deal file.

**Saved deals.** If this deployment is configured with Supabase credentials, the
"Save this deal" flow stores your sign-in email, the name you give the deal, and
the deal inputs in a hosted Postgres database, so you can reload them later.
Rows are scoped to your email address at the application layer. To have saved
data removed, email the address in the Reporting section below.

If this deployment has no Supabase credentials configured, sign-in and saved
deals are unavailable and no server-side storage occurs at all.

**Third-party data access.** The SEC Form ADV lookup fetches a dataset from a
private GitHub repository using a read-only fine-grained access token held in
Streamlit secrets. The token is never exposed to the browser. Firm searches are
executed locally against that dataset — search terms are not transmitted to any
third party.

## Scope and limitations

This is a modeling tool, not a system of record. It carries no uptime
commitment, no formal data-retention policy, and no contractual security
obligations. Do not enter information you are not permitted to place in a
third-party hosted tool, and check your own firm's policies first.
