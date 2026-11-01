"""Teams: every project belongs to exactly one."""
from app.modules.user.teams.constants import DEFAULT_TEAM_NAME
from app.modules.user.teams.readers import get_default_team_id
from app.modules.user.teams.routes import router
from app.modules.user.teams.writers import create_team

__all__ = ["DEFAULT_TEAM_NAME", "create_team", "get_default_team_id", "router"]
