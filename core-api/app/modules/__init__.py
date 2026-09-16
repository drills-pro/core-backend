"""The bounded contexts, grouped by audience.

Each group below (user/) is a directory of contexts, not a context
itself. A context's public API is what its __init__ exports — routers for
the composition root, plus the few functions other contexts are allowed
to call. Reaching into app.modules.<group>.<other>.<submodule> is a
boundary violation and fails tests/test_architecture.py.
"""
