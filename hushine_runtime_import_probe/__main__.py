from __future__ import annotations

import sys

from hushine_runtime_import_probe.protocol import _child_main

raise SystemExit(_child_main(sys.argv[1:]))
