"""Teams inside a tenant."""
from app.modules.user.teams.routes import router
from app.modules.user.teams.writers import create_team

__all__ = ["create_team", "router"]
