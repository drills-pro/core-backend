import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.modules.user.webhooks.constants import URL_MAX_LENGTH


class NewWebhook(BaseModel):
    url: str = Field(min_length=1, max_length=URL_MAX_LENGTH)  # https only, a public host


class Webhook(BaseModel):
    id: uuid.UUID
    url: str
    created_at: datetime
    disabled_at: datetime | None = None  # set when delivery failed too many times in a row


class CreatedWebhook(Webhook):
    secret: str  # signs every delivery (see X-Drills-Signature); shown here and recoverable only by the platform
