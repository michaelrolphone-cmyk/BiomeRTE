"""Build the pinned deployed SLAM source and record actual Linux dependencies."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT/"providers/robot-vision"))
from setup_slam import Installer, sha256

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--jobs", type=int, default=2)
args = parser.parse_args()
if args.jobs < 1:
    parser.error("--jobs must be positive")
installer = Installer("/opt/vision", "/opt/conda/bin/conda", args.jobs, "linux-cpu")
for action in (installer.checkout, installer.patches, installer.native,
               installer.python_packages, installer.vocabulary):
    action()
vocabulary = installer.source/"data/ORBvoc.txt"
expected = "f8dd027f7a6cb88129821341194d7f2c75b77b3394257ddd0d2229863d1a3570"
if sha256(vocabulary) != expected:
    raise RuntimeError("ORB vocabulary differs from the installed source's verified artifact")
installer.smoke()
print(json.dumps({"profile":"linux-cpu", "receipt":str(installer.receipt_path)}))
