# Lightweight web subpackage that re-exports the repository's top-level web modules.
# This file intentionally stays small: actual implementations live in the top-level modules
# (mailer.py, server.py, webconfig.py, etc.). The tests and code import apex.web.* so
# these wrappers expose the top-level implementations under apex.web.
__all__ = [
    "main",
    "security",
    "mailer",
    "server",
    "webconfig",
    "billing",
    "db",
    "service",
]
