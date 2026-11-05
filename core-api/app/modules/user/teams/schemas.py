import uuid
from datetime import datetime

from pydantic import BaseModel


class Team(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
