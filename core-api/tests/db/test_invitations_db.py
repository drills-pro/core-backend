"""accept_tenant_invitation on a real Postgres: the first accept joins, the same person accepting again gets the
workspace back without a second join, and anyone else gets nothing."""
from app.core.authz import InviteRole, TenantRole
from app.modules.user.tenant_members.service import hash_token

TOKEN, EMAIL, SUB = "tok-invite", "New@Example.com", "invitee"
INVITE = (
    "INSERT INTO tenant_invitations (tenant_id, email, role, token_hash, invited_by, expires_at) "
    "VALUES ($1, $2, $3, $4, 'u1', now() + interval '1 day')"
)
ACCEPT = "SELECT tenant_id, role, joined FROM accept_tenant_invitation($1, $2, $3)"
MEMBERS = "SELECT count(*) FROM tenant_members WHERE tenant_id = $1 AND user_sub = $2"
LEAVE = "DELETE FROM tenant_members WHERE tenant_id = $1 AND user_sub = $2"
AS_TENANT = "SELECT set_config('app.tenant_id', $1, true)"  # tenant_members is behind RLS for core_app


def test_the_invitee_joins_once_and_opening_the_link_again_returns_the_workspace(two_tenants):
    t = two_tenants

    async def go():
        await t.conn.execute(INVITE, t.a, EMAIL, InviteRole.ADMIN, hash_token(TOKEN))
        async with t.app_role() as conn:
            first = await conn.fetch(ACCEPT, hash_token(TOKEN), EMAIL.lower(), SUB)
            assert [tuple(r) for r in first] == [(t.a, TenantRole.ADMIN, True)]
            again = await conn.fetch(ACCEPT, hash_token(TOKEN), EMAIL, SUB)
            assert [tuple(r) for r in again] == [(t.a, TenantRole.ADMIN, False)]
            await conn.execute(AS_TENANT, str(t.a))
            assert await conn.fetchval(MEMBERS, t.a, SUB) == 1

    t.run(go())


def test_an_accepted_link_gives_nothing_to_another_account_or_address(two_tenants):
    t = two_tenants

    async def go():
        await t.conn.execute(INVITE, t.a, EMAIL, InviteRole.MEMBER, hash_token(TOKEN))
        async with t.app_role() as conn:
            assert len(await conn.fetch(ACCEPT, hash_token(TOKEN), EMAIL, SUB)) == 1
            assert await conn.fetch(ACCEPT, hash_token(TOKEN), EMAIL, "someone-else") == []  # same address, not a member
            assert await conn.fetch(ACCEPT, hash_token(TOKEN), "other@example.com", SUB) == []
            assert await conn.fetch(ACCEPT, hash_token("wrong"), EMAIL, SUB) == []

    t.run(go())


def test_a_member_who_left_cannot_rejoin_with_the_old_link(two_tenants):
    t = two_tenants

    async def go():
        await t.conn.execute(INVITE, t.a, EMAIL, InviteRole.MEMBER, hash_token(TOKEN))
        async with t.app_role() as conn:  # one block: leaving it rolls the accept back
            assert len(await conn.fetch(ACCEPT, hash_token(TOKEN), EMAIL, SUB)) == 1
            await conn.execute(AS_TENANT, str(t.a))
            await conn.execute(LEAVE, t.a, SUB)
            assert await conn.fetch(ACCEPT, hash_token(TOKEN), EMAIL, SUB) == []

    t.run(go())
