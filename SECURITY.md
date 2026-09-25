# Security Policy

## Supported versions

Only the latest release receives security fixes. The tool includes a
self-update feature; please update before reporting.

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately through GitHub:
<https://github.com/yaviral17/macos-rufus/security/advisories/new>

Include a description, reproduction steps, and the affected version. You can
expect an initial response within 7 days. Once a fix is released, you will be
credited unless you prefer to stay anonymous.

## Scope

Of particular interest: disk-selection logic that could erase the wrong drive,
privilege-elevation handling, ISO download/verification, and the self-update
mechanism.
