"""Patterns shared by modules that must not import each other's database code (a leaf: it imports nothing)."""

SHA256_PATTERN = r"^[0-9a-f]{64}$"  # mirrors job_packages_sha256_check
