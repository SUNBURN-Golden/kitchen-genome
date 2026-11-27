"""Instagram collection switch.

Off by default: Instagram disabled @kitchen.genome on 2026-10-22 and is not
accepting a review. Operators can temporarily re-enable a collector by setting
KG_INSTAGRAM_ENABLED=1; the daily pipeline also requires KG_IG=1.
"""
import os

_TRUE = {'1', 'true', 'yes', 'on'}

# Quiet skip token used in IG_SKIPPED lines and self-check reports.
DISABLED_REASON = 'disabled_account_banned'


def instagram_enabled():
    return os.environ.get('KG_INSTAGRAM_ENABLED', '0').strip().lower() in _TRUE
