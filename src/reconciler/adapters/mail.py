"""Mail transports (ADR 0012).

Two implementations behind one port. `ConsoleMailer` exists because the first thing you want
from an approval mail is to *read* it — before a transport is configured, and before anything
is sent to a real inbox. `SmtpMailer` is stdlib, so any provider's app password works and the
tier gains no dependency.
"""

import logging
import smtplib
import sys
from email.message import EmailMessage

logger = logging.getLogger("reconciler.adapters.mail")


class ConsoleMailer:
    """Prints the mail instead of sending it."""

    def __init__(self, stream=None, show_html: bool = False):
        self._stream = stream or sys.stdout
        self._show_html = show_html

    def send(self, *, subject: str, html: str, text: str) -> None:
        print("=" * 72, file=self._stream)
        print(f"SUBJECT: {subject}", file=self._stream)
        print("=" * 72, file=self._stream)
        print(text, file=self._stream)
        if self._show_html:
            print("-" * 72 + "\nHTML:\n" + html, file=self._stream)


class FileMailer:
    """Writes the HTML body to a file — for opening the real rendering in a browser."""

    def __init__(self, path):
        self._path = path

    def send(self, *, subject: str, html: str, text: str) -> None:
        self._path.write_text(f"<!-- {subject} -->\n{html}")
        logger.info("wrote mail preview to %s", self._path)


class SmtpMailer:
    """Sends over SMTP with STARTTLS, or implicit TLS on port 465."""

    def __init__(self, *, host: str, port: int, username: str, password: str,
                 sender: str, recipient: str, timeout: float = 30.0):
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._sender, self._recipient = sender, recipient
        self._timeout = timeout

    def send(self, *, subject: str, html: str, text: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self._sender
        message["To"] = self._recipient
        # Plain text first, HTML as the alternative — so a reply quotes something readable,
        # and the await that classifies that reply has clean text to work with.
        message.set_content(text)
        message.add_alternative(html, subtype="html")

        if self._port == 465:
            server = smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout)
        else:
            server = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        with server:
            if self._port != 465:
                server.starttls()
            server.login(self._username, self._password)
            server.send_message(message)
        logger.info("sent %r to %s", subject, self._recipient)
