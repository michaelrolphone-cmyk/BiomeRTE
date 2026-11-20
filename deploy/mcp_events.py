"""Compatibility module for the shared mcp_events implementation."""
import sys
from biomerte import mcp_events as _implementation
from biomerte.mcp_events import *
sys.modules[__name__] = _implementation
