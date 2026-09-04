# Contributing

Last Bell is a small tool with a large promise: a parent's password and
their children's grades stay on a computer the parent owns. Everything
below follows from that.

## The most useful things you can send

- **A district report.** Last Bell is verified against one district
  (MCPS) and built to work anywhere the Synergy PXP2 portal runs. If you
  are somewhere else, run the preflight and open an issue with its
  output. It is redacted by construction, so it carries no names, grades,
  or usernames:

  ```bash
  pipx run lastbell preflight --district your-portal-host --username you --report
  ```

  A `partial` verdict is the most fixable kind: the data path answers and
  one parser needs a tweak.
- **A bug report with `lastbell status` pasted in.** It shows the version,
  the service state, the last successful check, and who is subscribed to
  whom, with students as initials and no password. Trim the email
  addresses if you'd rather.
- **A question or an idea** in
  [Discussions](https://github.com/noestudios/lastbell/discussions).
  Anything touching a credential or a student's data goes through the
  private path in [SECURITY.md](SECURITY.md) instead.

## Working on the code

```bash
git clone https://github.com/noestudios/lastbell && cd lastbell
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # the suite runs offline, in a few seconds
ruff check .
```

`lastbell seed-demo --db data/demo.db` fabricates a family so you can work
on the dashboard (`lastbell dashboard --db data/demo.db`) without touching a
real portal. `scripts/build_demo_site.py` renders the same data to the
static demo site; `scripts/render_emails.py` writes the emails as HTML.

## Ground rules

- **Python 3.9 is the floor.** No `match`, no nested same-quote f-strings,
  no syntax newer than 3.9. CI runs 3.9 through 3.14.
- **No new outbound HTTP without a README change.** The README lists every
  place the code talks to the network. If a change adds one, the list
  changes in the same pull request, and the feature is off unless the
  owner turns it on.
- **Messages stay low-PII.** A notification carries initials, a course,
  and an assignment. Never a name.
- **The dashboard is stdlib.** No web framework, no build step, no
  external assets.
- **Every user-facing failure is one plain sentence with the next step.**
  A traceback is a bug report.
- **Tests come with the change.** The dashboard has an in-process harness
  (see `tests/test_hardening.py`), the service layer runs against a fake
  home with every subprocess recorded, and nothing in the suite touches a
  real keyring, portal, or network.

## Releases

Maintainer's job: bump the version in `pyproject.toml` and
`lastbell/__init__.py`, add a plain-words section to `CHANGELOG.md`, tag
`vX.Y.Z`. The tag builds, publishes to PyPI through trusted publishing
with a PEP 740 attestation, and creates the GitHub Release from the
changelog section.
