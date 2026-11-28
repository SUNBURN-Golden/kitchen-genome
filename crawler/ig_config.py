"""Instagram collection switch.

On by default (KG_INSTAGRAM_ENABLED=1). Set KG_INSTAGRAM_ENABLED=0 to skip
collection, login, and session load without changing collector code. The daily
pipeline also requires KG_IG=1.
"""
import os

_TRUE = {'1', 'true', 'yes', 'on'}

# Quiet skip token used in IG_SKIPPED lines when the master switch is off.
DISABLED_REASON = 'disabled'


def instagram_enabled():
    return os.environ.get('KG_INSTAGRAM_ENABLED', '1').strip().lower() in _TRUE
