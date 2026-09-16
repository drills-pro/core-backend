"""The end-user product: everything under /v1 a tenant's own users reach.

A grouping, not a bounded context of its own — it owns no routes and
exports nothing. Each package below it is a context with its own public
API; importing one is `from app.modules.user.<m> import thing`.
"""
