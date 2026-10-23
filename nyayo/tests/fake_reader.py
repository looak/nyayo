"""A stand-in for phant-indexer in the tests: the same command line and output, from python-chess."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if sys.argv[1:] == ["--version"]:
    print("phant-indexer 9.9.9 format 2")
    sys.exit(0)

from nyayo.matches.reader import PythonChess  # noqa: E402

data = sys.stdin.buffer.read()
if data.startswith(b"FAIL"):
    print("can't decompress", file=sys.stderr)
    sys.exit(1)
for record in PythonChess().read(data):
    print(json.dumps({"format": 2, "reader": "9.9.9", **record}))
