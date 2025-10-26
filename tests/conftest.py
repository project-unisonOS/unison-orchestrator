import os
import sys

# Add service root to sys.path so 'from src.server import app' works when running tests from repo root
CURRENT_DIR = os.path.dirname(__file__)
SERVICE_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, os.pardir))
if SERVICE_ROOT not in sys.path:
    sys.path.insert(0, SERVICE_ROOT)
