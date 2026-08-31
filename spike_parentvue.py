#!/usr/bin/env python3
"""Compatibility shim.

The district recon spike has graduated into the ``gradewatch`` package. Its
hard-coded username/district defaults are gone — configuration now comes from a
git-ignored ``.env`` (see ``.env.example``), and the password from the OS
keyring. This shim just forwards to the packaged preflight so an existing
`python spike_parentvue.py` still works.

Equivalent to:  gradewatch preflight   (or: python -m gradewatch preflight)
"""
from gradewatch.preflight import main

if __name__ == "__main__":
    main()
