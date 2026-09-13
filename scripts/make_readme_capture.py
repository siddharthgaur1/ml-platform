"""Render docs/drift-demo.svg from a real run of examples/fraud_detection_example.py.

    pip install -e ".[dev]"
    python scripts/make_readme_capture.py

Synthetic data with drift injected on purpose; no services or keys needed.
Only stdout is captured (the JSON alert lines go to stderr).
"""
import io
import os
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text

ROOT = Path(__file__).resolve().parents[1]
env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
out = subprocess.run([sys.executable, "examples/fraud_detection_example.py"], cwd=ROOT, env=env,
                     capture_output=True, text=True, encoding="utf-8", check=True).stdout

console = Console(record=True, width=104, file=io.StringIO())
console.print(Text("$ python examples/fraud_detection_example.py", style="bold green"))
console.print(Text(out.rstrip()))
console.save_svg(str(ROOT / "docs" / "drift-demo.svg"), title="ml-platform drift demo")
print("wrote docs/drift-demo.svg")
