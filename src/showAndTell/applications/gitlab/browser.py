"""Verified GitLab CE browser operations for teach demonstrations.

Selectors verified live against a running GitLab CE 16.11 container (login,
issue list, assignee, comment, label, milestone, close). Each GitLab task's
`demonstrate.py` composes these primitives, so the UI selectors live in exactly
one place. They use GitLab's stable hooks (`data-testid`, `js-user-link`) and
should hold across 16.x / WebArena's ~16.0 image.
"""
from __future__ import annotations

import re

_ASSIGNEE_BLOCK = "[data-testid=assignee-block-container]"


def login(page, app_url: str, creds: dict) -> None:
    # Idempotent AND verified. Under OS input the fill/click become CGEvent
    # gestures that can miss; a missed keystroke or submit click leaves the
    # session logged out, and since navigation works anonymously the demo would
    # only fail later at the first auth-gated step (the comment box shows
    # "sign in to reply"). So confirm the sign-in form is actually gone — GitLab
    # redirects /users/sign_in to the dashboard once authenticated — and retry a
    # missed submit, matching the entry-guard/exit-verify pattern the routed ops
    # use. (Under Playwright the first attempt authenticates; the recheck is a
    # fast no-op.)
    for _ in range(4):
        page.goto(f"{app_url}/users/sign_in", wait_until="networkidle")
        if page.query_selector("input[name='user[login]']") is None:
            return  # authenticated: no sign-in form to fill
        # Verify the text actually landed before submitting: a just-(re)armed
        # Teach recorder swallows synthetic input for a beat after navigation,
        # so an immediate fill can leave the fields empty (seen live on the VM:
        # the recording shows the submit firing on a blank form) — and the
        # outer retry's goto only re-opens the same swallow window.
        for _ in range(3):
            page.fill("input[name='user[login]']", creds["email"])
            page.fill("input[name='user[password]']", creds["password"])
            if page.input_value("input[name='user[login]']") == creds["email"]:
                break
            page.wait_for_timeout(1500)
        page.click("form.gl-show-field-errors button[type=submit], input[name=commit]")
        # Wait for the sign-in form to actually DETACH — the navigation commit.
        # wait_for_load_state can resolve against the old document while the
        # login POST is still in flight (seen live: the rails log showed the
        # 302 + authenticated dashboard land one second AFTER the old check
        # raised), so success is "the form went away", polled with a real
        # timeout, not a load-state snapshot.
        try:
            page.wait_for_selector("input[name='user[login]']", state="detached",
                                   timeout=8000)
            return
        except Exception:
            continue  # form still up — submit was swallowed; retry
    raise RuntimeError("login did not take effect — still on the sign-in form after retries")


def open_issue_iids(page, app_url: str, project: str, state: str = "opened") -> list[str]:
    """IIDs of the project's issues in the given state, newest first.

    The issue list is Vue-rendered, and the static "New issue" link
    (/-/issues/new) is present before the rows hydrate — so poll for links that
    carry a numeric IID rather than trusting the first render. Returns [] only
    when the queue is genuinely empty (after the poll window)."""
    page.goto(f"{app_url}/{project}/-/issues/?state={state}", wait_until="networkidle")
    link = f"a[href*='/{project}/-/issues/']"
    for _ in range(15):
        iids: list[str] = []
        for a in page.query_selector_all(link):
            m = re.search(r"/-/issues/(\d+)(?:$|[?#])", a.get_attribute("href") or "")
            if m and m.group(1) not in iids:
                iids.append(m.group(1))
        if iids:
            return iids
        page.wait_for_timeout(1000)
    return []


def open_issue(page, app_url: str, project: str, iid: str) -> None:
    page.goto(f"{app_url}/{project}/-/issues/{iid}", wait_until="networkidle")
    page.wait_for_timeout(1500)  # sidebar widgets hydrate after load


def assignee(page) -> str | None:
    """Assignee username from the Assignee sidebar block (scoped so it can't
    grab the issue author), or None. Href is absolute; take the last segment."""
    block = page.query_selector(_ASSIGNEE_BLOCK)
    if block is None:
        return None
    link = block.query_selector("a.js-user-link[href]")
    if link is None:
        return None
    href = (link.get_attribute("href") or "").rstrip("/")
    return href.split("/")[-1] or None if href else None


def assign_self(page, username: str) -> None:
    # This GitLab's issue sidebar has no "Assign yourself" shortcut (assignment
    # goes through the Edit dropdown), so use the /assign quick action — robust
    # and uniform with set_milestone and the OS-input driver. A quick action
    # doesn't live-update the sidebar, so confirm after a reload.
    if assignee(page) == username:
        return
    quick_action(page, f"/assign @{username}")
    for _ in range(3):
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)
        if assignee(page) == username:
            return
    raise RuntimeError(f"assign self did not take effect: @{username}")


def issue_title(page) -> str:
    for sel in ("[data-testid=issue-title]", "h1.title", "h1"):
        el = page.query_selector(sel)
        if el and (el.inner_text() or "").strip():
            return el.inner_text().strip()
    return ""


def applied_labels(page) -> list[str]:
    """Titles of labels currently applied, read from the Labels sidebar block."""
    block = page.query_selector("[data-testid=sidebar-labels]")
    if block is None:
        return []
    return [e.inner_text().strip()
            for e in block.query_selector_all(".gl-label-text, [data-testid=selected-label]")
            if (e.inner_text() or "").strip()]


def current_milestone(page) -> str | None:
    """Milestone title currently set on the issue, or None. The sidebar value
    sits in [data-testid=select-milestone] and reads 'None' when unset."""
    block = page.query_selector("[data-testid=sidebar-milestones]")
    if block is None:
        return None
    val = block.query_selector("[data-testid=select-milestone]") or \
        block.query_selector("a[href*='/milestones/']")
    text = (val.inner_text().strip().splitlines()[0].strip() if val and val.inner_text() else "")
    return None if not text or text.lower() == "none" else text


def add_comment(page, text: str) -> None:
    snippet = text[:24]
    posted = f".note-text:has-text({snippet!r}), .timeline-entry:has-text({snippet!r})"
    # Entry-guard: a re-run (OSOps retries on a verify miss) must not double-post.
    if page.locator(posted).count() > 0:
        return
    page.fill("[data-testid=comment-field]", text)
    page.click("[data-testid=comment-button]")
    # Exit-verify: the posted note renders in the discussion thread.
    page.wait_for_selector(posted, timeout=15000)


def add_label(page, label: str) -> None:
    # The sidebar label picker renders as small portal-mounted dropdown items
    # that a real OS click misses, so apply the /label quick action instead (as
    # assign_self / set_milestone do). Quick actions don't live-update the
    # sidebar, so re-attempt and confirm after a reload; the entry-guard at the
    # top of the loop keeps a re-run from re-applying.
    for _ in range(4):
        if label in applied_labels(page):
            return
        quick_action(page, f'/label ~"{label}"')
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1000)
    raise RuntimeError(f"add label did not take effect: {label}")


def quick_action(page, command: str) -> None:
    """Apply a GitLab slash quick action via the comment box (e.g.
    '/milestone %"Sprint 1"'). Quick actions are stable across GitLab versions,
    unlike the portal-rendered sidebar dropdowns."""
    page.fill("[data-testid=comment-field]", command)
    page.click("[data-testid=comment-button]")
    page.wait_for_timeout(1500)


def set_milestone(page, title: str) -> None:
    # The sidebar milestone picker renders its menu in a body-level portal that
    # is awkward to drive reliably; the /milestone quick action is robust. The
    # quick action doesn't live-update the sidebar, so re-attempt and confirm
    # after a reload (entry-guard makes re-attempting harmless).
    for _ in range(3):
        if current_milestone(page) == title:
            return
        quick_action(page, f'/milestone %"{title}"')
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1000)
    raise RuntimeError(f"set milestone did not take effect: {title}")


def _is_closed(page) -> bool:
    btn = page.locator("[data-testid=close-reopen-button]")
    return btn.count() > 0 and "reopen" in (btn.first.inner_text() or "").lower()


def close_issue(page) -> None:
    # The close/reopen control is a TOGGLE: clicking it when the issue is already
    # closed would reopen it. Entry-guard on the button label so a re-run (or a
    # missed OS click that actually landed) can never flip it back.
    for _ in range(4):
        if _is_closed(page):
            return
        btn = page.locator("[data-testid=close-reopen-button]")
        if btn.count() == 0:
            btn = page.get_by_role("button", name=re.compile("Close issue", re.I))
        btn.first.click()
        page.wait_for_timeout(900)
    raise RuntimeError("close issue did not take effect")


def has_own_comment(page, username: str) -> bool:
    """True if the discussion already has a note authored by `username`."""
    for a in page.query_selector_all(".notes .note a.js-user-link[href], .timeline-entry a.author-link[href]"):
        if (a.get_attribute("href") or "").rstrip("/").split("/")[-1] == username:
            return True
    return False
