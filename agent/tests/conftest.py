import os
import sys

# Agent modules use flat imports (from repo_map import ...), same as the
# server does when run from agent/ — make them importable under pytest.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
