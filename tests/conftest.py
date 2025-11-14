import os
import sys

# Add service root to sys.path so 'from src.server import app' works when running tests from repo root
CURRENT_DIR = os.path.dirname(__file__)
SERVICE_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, os.pardir))
SRC_ROOT = os.path.join(SERVICE_ROOT, "src")
REPO_ROOT = os.path.abspath(os.path.join(SERVICE_ROOT, os.pardir))
COMMON_SRC = os.path.join(REPO_ROOT, "unison-common", "src")

for path in (SERVICE_ROOT, SRC_ROOT, COMMON_SRC):
    if path not in sys.path:
        sys.path.insert(0, path)
