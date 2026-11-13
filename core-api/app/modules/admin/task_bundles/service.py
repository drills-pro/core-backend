"""Task bundles (planning/25): register a staged bundle and smoke it on the stable core."""
from app.core.job_packages import JobPackage
from app.integrations import data_plane


async def register(s3_key: str, actor: str) -> JobPackage:
    """Validate the staged zip, record it as a candidate (audited) and start its smoke; a known bundle comes back as it is."""
    return await data_plane.start_package_smoke(await data_plane.register_staged_bundle(s3_key, actor))
