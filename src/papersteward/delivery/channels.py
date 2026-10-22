"""文件原子不覆盖；SMTP仅单次尝试，未知送达结果由上层持久化。"""

import os
import smtplib
import ssl
import tempfile
from email.message import EmailMessage
from pathlib import Path

from papersteward.config import Settings
from papersteward.core.errors import InvariantViolation
from papersteward.delivery.contracts import Digest
from papersteward.delivery.render import render_html, render_markdown


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise InvariantViolation("delivery.path", "拒绝符号链接输出")
    if path.exists():
        if path.read_text(encoding="utf-8") == content:
            return
        raise InvariantViolation("delivery.path", "输出已存在且内容不同；未覆盖")
    fd, temporary = tempfile.mkstemp(prefix=".digest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_text(encoding="utf-8") != content:
                raise InvariantViolation("delivery.path", "输出路径被其他操作占用") from None
    finally:
        os.unlink(temporary)


def smtp_destination(settings: Settings) -> str:
    if not settings.smtp_host or not settings.smtp_from or not settings.smtp_to:
        raise InvariantViolation("smtp", "请配置PAPERSTEWARD_SMTP_HOST/FROM/TO")
    if bool(settings.smtp_username) != bool(settings.smtp_password):
        raise InvariantViolation("smtp", "PAPERSTEWARD_SMTP_USERNAME/PASSWORD须同时提供")
    return f"{settings.smtp_host}:{settings.smtp_port}:{settings.smtp_from}:{settings.smtp_to}"


def send_smtp(digest: Digest, settings: Settings) -> None:
    smtp_destination(settings)
    message = EmailMessage()
    message["Subject"] = f"PaperSteward 今日文献 · {digest.digest_date}"
    message["From"] = settings.smtp_from
    message["To"] = settings.smtp_to
    message["Message-ID"] = f"<papersteward-{digest.digest_date}@localhost>"
    message.set_content(render_markdown(digest))
    message.add_alternative(render_html(digest), subtype="html")
    assert settings.smtp_host is not None
    context = ssl.create_default_context()
    client = (
        smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_s, context=context
        )
        if settings.smtp_security == "ssl"
        else smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_s)
    )
    with client:
        if settings.smtp_security == "starttls":
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        if settings.smtp_username and settings.smtp_password:
            client.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        client.send_message(message)
