"""Compatibility module for the shared grok_receivers implementation."""
import sys
from biomerte import grok_receivers as _implementation
from biomerte.grok_receivers import *
sys.modules[__name__] = _implementation
