"""The outbound side of a device adapter: an actuator write becomes an effect.

Every other adapter here turns something outside into a pipe write -- a keystroke into
``user.messages``, a model's answer into ``llm.converse.replies``. This is the mirror, and
`pipes.yaml` says what it is for: an actuator write *changes world state*, and the driver
is what makes that true of the world outside the process.

So the job does not send mail. It writes to ``mail.outbox``, the write latches into
``mail.sent``, and this transmits what latched. The distinction matters because the
capability and integrity checks live at the pipe: a job whose integrity has been lowered
by something it read is refused *before* anything reaches here, which is only a guarantee
if there is no other route out. There isn't -- ``smtplib`` is reachable from this module
and from nowhere else in the application.

**Simulated unless configured otherwise**, which `pipes.yaml` already called for: the
point of a capability check is watching it refuse an effect that *would* otherwise
happen, and a demonstration that quietly mails somebody on first run is not a
demonstration anybody wants twice.
"""

from __future__ import annotations

import os
import re
import smtplib
import ssl
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote

__all__ = [
    "DesktopSender",
    "Letter",
    "MailAdapter",
    "MailClientUnavailable",
    "MailSettings",
    "SimulatedSender",
    "SmtpSender",
    "TRANSPORTS",
    "mailto_url",
    "open_uri",
    "settings",
]

#: How a letter leaves. `desktop` is the default because it needs no account at all and
#: cannot send on its own -- a person presses Send in their own mail client, which for a
#: demonstration about one consequential effect is a stronger ending than a silent
#: background send, not a weaker one.
TRANSPORTS = ("desktop", "smtp", "simulated")


@dataclass(frozen=True, slots=True)
class Letter:
    """One email, as it left the kernel."""

    to: str
    subject: str
    body: str

    def as_message(self, sender: str) -> EmailMessage:
        message = EmailMessage()
        message["From"] = sender
        message["To"] = self.to
        message["Subject"] = self.subject
        message["Date"] = formatdate(localtime=True)
        message.set_content(self.body)
        return message


@dataclass(frozen=True, slots=True)
class MailSettings:
    """The mail account, from the environment.

    Separate from the SMTP call so that "is this configured?" is answerable without
    opening a socket -- the page greys its button on it, and a demo run with no mail
    account at all has to be the ordinary case rather than a crash.
    """

    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    sender: str = ""
    recipient: str = ""
    #: Real sends happen only when this is explicitly on. See the module docstring.
    #: Only `smtp` reads it: `desktop` cannot send by itself, so there is nothing to arm.
    live: bool = False
    #: One of ``TRANSPORTS``.
    transport: str = "desktop"
    #: Where the complete letter is written, whatever the transport does with it.
    outbox: Path = Path("outbox")

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username and self.password and self.to)

    @property
    def from_address(self) -> str:
        """Who it is from. The account's own address unless one was given."""
        return self.sender or self.username

    @property
    def to(self) -> str:
        """Who it goes to. The brief is "the user's email account", so the default
        recipient is the account itself: the demonstration mails you your own
        conversation, and needs one address rather than two to be useful."""
        return self.recipient or self.from_address

    @property
    def sending_for_real(self) -> bool:
        """Whether SMTP is both configured and armed. Desktop has no equivalent."""
        return self.live and self.configured


def settings() -> MailSettings:
    def text(name: str) -> str:
        return (os.environ.get(name) or "").strip()

    port = text("MAIL_SMTP_PORT")
    return MailSettings(
        host=text("MAIL_SMTP_HOST"),
        port=int(port) if port.isdigit() else 587,
        username=text("MAIL_USERNAME"),
        password=text("MAIL_PASSWORD"),
        sender=text("MAIL_FROM"),
        recipient=text("MAIL_TO"),
        live=text("MAIL_LIVE").lower() in ("1", "true", "yes", "on"),
        transport=(text("MAIL_TRANSPORT").lower() or "desktop"),
        outbox=Path(text("MAIL_OUTBOX_DIR") or "outbox"),
    )


class SimulatedSender:
    """Keeps the letter instead of sending it. The default, and what the tests use."""

    def __init__(self) -> None:
        self.sent: list[Letter] = []

    def __call__(self, letter: Letter) -> str:
        self.sent.append(letter)
        if not letter.to:
            # No account configured at all, which is the first-run case. Saying "would
            # have gone to " with nothing after it reads as a bug in the demo rather than
            # as the absence of a setting.
            return "simulated — not sent — and no address is configured to send it to"
        return f"simulated — not sent — would have gone to {letter.to}"


class SmtpSender:
    """A real send, over STARTTLS.

    One connection per letter and no retry, which matches the descriptor: `on_fault:
    abort`, because there is no sensible retry for a consequential effect and trying
    again is how one refusal becomes a queue of attempts.
    """

    def __init__(self, config: MailSettings) -> None:
        self._config = config

    def __call__(self, letter: Letter) -> str:
        config = self._config
        message = letter.as_message(config.from_address)
        with smtplib.SMTP(config.host, config.port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(config.username, config.password)
            smtp.send_message(message)
        return f"sent to {letter.to}"


class MailClientUnavailable(Exception):
    """Nothing on this machine is registered to handle a ``mailto:`` URI.

    A headless box or a container has no such association, and that has to degrade to a
    letter on disk rather than a traceback -- the demonstration is not about mail.
    """


def open_uri(uri: str) -> None:
    """Hand a URI to whatever the desktop has registered for it.

    One call per platform, and deliberately **not** ``webbrowser.open``. On Windows that
    does the right thing, but on macOS ``webbrowser`` branches on the scheme: anything
    that is not http(s) is passed to the default *web browser*, so a `mailto:` would open
    a browser window which then bounces the URI back to the mail client. Going straight to
    the platform's own opener skips that.
    """
    try:
        if sys.platform == "win32":
            os.startfile(uri)  # noqa: S606 - a mailto URI, handed to the shell
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [opener, uri], check=True, capture_output=True, timeout=20
        )
    except FileNotFoundError as exc:  # no xdg-open at all
        raise MailClientUnavailable("no URI opener on this system") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode(errors="replace").strip() or f"exit {exc.returncode}"
        raise MailClientUnavailable(detail) from exc
    except OSError as exc:
        # `os.startfile` raises this when no application is associated with mailto.
        raise MailClientUnavailable(str(exc)) from exc


#: How much of a ``mailto:`` URI a desktop will accept. Windows passes it through
#: ShellExecute, which caps out around two thousand characters, and Outlook truncates near
#: the same place. Below that by a margin, because exceeding it silently loses the end of
#: the message rather than failing.
MAILTO_BUDGET = 1800

#: What replaces the part of a body that did not fit.
CONTINUES = " [...] the complete letter is in the .eml file beside this one."


def _mailto(to: str, subject: str, body: str) -> str:
    """A ``mailto:`` URI. Everything percent-encoded, including the separators."""
    fields = f"subject={quote(subject, safe='')}&body={quote(body, safe='')}"
    return f"mailto:{quote(to, safe='@')}?{fields}"


def mailto_url(letter: Letter, budget: int = MAILTO_BUDGET) -> str:
    """The letter as a URI a mail client will open, trimmed to fit.

    Trimming rather than failing: a transcript is longer than any desktop will accept, and
    the whole letter is on disk anyway. What matters is that the draft says so instead of
    stopping mid-sentence as though that were the end.
    """
    whole = _mailto(letter.to, letter.subject, letter.body)
    if len(whole) <= budget:
        return whole
    keep = len(letter.body)
    while keep > 0:
        keep = int(keep * 0.8)
        candidate = _mailto(letter.to, letter.subject, letter.body[:keep].rstrip() + CONTINUES)
        if len(candidate) <= budget:
            return candidate
    return _mailto(letter.to, letter.subject, CONTINUES.strip())


def _filename(letter: Letter) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", letter.subject.lower()).strip("-")[:40] or "letter"
    return f"{stamp}-{slug}.eml"


class DesktopSender:
    """Opens the letter as a draft in whatever mail client this desktop uses.

    The default transport, and it asks for no account: no host, no username, no password,
    nothing to leak and nothing to revoke. It also *cannot send* -- it composes, and a
    person presses Send -- which is why it needs no `MAIL_LIVE` to arm it.

    The complete letter is written to disk first and always, because the URI is trimmed to
    what a desktop will accept and a demonstration should not quietly lose the end of a
    conversation.

    One limitation worth stating rather than discovering: the client opens on whatever
    machine runs the *server*. That is the same machine for this demo, which serves
    127.0.0.1, and the wrong one the day it serves anything else.
    """

    def __init__(
        self,
        config: MailSettings,
        *,
        launch: object | None = None,
    ) -> None:
        self._config = config
        self._launch = launch if launch is not None else open_uri

    def _save(self, letter: Letter) -> Path:
        directory = self._config.outbox
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _filename(letter)
        path.write_bytes(bytes(letter.as_message(self._config.from_address or "zeos-chat")))
        return path

    def __call__(self, letter: Letter) -> str:
        path = self._save(letter)
        try:
            self._launch(mailto_url(letter))  # type: ignore[operator]
        except MailClientUnavailable as exc:
            return f"no mail client on this machine ({exc}) — the letter is at {path}"
        where = f" to {letter.to}" if letter.to else ""
        return f"opened a draft{where} in your mail client — full letter at {path}"


#: How a job's one write becomes a letter. The write is a single command's payload, so
#: the subject and the body arrive as one line with this between them -- a separator
#: rather than a newline because the seat splits a command on whitespace and a newline
#: does not survive the trip. The job chooses where to put it; this only splits on it.
SUBJECT_SEPARATOR = "::"


def letter_from(written: str, config: MailSettings) -> Letter:
    """The latched value, read as an email.

    Deliberately forgiving about the separator: a write with no subject is still a
    letter, because a job that got the formatting wrong should produce a mail the person
    can read rather than a fault they have to interpret.
    """
    subject, separator, body = written.partition(SUBJECT_SEPARATOR)
    if not separator:
        return Letter(to=config.to, subject="Your ZEOS Chat conversation", body=written.strip())
    return Letter(to=config.to, subject=subject.strip(), body=body.strip())


class MailAdapter:
    """Transmits whatever latched into ``mail.sent``.

    Driven by the journal rather than by a drain, because the outbox is an *actuator* and
    not a sink: a sink carries a history the driver empties, an actuator carries a value
    that latches. ``PipeWritten(latched=True)`` is the kernel saying the world changed,
    and acting on that is this adapter's whole job.
    """

    def __init__(
        self,
        config: MailSettings | None = None,
        sender: object | None = None,
        *,
        on_result: object | None = None,
    ) -> None:
        self.config = config if config is not None else settings()
        self._send = sender if sender is not None else self._default_sender()
        #: Told what became of each letter. Set by whoever is showing it to a person,
        #: after construction: the adapter has to exist before the server that reports
        #: its results, so tying the knot at construction made every caller but one
        #: forget to -- and a forgotten callback is a send with no outcome on the page.
        self.on_result = on_result
        #: What was transmitted, newest last. The page reads this, and so do the tests.
        self.log: list[tuple[Letter, str]] = []

    def _default_sender(self) -> object:
        if self.config.transport == "simulated":
            return SimulatedSender()
        if self.config.transport == "smtp":
            # Still only for real when armed *and* configured, as before.
            return SmtpSender(self.config) if self.config.sending_for_real else SimulatedSender()
        return DesktopSender(self.config)

    @property
    def mode(self) -> str:
        """What a press of the button will actually do: the page says so before it is
        pressed, and "opens a draft" and "sends" are not the same promise."""
        if isinstance(self._send, SmtpSender):
            return "live"
        if isinstance(self._send, DesktopSender):
            return "desktop"
        return "simulated"

    @property
    def simulated(self) -> bool:
        """Whether nothing leaves the machine at all. A draft does not, by itself."""
        return self.mode != "live"

    def actuated(self, written: str) -> str:
        """One latched write. Returns what happened, in words for a person."""
        letter = letter_from(written, self.config)
        try:
            outcome = self._send(letter)  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - a failed send must reach the person
            # No retry, and no raise past here: the job that wrote this has already
            # exited, so there is nobody left to fault. What is left is telling the truth
            # on the page.
            outcome = f"could not be sent: {exc}"
        self.log.append((letter, outcome))
        if self.on_result is not None:
            self.on_result(letter, outcome)  # type: ignore[operator]
        return outcome
