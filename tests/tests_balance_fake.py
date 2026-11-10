"""Explicit no-network healthy balance transport for injected completion clients."""
def healthy_balance(key):
    return 200, b'{"is_available":true,"balance_infos":[{"currency":"USD","total_balance":"1.00"}]}'
