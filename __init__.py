import logging
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

history_nodes = importlib.import_module(".history_nodes", package=__name__)
history_routes = importlib.import_module(".history_routes", package=__name__)

NODE_CLASS_MAPPINGS = history_nodes.NODE_CLASS_MAPPINGS
NODE_DISPLAY_NAME_MAPPINGS = history_nodes.NODE_DISPLAY_NAME_MAPPINGS
register_routes = history_routes.register_routes

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

# Register routes on import (if PromptServer is available)
try:
    register_routes()
except Exception:
    logging.exception("Failed to register history routes.")
