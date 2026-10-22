"""Use an explicit synthetic owner for discovery and its child processes."""

import os
from pathlib import Path

os.environ["GSD_OWNER_CONFIG"] = str(Path(__file__).with_name("owner.json"))
