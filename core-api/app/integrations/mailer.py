"""Outgoing mail over SMTP (ZeptoMail by Zoho).

The credentials are the `zoho` secret file (Vault `app-secret/app-backend/zoho`, keys Servername, Port, Username,
Password), read on every send so a rotated password needs no restart. Port 465 is implicit TLS; any other port
upgrades with STARTTLS (ZeptoMail: 587). Mail is never sent in clear text.

smtplib blocks, so a send runs in a worker thread.
"""
import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from pydantic import BaseModel

from app.core import secret_files
from app.core.settings import settings

logger = logging.getLogger(__name__)

IMPLICIT_TLS_PORT = 465
# Mail on someone's behalf: their name is shown, the address stays ours. ZeptoMail sends only from a verified domain,
# and a person's own address as From would fail SPF/DMARC, so their address goes in Reply-To instead.
ON_BEHALF_NAME = "{person} via {service}"


class OutgoingMail(BaseModel):
    to: str
    subject: str
    text: str  # the plain-text part, for clients that do not render HTML
    html: str
    on_behalf_of: str | None = None  # the person's name shown as the sender
    reply_to: str | None = None  # where a reply goes: that person's own address


def _one_line(value: str) -> str:
    """A person's name in a header: CR/LF would be refused, so whitespace runs become one space."""
    return " ".join(value.split())


def _sender_name(mail: OutgoingMail) -> str:
    if not mail.on_behalf_of:
        return settings.mail.from_name
    return ON_BEHALF_NAME.format(person=_one_line(mail.on_behalf_of), service=settings.mail.from_name)


class MailError(RuntimeError):
    """The message was not handed to the SMTP server. Carries no credential."""


def _message(mail: OutgoingMail) -> EmailMessage:
    msg = EmailMessage()  # the default policy refuses CR/LF in a header value, so a field cannot add headers
    msg["Subject"] = mail.subject
    msg["From"] = formataddr((_sender_name(mail), settings.mail.from_address))
    msg["To"] = mail.to
    if mail.reply_to:
        msg["Reply-To"] = mail.reply_to
    msg.set_content(mail.text)
    msg.add_alternative(mail.html, subtype="html")
    return msg


def _deliver(msg: EmailMessage) -> None:
    creds = secret_files.get(secret_files.ZOHO)
    host, port = creds["Servername"], int(creds["Port"])
    timeout = settings.mail.timeout_seconds
    tls = ssl.create_default_context()
    if port == IMPLICIT_TLS_PORT:
        with smtplib.SMTP_SSL(host, port, context=tls, timeout=timeout) as server:
            server.login(creds["Username"], creds["Password"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.starttls(context=tls)
            server.login(creds["Username"], creds["Password"])
            server.send_message(msg)


async def send(mail: OutgoingMail) -> None:
    """Hand one message to the SMTP server; MailError when it was not accepted."""
    try:
        msg = _message(mail)
        await asyncio.to_thread(_deliver, msg)
    except (smtplib.SMTPException, OSError, ValueError, secret_files.SecretError) as e:
        # The type and smtplib's reply only: the recipient is personal data and the credential never appears.
        logger.warning("mail not sent: %s: %s", type(e).__name__, e)
        raise MailError(type(e).__name__) from e
