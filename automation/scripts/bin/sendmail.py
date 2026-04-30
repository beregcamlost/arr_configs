#!/usr/bin/env python3
"""sendmail.py — Gmail SMTP relay helper using app-password auth.

Reads credentials from environment:
  GMAIL_USER         — Gmail address (used as default From and SMTP login)
  GMAIL_APP_PASSWORD — 16-char app password (spaces allowed, stripped before use)

Usage:
  sendmail.py --to <addr> [--to <addr2> ...] \\
              --subject "..." \\
              [--body "text" | --body-file path | --stdin] \\
              [--from <addr>] \\
              [--html] \\
              [--dry-run]
"""
import argparse
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def get_credentials() -> tuple[str, str]:
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not user:
        die(
            "GMAIL_USER is not set.\n"
            "  Export it before calling this script:\n"
            "    export GMAIL_USER=you@gmail.com"
        )
    if not password:
        die(
            "GMAIL_APP_PASSWORD is not set.\n"
            "  Generate an app password at https://myaccount.google.com/apppasswords\n"
            "  then export it:\n"
            "    export GMAIL_APP_PASSWORD='xxxx xxxx xxxx xxxx'"
        )
    return user, password


def build_message(
    *,
    from_addr: str,
    to_addrs: list[str],
    subject: str,
    body: str,
    html: bool,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    content_type = "html" if html else "plain"
    msg.set_content(body, subtype=content_type)
    return msg


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send email via Gmail SMTP using app-password auth."
    )
    parser.add_argument(
        "--to",
        dest="to",
        action="append",
        required=True,
        metavar="ADDR",
        help="Recipient address (repeatable).",
    )
    parser.add_argument("--subject", required=True, help="Email subject line.")

    body_group = parser.add_mutually_exclusive_group()
    body_group.add_argument("--body", help="Inline body text.")
    body_group.add_argument(
        "--body-file", type=Path, help="Path to a file whose contents become the body."
    )
    body_group.add_argument(
        "--stdin",
        action="store_true",
        help="Read body from stdin.",
    )

    parser.add_argument(
        "--from",
        dest="from_addr",
        default=None,
        metavar="ADDR",
        help="From address (default: GMAIL_USER). Gmail rewrites envelope sender to the auth account.",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="Treat body as HTML (MIME text/html).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build message and print headers; do NOT connect to SMTP.",
    )
    return parser.parse_args()


def read_body(args: argparse.Namespace) -> str:
    if args.body is not None:
        return args.body
    if args.body_file is not None:
        path = args.body_file
        if not path.is_file():
            die(f"--body-file path does not exist or is not a file: {path}")
        return path.read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.read()
    # Default: empty body (caller may have reason to send subject-only)
    return ""


def main() -> None:
    args = parse_args()
    gmail_user, gmail_password = get_credentials()

    from_addr = args.from_addr if args.from_addr else gmail_user
    body = read_body(args)

    msg = build_message(
        from_addr=from_addr,
        to_addrs=args.to,
        subject=args.subject,
        body=body,
        html=args.html,
    )

    if args.dry_run:
        print("=== DRY RUN — message NOT sent ===", file=sys.stderr)
        print(f"SMTP host : {SMTP_HOST}:{SMTP_PORT} (STARTTLS)", file=sys.stderr)
        print(f"Auth user : {gmail_user}", file=sys.stderr)
        print("--- Headers ---")
        for key in ("From", "To", "Subject"):
            print(f"{key}: {msg[key]}")
        print(f"Content-Type: {msg.get_content_type()}")
        print(f"Body length : {len(body)} chars")
        sys.exit(0)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(gmail_user, gmail_password)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        die(
            f"SMTP authentication failed: {exc}\n"
            "  Check GMAIL_USER and GMAIL_APP_PASSWORD. Make sure 2FA is on\n"
            "  and the app password was generated at https://myaccount.google.com/apppasswords"
        )
    except smtplib.SMTPException as exc:
        die(f"SMTP error: {exc}")
    except OSError as exc:
        die(f"Network error connecting to {SMTP_HOST}:{SMTP_PORT} — {exc}")

    print(f"Sent to: {', '.join(args.to)}", file=sys.stderr)


if __name__ == "__main__":
    main()
