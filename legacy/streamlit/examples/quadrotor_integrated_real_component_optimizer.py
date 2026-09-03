#!/usr/bin/env python3
"""Legacy CLI shim — numerical engine lives in ``optimizer.propulsion.integrated``."""

from optimizer.propulsion.integrated import main

if __name__ == "__main__":
    raise SystemExit(main())
