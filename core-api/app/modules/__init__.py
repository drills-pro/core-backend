"""The bounded contexts.

A module's public API is what its __init__ exports — routers for the
composition root, plus the few functions other modules are allowed to
call. Reaching into app.modules.<other>.<submodule> is a boundary
violation and fails tests/test_architecture.py.
"""
