#!/usr/bin/env python3
"""Compatibility shim.

The Canvas recon spike (2026-09-03) has graduated into the ``lastbell``
package as ``lastbell/canvas.py``; the read-only check it performed is now
``lastbell canvas``. This shim just forwards to it.

Equivalent to:  lastbell canvas
"""
import sys

from lastbell.cli import main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "canvas"]
    main()
