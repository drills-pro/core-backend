import asyncio
import json
import smtplib

import pytest

from app.core.settings import settings
from app.integrations import mailer

MAIL = mailer.OutgoingMail(to="new@b.co", subject="สวัสดี invite", text="plain", html="<p>html</p>")


class FakeSMTP:
    """Records the conversation instead of opening a socket."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, **kw):
        self.host, self.port, self.kw, self.steps = host, port, kw, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.steps.append("starttls")

    def login(self, user, password):
        self.steps.append(("login", user))

    def send_message(self, msg):
        self.steps.append(("send", msg))


@pytest.fixture
def zoho(monkeypatch, tmp_path):
    def write(port):
        path = tmp_path / f"zoho-{port}.json"
        path.write_text(json.dumps({"Servername": "smtp.example.test", "Port": str(port), "Username": "emailapikey",
                                    "Password": "not-a-real-one", "apikey": "unused"}))
        monkeypatch.setattr(settings.secrets, "zoho", path)

    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return write


def test_port_587_upgrades_with_starttls_before_logging_in(zoho):
    zoho(587)
    asyncio.run(mailer.send(MAIL))
    [server] = FakeSMTP.instances
    assert (server.host, server.port) == ("smtp.example.test", 587)
    assert server.steps[0] == "starttls" and server.steps[1] == ("login", "emailapikey")
    msg = server.steps[2][1]
    assert msg["To"] == "new@b.co" and settings.mail.from_address in msg["From"]
    assert {p.get_content_type() for p in msg.iter_parts()} == {"text/plain", "text/html"}


def test_mail_on_behalf_shows_the_person_but_sends_from_our_domain_and_replies_to_them(zoho):
    zoho(587)
    mail = MAIL.model_copy(update={"on_behalf_of": "Somchai\r\nBcc: x@evil.co", "reply_to": "owner@gmail.com"})
    asyncio.run(mailer.send(mail))
    msg = FakeSMTP.instances[0].steps[2][1]
    assert msg["From"].addresses[0].addr_spec == settings.mail.from_address  # never the owner's own address
    assert msg["From"].addresses[0].display_name == f"Somchai Bcc: x@evil.co via {settings.mail.from_name}"
    assert msg["Reply-To"] == "owner@gmail.com" and msg["Bcc"] is None


def test_mail_without_a_person_has_no_reply_to(zoho):
    zoho(587)
    asyncio.run(mailer.send(MAIL))
    msg = FakeSMTP.instances[0].steps[2][1]
    assert msg["From"].addresses[0].display_name == settings.mail.from_name and msg["Reply-To"] is None


def test_port_465_is_implicit_tls(zoho):
    zoho(mailer.IMPLICIT_TLS_PORT)
    asyncio.run(mailer.send(MAIL))
    [server] = FakeSMTP.instances
    assert "starttls" not in server.steps and "context" in server.kw


def test_a_missing_secret_or_a_refused_login_is_a_mail_error(zoho, monkeypatch):
    with pytest.raises(mailer.MailError):  # test.yaml points at /nonexistent
        asyncio.run(mailer.send(MAIL))
    zoho(587)

    def refuse(self, user, password):
        raise smtplib.SMTPAuthenticationError(535, b"Authentication Failed")

    monkeypatch.setattr(FakeSMTP, "login", refuse)
    with pytest.raises(mailer.MailError):
        asyncio.run(mailer.send(MAIL))
