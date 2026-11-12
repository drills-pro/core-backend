"""The invitation email: Thai first, then English, since the app is bilingual and the invitee has no language
setting yet. Every value that comes from a person (workspace name, inviter) is escaped in the HTML part."""
from html import escape

from pydantic import BaseModel

from app.core.authz import InviteRole
from app.core.settings import settings
from app.integrations.mailer import OutgoingMail
from app.modules.user.tenant_members.constants import INVITATION_TTL, INVITE_PATH

_ROLE_TH = {InviteRole.ADMIN: "ผู้ดูแล (admin)", InviteRole.MEMBER: "สมาชิก (member)"}


class InvitationMail(BaseModel):
    to: str
    tenant_name: str
    inviter: str  # the inviter's display name or email, as the invitee would know them
    inviter_email: str | None  # the reply goes here; None when the inviter's address is unknown
    role: InviteRole
    token: str


def invite_link(token: str) -> str:
    return settings.mail.app_url + INVITE_PATH.format(token=token)


def build(inv: InvitationMail) -> OutgoingMail:
    link = invite_link(inv.token)
    days = INVITATION_TTL.days
    subject = f"{inv.inviter} invited you to {inv.tenant_name} on drills.pro"
    text = (
        f"{inv.inviter} เชิญคุณเข้าร่วม workspace \"{inv.tenant_name}\" บน drills.pro ในบทบาท {_ROLE_TH[inv.role]}\n"
        f"กดลิงก์นี้เพื่อยืนยันและเข้าร่วม (ใช้ได้ครั้งเดียว ภายใน {days} วัน):\n{link}\n\n"
        f"{inv.inviter} invited you to join the workspace \"{inv.tenant_name}\" on drills.pro as {inv.role.value}.\n"
        f"Open this link to accept (it works once and expires in {days} days):\n{link}\n\n"
        f"Sign in with {inv.to} to accept. If you did not expect this, ignore this email.\n"
    )
    who, where, url = escape(inv.inviter), escape(inv.tenant_name), escape(link, quote=True)
    html = f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f5f5f4;font-family:Arial,Helvetica,sans-serif;color:#1c1917">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table role="presentation" width="520" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:8px;padding:32px">
<tr><td>
<p style="margin:0 0 16px;font-size:18px;font-weight:bold">drills.pro</p>
<p style="margin:0 0 8px;font-size:15px;line-height:1.6"><b>{who}</b> เชิญคุณเข้าร่วม workspace <b>{where}</b> ในบทบาท {escape(_ROLE_TH[inv.role])}</p>
<p style="margin:0 0 24px;font-size:15px;line-height:1.6;color:#57534e"><b>{who}</b> invited you to join <b>{where}</b> as {inv.role.value}.</p>
<p style="margin:0 0 24px"><a href="{url}" style="display:inline-block;background:#1c1917;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:6px;font-weight:bold">ยืนยันและเข้าร่วม · Accept invitation</a></p>
<p style="margin:0 0 8px;font-size:13px;line-height:1.6;color:#57534e">ลิงก์ใช้ได้ครั้งเดียวภายใน {days} วัน และต้องเข้าสู่ระบบด้วย {escape(inv.to)}<br>
The link works once, expires in {days} days, and needs you signed in as {escape(inv.to)}.</p>
<p style="margin:0;font-size:12px;line-height:1.6;color:#a8a29e;word-break:break-all">{url}</p>
<p style="margin:16px 0 0;font-size:12px;color:#a8a29e">ถ้าคุณไม่ได้คาดว่าจะได้รับอีเมลนี้ ไม่ต้องทำอะไร · If you did not expect this, ignore this email.</p>
</td></tr></table>
</td></tr></table>
</body></html>"""
    return OutgoingMail(to=inv.to, subject=subject, text=text, html=html, on_behalf_of=inv.inviter, reply_to=inv.inviter_email)
