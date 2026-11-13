"""Launch the calibration GUI using a portable or development Python runtime."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from leanwatts_can.gui import main

raise SystemExit(main())
