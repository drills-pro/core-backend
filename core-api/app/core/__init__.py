"""Shared kernel: infrastructure every module is allowed to depend on.

Nothing in here may import app.integrations or app.modules — that rule
is what keeps the dependency graph one-directional.
"""
