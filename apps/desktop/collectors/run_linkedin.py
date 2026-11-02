"""Execute the pinned collector without shadowing its linkedin_cli dependency."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))

from linkedin_enrichment.cli import main

if __name__ == "__main__":
    main()
