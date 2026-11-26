-- migrate:up
-- Public models are gone (ADR-031), so the allow_public_models switch goes. A stored key would make
-- TenantSettings (extra=forbid) reject every read of that tenant's settings, so it is stripped here.

UPDATE tenants SET settings = settings - 'allow_public_models' WHERE settings ? 'allow_public_models';

-- migrate:down
-- Nothing to restore: the stripped value is not kept (ADR-031).
