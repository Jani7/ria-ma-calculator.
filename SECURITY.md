# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it responsibly.

**Email:** dhruvjani7@gmail.com

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

- No user data is stored persistently; all inputs are session-scoped
- No authentication tokens, API keys, or secrets are used in the application
- Dependencies are pinned to exact versions and reviewed before updates
- All HTML rendering uses hardcoded templates only (no user-generated HTML)
