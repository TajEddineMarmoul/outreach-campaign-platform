import base64
import email
from email.message import EmailMessage
from pathlib import Path
from src.gmail_sender import EmailAttachment, build_message

def test_build_message_plain_text():
    raw_msg = build_message(
        sender="test@example.com",
        recipient="user@example.com",
        subject="Test Subject",
        body="Hello world",
        attachment_path=None
    )
    
    assert "raw" in raw_msg
    
    # decode back
    msg_bytes = base64.urlsafe_b64decode(raw_msg["raw"].encode("ascii"))
    msg = email.message_from_bytes(msg_bytes)
    
    assert msg["To"] == "user@example.com"
    assert msg["From"] == "test@example.com"
    assert msg["Subject"] == "Test Subject"
    assert not msg.is_multipart()
    assert msg.get_content_type() == "text/plain"
    assert "Hello world" in msg.get_payload()

def test_build_message_html():
    raw_msg = build_message(
        sender="test@example.com",
        recipient="user@example.com",
        subject="Test HTML",
        body="<p>Hello <b>world</b></p>",
        attachment_path=None
    )
    
    msg_bytes = base64.urlsafe_b64decode(raw_msg["raw"].encode("ascii"))
    msg = email.message_from_bytes(msg_bytes)
    
    assert not msg.is_multipart()
    assert msg.get_content_type() == "text/html"
    assert "<p>Hello <b>world</b></p>" in msg.get_payload()

def test_build_message_ignores_legacy_attachment_path(tmp_path):
    # Create a dummy attachment
    dummy_pdf = tmp_path / "test.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4 dummy content")
    
    raw_msg = build_message(
        sender="test@example.com",
        recipient="user@example.com",
        subject="Attachment Test",
        body="Check the attachment",
        attachment_path=str(dummy_pdf)
    )
    
    msg_bytes = base64.urlsafe_b64decode(raw_msg["raw"].encode("ascii"))
    msg = email.message_from_bytes(msg_bytes)
    
    assert not msg.is_multipart()
    assert msg.get_content_type() == "text/plain"


def test_build_message_with_stored_attachment():
    raw_msg = build_message(
        sender="test@example.com",
        recipient="user@example.com",
        subject="Stored attachment",
        body="Check the attachment",
        attachment=EmailAttachment(
            filename="resume.pdf",
            content_type="application/pdf",
            content=b"%PDF-1.4 stored content",
        ),
    )

    msg_bytes = base64.urlsafe_b64decode(raw_msg["raw"].encode("ascii"))
    msg = email.message_from_bytes(msg_bytes)
    pdf_parts = [part for part in msg.walk() if part.get_content_type() == "application/pdf"]

    assert len(pdf_parts) == 1
    assert pdf_parts[0].get_filename() == "resume.pdf"
    assert pdf_parts[0].get_payload(decode=True) == b"%PDF-1.4 stored content"


def test_build_message_with_multiple_stored_attachments():
    raw_msg = build_message(
        sender="test@example.com",
        recipient="user@example.com",
        subject="Multiple attachments",
        body="Check the attachments",
        attachments=[
            EmailAttachment(
                filename="resume.pdf",
                content_type="application/pdf",
                content=b"resume",
            ),
            EmailAttachment(
                filename="portfolio.txt",
                content_type="text/plain",
                content=b"portfolio",
            ),
        ],
    )

    msg_bytes = base64.urlsafe_b64decode(raw_msg["raw"].encode("ascii"))
    msg = email.message_from_bytes(msg_bytes)
    attachment_parts = [part for part in msg.walk() if part.get_filename()]

    assert [part.get_filename() for part in attachment_parts] == ["resume.pdf", "portfolio.txt"]
    assert [part.get_payload(decode=True) for part in attachment_parts] == [b"resume", b"portfolio"]

