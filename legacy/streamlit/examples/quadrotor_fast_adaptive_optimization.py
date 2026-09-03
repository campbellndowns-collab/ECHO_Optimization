#!/usr/bin/env python3
"""Legacy CLI shim — numerical engine lives in ``optimizer.propulsion.guide``."""

from optimizer.propulsion.guide import main

if __name__ == "__main__":
    raise SystemExit(main())
