# Security

Last Bell holds a parent's school-portal password and children's grades.
If you find a way it could leak either, please tell us privately first.

## Reporting

Use GitHub's private vulnerability reporting: **Security → Report a
vulnerability** on <https://github.com/noestudios/lastbell/security>.
It opens a private thread with the maintainer; nothing is public until a
fix is out. Expect an acknowledgement within a week.

Please don't open a public issue for anything that could expose a
credential or a student's data.

## What counts

Anything that breaks a promise in the README's
[Credentials & student data](README.md#credentials--student-data-the-actual-guarantees)
section: a credential reaching anywhere but the OS keyring, the
owner-only settings file, and the district's own servers; student data
leaving the machine other than through a channel the owner configured;
a way for another machine, or a web page in the owner's browser, to read
the dashboard without the network key; or personal data reaching the
preflight's shareable report.

Also in scope: dependency vulnerabilities that this project actually
exercises, and anything in the setup wizard or `lastbell forget` that
would leave a secret behind when it says it didn't.

## Out of scope

Weaknesses in ParentVUE, Synergy, or Canvas themselves (report those to
the vendor or district); a dashboard the owner deliberately bound to a
public address and overrode the refusal for; physical access to the
machine running Last Bell.

## Supported versions

The latest release on PyPI. Fixes ship as a new release, noted in
[CHANGELOG.md](CHANGELOG.md) with enough detail to judge whether you
were affected.
