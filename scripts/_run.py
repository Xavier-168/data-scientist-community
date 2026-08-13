#!/usr/bin/env python3
"""Pure-Python application launcher."""
import os
import sys

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)

import runner
runner._start_server()
