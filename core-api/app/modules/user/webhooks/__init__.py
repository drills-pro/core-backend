"""Webhooks: signed delivery of a project's job events (the outbox read by /events) to a customer's https endpoint."""
from app.modules.user.webhooks.dispatcher import dispatch_webhooks
from app.modules.user.webhooks.routes import router

__all__ = ["dispatch_webhooks", "router"]
