"""otc console shim — call the CLI from a source checkout:

  uv run otc.py --url https://example.com --task "..." --budget 120

(When the project is pip-installed, pyproject [project.scripts] provides
the `otc` / `open-typesafe-camoufox` commands instead.)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.run.run import cli

if __name__ == "__main__":
    cli()
