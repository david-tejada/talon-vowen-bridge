# Vowen -> Talon integration.
#
# The Python module registers these settings with safe defaults too.  Keeping
# them here makes the operational policy visible and easy to override from a
# normal Talon user profile without editing Python.
settings():
    user.vowen_talon_bridge_enabled = true
    user.vowen_talon_bridge_poll_interval_ms = 300
    user.vowen_talon_bridge_dispatch_interval_ms = 50
    user.vowen_talon_bridge_request_timeout_ms = 150
    user.vowen_talon_bridge_failure_grace_ms = 3000
