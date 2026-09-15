"""Roundcube's browser plane: Playwright operations against the real webmail.

``login`` is what capture and replay both need. The operations below it are the
semantic vocabulary a demonstration is recorded in, so they name what a person
does — open the inbox, read a message — rather than which selector was clicked.

Every selector here is verified against roundcubemail:1.6.17 with the elastic
skin, which is what ``compose.yaml`` pins. Three things about that skin are not
guessable and each one silently breaks a plausible-looking implementation:

* ``.subject`` matches twice per row — the whole ``<td>`` and an inner
  ``<span>`` — so a bare ``.subject`` lookup returns the sender, date and size
  concatenated with the subject. The anchor inside it is the subject alone.
* The subject anchor carries ``onclick="return rcube_event.keyboard_only(...)"``
  and therefore ignores mouse clicks entirely. A message is opened by clicking
  its row; the anchor exists for keyboard navigation.
* An opened message renders inside the ``#messagecontframe`` iframe, so nothing
  about it is reachable from the top document.
"""
from __future__ import annotations

import contextlib
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

LOGIN_USER = 'input[name="_user"]'
LOGIN_PASSWORD = 'input[name="_pass"]'
MESSAGE_LIST = "#messagelist"
MESSAGE_ROWS = "#messagelist tbody tr"
SUBJECT_LINK = ".subject a"
PREVIEW = "#messagecontframe"
SEARCH_INPUT = 'input[name="_q"]'
LOGOUT = "a.logout"
ACCOUNT = ".username"
# The recipient tokenizer is built by compose init out of the To textarea;
# it is the only compose control that is not in the server-rendered document,
# so its input existing is what "compose has finished wiring itself" means.
# Scoped to the To row: Cc/Bcc rows carry tokenizers of their own.
COMPOSE_RECIPIENT_INPUT = "#compose_to .recipient-input input"


def replay_completion(kind: str, target: dict) -> str | None:
    """Name the Roundcube actions whose asynchronous outcome must settle."""
    if (
        kind == "click"
        and target.get("role") == "button"
        and target.get("name") == "Send"
    ):
        return "send"
    return None


def wait_for_replay_completion(page, completion: str) -> None:
    """Wait until a sent draft has reached Roundcube's usable inbox view."""
    if completion != "send":
        raise ValueError(f"unknown Roundcube replay completion: {completion!r}")

    deadline = time.monotonic() + 30.0
    while True:
        try:
            query = dict(parse_qsl(
                urlsplit(str(page.url)).query, keep_blank_values=True))
        except Exception:
            query = None

        is_inbox = (
            query is not None
            and query.get("_task", "mail") == "mail"
            and query.get("_action", "") in ("", "list")
        )
        if is_inbox:
            try:
                page.locator(MESSAGE_LIST).wait_for(
                    state="attached", timeout=250)
            except Exception:
                pass
            else:
                return

        if time.monotonic() >= deadline:
            raise RuntimeError("Roundcube did not confirm message delivery")
        page.wait_for_timeout(100)


def normalize_replay_url(url: str) -> str:
    """Drop compose-session state that cannot survive a fresh task runtime.

    Roundcube's ``_id`` names an in-memory compose session. Reusing an ID
    captured from another deployment opens a COMPOSE SESSION ERROR page even
    when the rest of the route is valid. Other routes can use ``_id`` for
    unrelated purposes, so only fresh compose navigation is normalized.
    A URL with nothing to strip is returned byte-identical: re-encoding an
    already-valid address would shift bytes the replay driver's byte-equality
    contract has no reason to see move.
    """
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    values = dict(query)
    if values.get("_task") != "mail" or values.get("_action") != "compose":
        return url
    if "_id" not in values:
        return url
    normalized = [(key, value) for key, value in query if key != "_id"]
    return urlunsplit(parts._replace(query=urlencode(normalized)))


def _await_mail(page) -> None:
    """Wait for the folder to have finished loading.

    Two traps, and the obvious fix for either one causes the other. Waiting
    for #messagelist to be VISIBLE hangs on an empty mailbox, which is exactly
    the state a freshly seeded run starts from -- Roundcube leaves the table
    attached but hidden when there is nothing in it. Waiting only for it to be
    ATTACHED returns before the folder has loaded, so a caller reading rows
    straight afterwards sees none. Settling the network covers both.
    """
    page.locator(MESSAGE_LIST).wait_for(state="attached")
    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle")


def ready(page) -> None:
    """Block until the view this page's URL names can actually be driven.

    Compose is the route that builds its surface after the document loads:
    the URL already says ``_action=compose`` while the widgets replay needs
    are still being wired, and a background tab's throttled timers stretch
    that gap to whole seconds. Waiting on server-rendered controls proves
    nothing — they are attached, and visible, before compose init has run —
    so the gate is the tokenizer input that init itself creates. Folder
    views settle the same way login does. Every other route declares no
    gate: a full-page message, settings, or the addressbook has no
    #messagelist, and waiting on one there turns a readiness gate into a
    thirty-second hang.
    """
    query = dict(parse_qsl(urlsplit(str(page.url)).query,
                           keep_blank_values=True))
    if query.get("_task", "mail") != "mail":
        return
    action = query.get("_action", "")
    if action == "compose":
        page.locator(COMPOSE_RECIPIENT_INPUT).wait_for(
            state="visible", timeout=60_000)
        return
    if action in ("", "list"):
        _await_mail(page)


def signed_in_as(page) -> str:
    """The account Roundcube currently holds a session for, if any."""
    account = page.locator(ACCOUNT)
    if not account.count():
        return ""
    return (account.first.inner_text() or "").strip()


def _sign_in(page, email: str, password: str) -> None:
    page.locator(LOGIN_USER).fill(email)
    page.locator(LOGIN_PASSWORD).fill(password)
    page.get_by_role(
        "button", name=re.compile(r"^(login|log in)$", re.IGNORECASE)
    ).click()
    _await_mail(page)


def login(page, app_url: str, creds: dict) -> None:
    """Sign in if there is a form, and land on the inbox either way.

    Shaped like ERPNext's: a visible form is filled, an absent one means the
    session is already authenticated and is left alone. That makes the call
    idempotent, which it has to be -- a trial signs a surface in when it opens
    it and again when it replays the setup gestures.

    Deliberately no "signed in as somebody else, so sign out" branch. Roundcube
    has no account switch and signing out is a real, visible user action, so it
    belongs in open_chooser/switch_account where a task asks for it explicitly.
    """
    page.goto(f"{app_url.rstrip('/')}/", wait_until="domcontentloaded")
    # Remembered so switch_account() can sign back in without being handed the
    # password again part-way through a demonstration.
    setattr(page, "_showAndTell_roundcube_password", creds["password"])
    user = page.locator(LOGIN_USER)
    if user.count() and user.first.is_visible():
        _sign_in(page, creds["email"], creds["password"])
        return
    _await_mail(page)


def open_inbox(page, app_url: str) -> None:
    page.goto(f"{app_url.rstrip('/')}/?_task=mail&_mbox=INBOX",
              wait_until="networkidle")
    _await_mail(page)


def open_chooser(page) -> None:
    """Roundcube has no account chooser; logging out is the real equivalent."""
    page.locator(LOGOUT).first.click()
    page.locator(LOGIN_USER).wait_for()


def switch_account(page, email: str) -> None:
    password = getattr(page, "_showAndTell_roundcube_password", None)
    if not password:
        raise RuntimeError("switching Roundcube accounts needs the mailbox password")
    _sign_in(page, email, password)


def list_rows(page) -> list[dict]:
    """One entry per message, as a person reads the list."""
    return page.locator(MESSAGE_ROWS).evaluate_all(
        """rows => rows.map(row => {
            const link = row.querySelector('.subject a');
            const href = link ? link.getAttribute('href') || '' : '';
            const uid = (href.match(/[?&]_uid=(\\d+)/) || [])[1] || '';
            const sender = row.querySelector('.fromto .rcmContactAddress');
            return {
                // The row id is Roundcube's own encoding; _uid is the IMAP
                // identity a seed and an assertion can both talk about.
                id: uid,
                sender: (sender?.textContent || '').trim(),
                sender_email: sender?.getAttribute('title') || '',
                subject: (link?.textContent || '').trim(),
                time: (row.querySelector('.date')?.textContent || '').trim(),
                unread: row.classList.contains('unread'),
                attachment: !!row.querySelector('.attachment img, .attachment span'),
            };
        })"""
    )


def open_message(page, subject: str) -> None:
    """Open a message by the subject a person reads on screen.

    The row is clicked rather than the subject link: that anchor only responds
    to keyboard activation, so a mouse click on it does nothing at all.
    """
    row = page.locator(MESSAGE_ROWS).filter(
        has=page.locator(SUBJECT_LINK, has_text=subject))
    if not row.count():
        raise ValueError(f"no message in the list with subject {subject!r}")
    row.first.locator("td.subject").click()
    page.frame_locator(PREVIEW).locator(".subject").first.wait_for()


def read_message(page) -> dict:
    """The opened message, read out of the preview frame."""
    frame = page.frame_locator(PREVIEW)
    sender = frame.locator('a[href^="mailto:"]').first
    address = sender.get_attribute("href") or ""
    return {
        "subject": frame.locator(".subject").first.inner_text().strip(),
        "sender": sender.inner_text().strip(),
        "sender_email": address.removeprefix("mailto:"),
        "body": frame.locator("#messagebody").first.inner_text().strip(),
    }


def back_to_inbox(page) -> None:
    """Return to the list. Elastic keeps it beside the preview on wide screens."""
    back = page.locator("a.back-list-button")
    if back.count() and back.first.is_visible():
        back.first.click()
    _await_mail(page)


def search_mail(page, query: str) -> None:
    search = page.locator(SEARCH_INPUT).first
    search.click()
    search.fill(query)
    search.press("Enter")
    _await_mail(page)


__all__ = [
    "back_to_inbox", "list_rows", "login", "normalize_replay_url",
    "open_chooser", "open_inbox", "open_message", "read_message", "ready",
    "search_mail", "signed_in_as", "switch_account",
]
