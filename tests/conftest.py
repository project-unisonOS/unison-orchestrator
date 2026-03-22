import os
import sys

# Add service root to sys.path so 'from src.server import app' works when running tests from repo root
CURRENT_DIR = os.path.dirname(__file__)
SERVICE_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, os.pardir))
SRC_ROOT = os.path.join(SERVICE_ROOT, "src")
REPO_ROOT = os.path.abspath(os.path.join(SERVICE_ROOT, os.pardir))
WORKSPACE_COMMON_SRC = os.path.join(REPO_ROOT, "unison-common", "src")
TOPLEVEL_ROOT = os.path.abspath(os.path.join(REPO_ROOT, os.pardir))
TOPLEVEL_COMMON_SRC = os.path.join(TOPLEVEL_ROOT, "unison-common", "src")

sys.path[:] = [
    path for path in sys.path
    if os.path.abspath(path) != os.path.abspath(WORKSPACE_COMMON_SRC)
]

paths = [SERVICE_ROOT, SRC_ROOT]
if os.path.isdir(TOPLEVEL_COMMON_SRC):
    paths.append(TOPLEVEL_COMMON_SRC)
elif os.path.isdir(WORKSPACE_COMMON_SRC):
    paths.append(WORKSPACE_COMMON_SRC)

for path in reversed(paths):
    if path not in sys.path:
        sys.path.insert(0, path)

sys.modules.pop("unison_common", None)
