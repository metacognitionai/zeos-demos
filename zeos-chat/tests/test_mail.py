"""The one consequential effect, end to end and without a mail server.

What is worth testing here is not SMTP. It is that the effect leaves the machine by
exactly one route -- a job's write to an actuator -- so that the capability and integrity
checks at that pipe are the only thing standing between a conversation and somebody's
inbox. A second route would make them decoration.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from zeos.core.events import PipeWritten
from zeos.core.ids import PipeName
from zeos.descriptor.loader import load_case

from zeos_chat.build import build_session
from zeos_chat.mail import (
    SUBJECT_SEPARATOR,
    Letter,
    MailAdapter,
    MailSettings,
    SimulatedSender,
    letter_from,
)

CASE = Path(__file__).resolve().parents[1] / "cases" / "chat-scripted"
REQUESTS = PipeName("mail.requests")
LETTERS = PipeName("mail.letters")
OUTBOX = PipeName("mail.outbox")


def _ask(session: object, subject: str, body: str) -> None:
    """The letter, then the doorbell -- the order the conversation's own two lines use.

    A firing that arrived before the content would dispatch a job that then blocks on an
    empty pipe, which is exactly what the first version of this did.
    """
    session.deliver(LETTERS, f"{subject}{SUBJECT_SEPARATOR}{body}")  # type: ignore[attr-defined]
    session.deliver(REQUESTS, "send")  # type: ignore[attr-defined]


def _outbox(adapter: MailAdapter | None = None) -> tuple[object, MailAdapter, SimulatedSender]:
    sender = SimulatedSender()
    mail = adapter or MailAdapter(MailSettings(recipient="someone@example.com"), sender)
    session, _, _ = build_session(load_case(CASE), mail=mail)
    session.boot()
    return session, mail, sender


# -- the request reaches the job, and the job's write is the effect ----------


def test_a_request_dispatches_the_service_and_its_write_is_the_letter() -> None:
    """The whole path: a device write fires a vector, the kernel dispatches `send-email`
    at priority 40, and the job's write to the actuator is what gets transmitted."""
    session, mail, sender = _outbox()
    _ask(session, "Kyoto temples", "three of them, and why")
    for _ in range(60):
        session.step()

    assert len(sender.sent) == 1, f"expected one letter, got {sender.sent}"
    letter = sender.sent[0]
    assert letter.to == "someone@example.com"
    assert "Kyoto temples" in letter.subject
    assert "three of them" in letter.body
    assert mail.log and mail.log[0][1].startswith("simulated")


def test_the_letter_comes_from_a_latched_actuator_write() -> None:
    """Not from the request pipe, and not from the driver. The write to `mail.outbox` is
    the effect, which is what makes the check at that pipe the only one that matters."""
    session, _, sender = _outbox()
    _ask(session, "a subject", "a body")
    for _ in range(60):
        session.step()

    latched = [
        e
        for e in session.events  # type: ignore[attr-defined]
        if isinstance(e, PipeWritten) and e.pipe == OUTBOX and e.latched
    ]
    assert latched, "nothing latched into mail.sent"
    assert latched[0].job is not None, "the write came from the driver rather than from a job"
    assert len(sender.sent) == len(latched), "a letter was sent without a latched write"


def test_nothing_is_sent_when_nobody_asked() -> None:
    """The control. An idle conversation must not produce mail, and the point of saying
    so is that the vector is the only thing that dispatches the service."""
    session, _, sender = _outbox()
    session.deliver(PipeName("user.messages"), "hello")
    session.deliver(PipeName("user.arrivals"), "message")
    for _ in range(60):
        session.step()

    assert sender.sent == []


def test_two_requests_are_two_letters() -> None:
    """`policy: queue` on the vector, and a consequential effect is the last place to
    coalesce: a second request to send is not a restatement of the first."""
    session, _, sender = _outbox()
    _ask(session, "first", "one")
    for _ in range(40):
        session.step()
    _ask(session, "second", "two")
    for _ in range(40):
        session.step()

    assert [letter.subject for letter in sender.sent] == ["first", "second"]


# -- the letter itself ------------------------------------------------------


def test_a_write_with_no_separator_is_still_a_letter() -> None:
    """A job that got the formatting wrong should produce a mail a person can read, not a
    fault they have to interpret."""
    letter = letter_from("just some text", MailSettings(recipient="a@b.c"))
    assert letter.body == "just some text"
    assert letter.subject


def test_the_default_recipient_is_the_account_itself() -> None:
    """The brief is "the user's email account": the demo mails you your own conversation,
    so it needs one address rather than two."""
    config = MailSettings(username="me@example.com")
    assert config.to == "me@example.com"
    assert MailSettings(username="me@example.com", recipient="other@example.com").to == (
        "other@example.com"
    )


# -- and it does not send by accident ---------------------------------------


def test_mail_is_simulated_until_it_is_both_configured_and_live() -> None:
    """Two conditions, and the second is a deliberate switch. `pipes.yaml` asks for this:
    the point of the capability check is watching it refuse an effect that *would*
    otherwise happen, which needs the effect to be armed only on purpose."""
    account = {
        "host": "smtp.example.com",
        "username": "me@example.com",
        "password": "secret",  # noqa: S106 - not a credential, a fixture
    }
    assert not MailSettings(**account).sending_for_real, "unset MAIL_LIVE sent for real"
    assert not MailSettings(live=True).sending_for_real, "sent for real with no account"
    assert MailSettings(**account, live=True).sending_for_real


def test_a_failing_send_is_reported_rather_than_raised() -> None:
    """The job that wrote it has already exited, so there is nobody left to fault. What is
    left is telling the truth on the page."""

    def broken(letter: Letter) -> str:
        raise OSError("no route to host")

    mail = MailAdapter(MailSettings(recipient="a@b.c"), broken)
    outcome = mail.actuated(f"s{SUBJECT_SEPARATOR}b")

    assert "could not be sent" in outcome
    assert "no route to host" in outcome


def test_an_unconfigured_run_says_there_is_no_address() -> None:
    """The first-run case. "would have gone to " with nothing after it reads as a bug in
    the demo rather than as a setting nobody has filled in."""
    sender = SimulatedSender()
    outcome = sender(Letter(to="", subject="s", body="b"))

    assert "no address is configured" in outcome
    assert not outcome.endswith("to ")


# -- the SMTP path itself ---------------------------------------------------


def test_a_real_send_starts_tls_before_it_authenticates(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The order is the security property: a login before STARTTLS would put the
    password on the wire in clear. Checked against a fake server rather than a real one,
    because what is worth pinning here is the sequence, not that Gmail exists."""
    from zeos_chat import mail as mail_module

    called: list[str] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int = 0) -> None:
            called.append(f"connect {host}:{port}")

        def __enter__(self) -> "FakeSMTP":
            return self

        def __exit__(self, *exc: object) -> None:
            called.append("close")

        def starttls(self, context: object = None) -> None:
            called.append("starttls")

        def login(self, user: str, password: str) -> None:
            called.append(f"login {user}")

        def send_message(self, message: object) -> None:
            called.append("send")

    monkeypatch.setattr(mail_module.smtplib, "SMTP", FakeSMTP)
    config = MailSettings(
        host="smtp.example.com",
        port=587,
        username="me@example.com",
        password="secret",  # noqa: S106 - a fixture, not a credential
        live=True,
    )
    outcome = mail_module.SmtpSender(config)(Letter(to="me@example.com", subject="s", body="b"))

    assert called == [
        "connect smtp.example.com:587",
        "starttls",
        "login me@example.com",
        "send",
        "close",
    ]
    assert "sent to me@example.com" in outcome


def test_a_rejected_login_reaches_the_person_rather_than_the_logs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Met in practice: a Gmail account password where an app password was needed gives
    `535 5.7.8 BadCredentials`. The job that wrote the letter has already exited, so the
    only place left to say so is the page."""
    import smtplib

    from zeos_chat import mail as mail_module

    class Rejecting:
        def __init__(self, *a: object, **k: object) -> None: ...
        def __enter__(self) -> "Rejecting":
            return self

        def __exit__(self, *exc: object) -> None: ...
        def starttls(self, context: object = None) -> None: ...
        def login(self, user: str, password: str) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

        def send_message(self, message: object) -> None:  # pragma: no cover - never reached
            raise AssertionError("sent despite a rejected login")

    monkeypatch.setattr(mail_module.smtplib, "SMTP", Rejecting)
    config = MailSettings(
        host="smtp.example.com",
        username="me@example.com",
        password="wrong",  # noqa: S106 - a fixture, not a credential
        live=True,
    )
    adapter = MailAdapter(config, mail_module.SmtpSender(config))
    outcome = adapter.actuated(f"s{SUBJECT_SEPARATOR}b")

    assert "could not be sent" in outcome
    assert "535" in outcome, "the page should carry the server's own reason"


# -- the desktop transport, which is the default ----------------------------


def _desktop(tmp_path, **over):  # type: ignore[no-untyped-def]
    from zeos_chat.mail import DesktopSender

    opened: list[str] = []
    config = MailSettings(recipient=over.pop("to", "a@b.c"), outbox=tmp_path, **over)
    return DesktopSender(config, launch=opened.append), opened


def test_a_draft_is_opened_and_the_whole_letter_is_kept(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Both halves matter. The draft is what a person sees; the file is what survives the
    URI being trimmed to what a desktop will accept."""
    sender, opened = _desktop(tmp_path)
    outcome = sender(Letter(to="a@b.c", subject="Kyoto", body="three temples"))

    assert len(opened) == 1 and opened[0].startswith("mailto:a@b.c?")
    assert "Kyoto" in opened[0] or "Kyoto" in opened[0].replace("%20", " ")
    written = list(tmp_path.glob("*.eml"))
    assert len(written) == 1, f"expected one .eml, got {written}"
    text = written[0].read_text(encoding="utf-8")
    assert "Subject: Kyoto" in text
    assert "three temples" in text
    assert "opened a draft" in outcome


def test_nothing_needs_configuring_for_a_draft(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """No host, no username, no password. That is the whole reason it is the default: a
    fresh clone can press the button."""
    sender, opened = _desktop(tmp_path, to="")
    outcome = sender(Letter(to="", subject="s", body="b"))

    assert opened and opened[0].startswith("mailto:?")
    assert "opened a draft" in outcome


def test_a_long_letter_is_trimmed_to_what_a_desktop_accepts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A transcript is longer than any mail client will take from a URI. Windows passes it
    through ShellExecute, which caps near two thousand characters and silently loses the
    end -- so the draft has to say it was cut rather than stop mid-sentence."""
    from zeos_chat.mail import CONTINUES, MAILTO_BUDGET, mailto_url

    letter = Letter(to="a@b.c", subject="long", body="word " * 4000)
    url = mailto_url(letter)

    assert len(url) <= MAILTO_BUDGET
    assert CONTINUES.strip() in unquote(url), "the draft stops mid-sentence with no note"

    # and the file keeps every word of it
    sender, _ = _desktop(tmp_path)
    sender(letter)
    kept = next(iter(tmp_path.glob("*.eml"))).read_text(encoding="utf-8")
    assert kept.count("word") > 3000, "the .eml was trimmed too"


def test_a_short_letter_is_not_trimmed() -> None:
    """The control: trimming a letter that fits would put a "continues" note on a
    complete message, which is a lie about the message."""
    from zeos_chat.mail import CONTINUES, mailto_url

    url = mailto_url(Letter(to="a@b.c", subject="s", body="a short body"))
    assert CONTINUES.strip() not in unquote(url)
    assert unquote(url).endswith("a short body")


def test_a_machine_with_no_mail_client_says_so_and_keeps_the_letter(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A headless box or a container has no mailto association. That has to degrade to a
    letter on disk rather than a traceback: the demonstration is not about mail."""
    from zeos_chat.mail import DesktopSender, MailClientUnavailable

    def no_handler(uri: str) -> None:
        raise MailClientUnavailable("no URI opener on this system")

    config = MailSettings(recipient="a@b.c", outbox=tmp_path)
    outcome = DesktopSender(config, launch=no_handler)(Letter(to="a@b.c", subject="s", body="b"))

    assert "no mail client" in outcome
    assert str(tmp_path) in outcome, "the person is not told where the letter went"
    assert list(tmp_path.glob("*.eml")), "the letter was lost with the client"


def test_the_uri_goes_to_the_platform_opener_not_to_a_browser(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`webbrowser.open` looks right and is wrong on macOS: for a non-http scheme it
    resolves the default *web browser* and hands the URI to that, so a `mailto:` opens a
    browser window which then bounces it back. Each platform's own opener skips the hop."""
    from zeos_chat import mail as mail_module

    for platform, expected in (("darwin", "open"), ("linux", "xdg-open")):
        ran: list[list[str]] = []
        monkeypatch.setattr(mail_module.sys, "platform", platform)
        monkeypatch.setattr(
            mail_module.subprocess,
            "run",
            lambda argv, **kw: ran.append(argv) or _completed(),  # type: ignore[func-returns-value]
        )
        mail_module.open_uri("mailto:a@b.c?subject=s")
        assert ran == [[expected, "mailto:a@b.c?subject=s"]], f"{platform}: {ran}"


def _completed():  # type: ignore[no-untyped-def]
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")


def test_a_failing_opener_becomes_the_unavailable_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """So the caller has one thing to catch, whichever way the desktop refused."""
    import subprocess

    from zeos_chat import mail as mail_module

    monkeypatch.setattr(mail_module.sys, "platform", "linux")

    def missing(argv, **kw):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("xdg-open")

    monkeypatch.setattr(mail_module.subprocess, "run", missing)
    try:
        mail_module.open_uri("mailto:a@b.c")
    except mail_module.MailClientUnavailable as exc:
        assert "no URI opener" in str(exc)
    else:
        raise AssertionError("a missing opener was not reported")

    def refused(argv, **kw):  # type: ignore[no-untyped-def]
        raise subprocess.CalledProcessError(4, argv, stderr=b"no handler for mailto")

    monkeypatch.setattr(mail_module.subprocess, "run", refused)
    try:
        mail_module.open_uri("mailto:a@b.c")
    except mail_module.MailClientUnavailable as exc:
        assert "no handler for mailto" in str(exc), "the desktop's own reason was dropped"
    else:
        raise AssertionError("a refusing opener was not reported")
