from pydantic import BaseModel, ConfigDict


class TenantSettings(BaseModel):
    """Per-tenant policy switches, stored in tenants.settings (jsonb); a missing key reads as its default here.

    Also the body of a partial update: only the fields sent change (`model_dump(exclude_unset=True)`), an unknown
    key or a non-boolean value is a 422.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    members_can_create_projects: bool = True
    require_review_before_training: bool = True
    allow_public_models: bool = False
