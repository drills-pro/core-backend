"""Clients for the systems outside this process.

Each module here owns the wire format, auth and quirks of one external
service, so the bounded contexts in app/modules never speak HTTP/S3 to
them directly. May depend on app.core; never on app.modules.
"""
