"""Playwright operations for the genuine Roundcube surface."""
from __future__ import annotations

import re

from showAndTell.applications.browser import context as browser_context
MESSAGE_SUBJECTS = {
    "w-ci1": "[bracketthq/brackett] Run failed: Deploy - main (405364e)",
    "w-urgent": "Fwd: You have employees who must install Rollcrew",
}

LOGIN_USER = 'input[name="_user"]'
LOGIN_PASSWORD = 'input[name="_pass"]'
MESSAGE_ROWS = "#messagelist tbody tr"
SEARCH_INPUT = '#mailsearchform input[name="_q"], #quicksearchbox'


def _password(page) -> str:
    value = getattr(page, "_showAndTell_mailfix_password", None)
    if isinstance(value, str) and value:
        return value
    raise RuntimeError("Roundcube account switch needs the seeded mailbox password")


def _roundcube_login(page, email: str, password: str) -> None:
    page.locator(LOGIN_USER).fill(email)
    page.locator(LOGIN_PASSWORD).fill(password)
    page.get_by_role(
        "button", name=re.compile(r"^(login|log in)$", re.IGNORECASE)
    ).click()
    page.locator("#messagelist").wait_for()


def login(page, app_url: str, creds: dict) -> None:
    browser_context.bind(page, creds)
    page.goto(f"{app_url.rstrip('/')}/", wait_until="domcontentloaded")
    setattr(page, "_showAndTell_mailfix_password", creds["password"])
    user = page.locator(LOGIN_USER)
    if not user.count() or not user.first.is_visible():
        page.locator(
            'a.button-logout, a[aria-label*="logout" i], '
            'a[title*="logout" i]'
        ).first.click()
        user.first.wait_for()
    _roundcube_login(page, creds["email"], creds["password"])


def open_personal_inbox(page, app_url: str) -> None:
    page.goto(
        f"{app_url.rstrip('/')}/?_task=mail&_mbox=INBOX",
        wait_until="networkidle",
    )
    page.locator("#messagelist").wait_for()


def open_chooser(page) -> None:
    # Roundcube intentionally has no Gmail-style multi-account chooser.  The
    # real equivalent is its visible Logout action followed by another mailbox
    # login, split across this operation and switch_account().
    page.locator(
        'a.button-logout, a[aria-label*="logout" i], a[title*="logout" i]'
    ).first.click()
    page.locator(LOGIN_USER).wait_for()


def switch_account(page, email: str) -> None:
    _roundcube_login(page, email, _password(page))


def list_rows(page) -> list[dict]:
    return page.locator(MESSAGE_ROWS).evaluate_all(
        """rows => rows.map(row => ({
            id: String(row.dataset.uid || ''),
            sender: (row.querySelector('.fromto, .from')?.textContent || '').trim(),
            subject: (row.querySelector('.subject a, .subject')?.textContent || '').trim(),
            time: (row.querySelector('.date')?.textContent || '').trim(),
            unread: row.classList.contains('unread'),
            attachment: !!row.querySelector('.attachment'),
            aria: row.getAttribute('aria-label') || ''
        }))"""
    )


def open_message(page, mail_id: str) -> None:
    try:
        subject = MESSAGE_SUBJECTS[mail_id]
    except KeyError as exc:
        raise ValueError(f"unknown seeded Roundcube message {mail_id!r}") from exc
    page.get_by_role("link", name=subject, exact=True).click()
    page.locator("#messageheader, .message-partheaders").first.wait_for()
    setattr(page, "_showAndTell_mailfix_message_id", mail_id)


def read_message(page) -> dict:
    subject = page.locator(
        "#messageheader .subject, .message-partheaders .subject, .header-title"
    ).first
    sender = page.locator(
        "#messageheader .from a, .message-partheaders .from a, .header.from a"
    ).first
    sender_text = sender.inner_text().strip()
    address = sender.get_attribute("href") or sender.get_attribute("data-email") or ""
    return {
        "id": str(getattr(page, "_showAndTell_mailfix_message_id", "")),
        "subject": subject.inner_text().strip(),
        "sender": sender_text,
        "sender_email": address.removeprefix("mailto:"),
    }


def back_to_inbox(page) -> None:
    page.locator(
        'a.back-list-button, a.button.back, a[aria-label*="back" i]'
    ).first.click()
    page.locator("#messagelist").wait_for()


def search_mail(page, query: str) -> None:
    search = page.locator(SEARCH_INPUT).first
    search.fill(query)
    search.press("Enter")
    page.locator("#messagelist").wait_for()


def _retired_messaging_surface() -> None:
    raise RuntimeError(
        "the messaging surface for this archived workflow has been retired"
    )


def open_slack(page, app_url: str) -> None:
    del page, app_url
    _retired_messaging_surface()


def post_slack_ping(page, text: str) -> None:
    del page, text
    _retired_messaging_surface()


def list_slack_messages(page) -> list[dict]:
    del page
    _retired_messaging_surface()


__all__ = [
    "back_to_inbox",
    "list_rows",
    "list_slack_messages",
    "login",
    "open_chooser",
    "open_message",
    "open_personal_inbox",
    "open_slack",
    "post_slack_ping",
    "read_message",
    "search_mail",
    "switch_account",
]
