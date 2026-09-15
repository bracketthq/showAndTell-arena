"""Postmill browser operations for teach demonstrations.

Each reddit task's `demonstrate.py` composes these primitives, so the UI
selectors live in exactly one place. They are written for Postmill — the
Symfony forum app in WebArena's `postmill-populated-exposed-withimg` image —
from its templates: /login with `_username`/`_password` inputs, `.submission`
rows titled by a `.submission__title` link, vote forms with `.vote__up` /
`.vote__down` buttons (a "voted" modifier class marks an existing vote), and a
`comment[body]` textarea. Unlike gitlab_ui these selectors have NOT yet been
verified against the live WebArena image, so readers accept alternative markup
(class fallbacks, aria-pressed) and the list readers poll for rendered rows
the way gitlab_ui.open_issue_iids does.
"""
from __future__ import annotations

import re

from showAndTell.core.pwerrors import PlaywrightError

_SETTLE_MS = 800
_POLL_ROUNDS = 15
_FORUM_HREF = re.compile(r"^/f/([^/?#]+)/?$")


def login(page, app_url: str, creds: dict) -> None:
    # Idempotent: the managed Chrome profile persists the session across runs,
    # so /login may render the signed-in chrome with no login form.
    page.goto(f"{app_url}/login", wait_until="networkidle")
    if page.query_selector("input[name=_username]") is None:
        return  # already signed in
    page.fill("input[name=_username]", creds["email"])
    page.fill("input[name=_password]", creds["password"])
    page.click("form:has(input[name=_username]) button[type=submit]")
    page.wait_for_load_state("networkidle")


def goto_forum(page, app_url: str, forum: str, newest: bool = True) -> None:
    """Open a forum, newest-first by default: the tasks triage 'the newest
    posts', and /new gives a deterministic order that hot-ranking does not."""
    path = f"/f/{forum}/new" if newest else f"/f/{forum}"
    page.goto(f"{app_url}{path}", wait_until="networkidle")


def _strip_origin(href: str) -> str:
    return re.sub(r"^https?://[^/]+", "", href or "")


def _vote_state(scope) -> str | None:
    """'up' | 'down' | None from the vote buttons. Postmill marks an existing
    vote with a modifier class on the button; accept any 'voted'/'--active'
    token or aria-pressed since the live image's exact markup is unverified."""
    for direction in ("up", "down"):
        btn = scope.query_selector(f".vote__{direction}")
        if btn is None:
            continue
        classes = btn.get_attribute("class") or ""
        if ("voted" in classes or "--active" in classes
                or btn.get_attribute("aria-pressed") == "true"):
            return direction
    return None


def _comment_count_from(text: str) -> int:
    # Postmill renders 'no comments' at zero, else '1 comment' / 'N comments'.
    m = re.search(r"(\d+)\s+comment", text or "")
    return int(m.group(1)) if m else 0


def list_posts(page) -> list[dict]:
    """The rendered submission rows: title, comment count, my current vote (if
    detectable), and the permalink for open_post. The permalink comes from the
    comments link — on a link post the title anchor points at the external URL.
    Rows are server-rendered but poll anyway (mirrors open_issue_iids) so a
    slow page can't come back empty."""
    for _ in range(_POLL_ROUNDS):
        posts: list[dict] = []
        for row in page.query_selector_all("article.submission, .submission"):
            link = row.query_selector(".submission__title a, h1.submission__title a")
            if link is None:
                continue
            # The comment count is the row link whose TEXT reads "N comments" (its
            # href is the post permalink, not a /comment url, and the class is
            # submission__new-comments not __comments) — so match by text.
            comments = None
            for a in row.query_selector_all("a"):
                if re.search(r"\bno comments\b|\d+\s+comment", (a.inner_text() or ""), re.I):
                    comments = a
                    break
            perma = _strip_origin(comments.get_attribute("href") if comments else "")
            posts.append({
                "title": (link.inner_text() or "").strip(),
                "url": perma or _strip_origin(link.get_attribute("href") or ""),
                "num_comments": _comment_count_from(comments.inner_text() if comments else ""),
                "my_vote": _vote_state(row),
            })
        if posts:
            return posts
        page.wait_for_timeout(1000)
    return []


def _row_for(page, post: dict):
    """Re-locate a listing row for `post` (by its permalink, then title). Rows
    are re-found rather than cached so a vote's in-place re-render can't hand
    back a stale handle."""
    url, title = post.get("url"), post.get("title")
    for row in page.query_selector_all("article.submission, .submission"):
        link = row.query_selector("a[href*='comment'], .submission__comments")
        href = _strip_origin(link.get_attribute("href") or "") if link else ""
        if url and href and href.rstrip("/") == url.rstrip("/"):
            return row
        tl = row.query_selector(".submission__title a")
        if title and tl and (tl.inner_text() or "").strip() == title:
            return row
    return None


def post_vote(page, post: dict) -> str | None:
    """My current vote on a post, read from its listing row ('up'|'down'|None).
    Lets a demo triage from the newest-posts list without opening each post."""
    row = _row_for(page, post)
    return _vote_state(row) if row is not None else None


def _vote_post(page, post: dict, direction: str) -> None:
    # Click the vote button in the post's LISTING row — a real interaction the
    # Show-and-Tell recorder captures, unlike a page.goto into the post. Postmill
    # votes toggle, so never re-click an existing vote (that retracts it).
    row = _row_for(page, post)
    if row is None:
        raise RuntimeError(f"listing row not found for {post.get('title')!r}")
    if _vote_state(row) == direction:
        return
    btn = row.query_selector(f".vote__{direction}")
    if btn is None:
        raise RuntimeError(f"vote {direction} button not in row {post.get('title')!r}")
    btn.click()
    page.wait_for_timeout(_SETTLE_MS)


def upvote_post(page, post: dict) -> None:
    _vote_post(page, post, "up")


def downvote_post(page, post: dict) -> None:
    _vote_post(page, post, "down")


def open_post(page, app_url: str, post) -> None:
    """Open a post by CLICKING its title link in the listing (so the recorder
    captures the navigation), falling back to a direct visit only if the link
    isn't on the current page. Accepts a post dict or a raw url/href."""
    url = post["url"] if isinstance(post, dict) else post
    link = page.query_selector(f"a[href$='{url}']") or \
        page.query_selector(f"a[href*='{url.rstrip('/')}']")
    target = url if url.startswith("http") else f"{app_url}{url}"
    try:
        if link is not None:
            link.click()
            page.wait_for_load_state("networkidle")
        else:
            page.goto(target, wait_until="networkidle")
    except PlaywrightError as exc:
        # Postmill occasionally aborts a navigation while replacing the forum
        # listing document. Retrying the same organic permalink after the
        # aborted request is safe and avoids losing a long queue walk.
        if "ERR_ABORTED" not in str(exc):
            raise
        page.goto(target, wait_until="domcontentloaded")
    page.wait_for_timeout(_SETTLE_MS)


def back_to_forum(page, app_url: str, forum: str) -> None:
    """Return to a forum's newest listing by clicking its breadcrumb/link on the
    current post page (a real click), falling back to a visit if absent."""
    link = page.query_selector(f"a[href$='/f/{forum}/new'], a[href$='/f/{forum}'], a[href*='/f/{forum}']")
    if link is not None:
        link.click()
        page.wait_for_load_state("networkidle")
    else:
        goto_forum(page, app_url, forum)


def _submission(page):
    """The submission article on an open post page — vote reads/clicks are
    scoped to it so a comment's vote widget can never be mistaken for the
    post's."""
    return page.query_selector("article.submission, .submission") or page


def post_title(page) -> str:
    for sel in (".submission__title", "h1.submission__title", "h1"):
        el = page.query_selector(sel)
        if el and (el.inner_text() or "").strip():
            return el.inner_text().strip()
    return ""


def comment_count(page) -> int:
    """Comment count on an open post. Prefer the 'N comments' text (it exists
    even at zero, as 'no comments'); fall back to counting rendered comments."""
    el = page.query_selector(".submission__comments, a[href$='#comments']")
    if el and (el.inner_text() or "").strip():
        return _comment_count_from(el.inner_text())
    return len(page.query_selector_all(".comment"))


def my_vote(page) -> str | None:
    return _vote_state(_submission(page))


def _vote(page, direction: str) -> None:
    # Never click a vote that's already recorded: Postmill vote buttons
    # toggle, so a second click would RETRACT the vote instead of confirming.
    if _vote_state(_submission(page)) == direction:
        return
    btn = _submission(page).query_selector(f".vote__{direction}")
    if btn is None:
        raise RuntimeError(f"vote {direction} button not found")
    btn.click()
    page.wait_for_timeout(_SETTLE_MS)


def upvote(page) -> None:
    _vote(page, "up")


def downvote(page) -> None:
    _vote(page, "down")


# Postmill names the top-level reply field `reply_to_submission_<id>[comment]`
# (older builds used `comment[body]`); per-comment reply forms (hidden until
# "reply" is clicked) share the `[comment]` suffix, so target the submission
# form by its `reply_to_submission_` prefix and fall back to the older name.
_COMMENT_FIELD = ("textarea[name^='reply_to_submission_'], "
                  "textarea[name='comment[comment]'], textarea[name='comment[body]']")


def add_comment(page, text: str) -> None:
    field = page.locator(_COMMENT_FIELD).first
    field.fill(text)
    # Postmill's submit is a plain `<button class="button">Post</button>` (no
    # type attribute), alongside the Markdown toolbar's type=button buttons —
    # so match the labelled submit, falling back to the form's non-toolbar button.
    form = page.locator(f"form:has({_COMMENT_FIELD})").first
    labelled = form.get_by_role("button", name=re.compile(r"post|comment|reply|submit|save", re.I))
    (labelled.first if labelled.count() else form.locator("button:not([type=button])").last).click()
    # the posted comment renders in the thread
    page.wait_for_selector(f".comment:has-text({text[:24]!r})", timeout=15000)


def has_own_comment(page, username: str) -> bool:
    """True if any rendered comment is authored by `username` (Postmill
    comment bylines link to /user/<name>)."""
    for a in page.query_selector_all(".comment a[href*='/user/']"):
        href = _strip_origin(a.get_attribute("href") or "").split("?")[0]
        if href.rstrip("/").split("/")[-1] == username:
            return True
    return False


def _subscribed_state(scope) -> bool:
    """Subscribed iff the toggle currently offers Unsubscribe — read from the
    form action or the button label, whichever this build renders."""
    if scope.query_selector("form[action*='unsubscribe']") is not None:
        return True
    btn = scope.query_selector("form[action*='subscribe'] button, .subscribe-button")
    return bool(btn and re.search(r"\bunsubscribe\b", btn.inner_text() or "", re.I))


def forum_subscribed(page) -> bool:
    """Whether I'm subscribed to the forum currently open (/f/<name>), read
    from its sidebar subscribe toggle."""
    return _subscribed_state(page)


def subscribe(page) -> None:
    """Subscribe to the forum currently open. Idempotent: no-ops when already
    subscribed rather than clicking what is then an Unsubscribe toggle."""
    if forum_subscribed(page):
        return
    btn = page.query_selector("form[action*='subscribe'] button, .subscribe-button")
    if btn is None:
        raise RuntimeError("subscribe button not found")
    btn.click()
    page.wait_for_load_state("networkidle")
    for _ in range(5):
        if forum_subscribed(page):
            return
        page.wait_for_timeout(400)
    raise RuntimeError("subscribe did not take effect")


def _forum_row(page, name: str):
    a = page.query_selector(f"a[href='/f/{name}'], a[href$='/f/{name}']")
    if a is None:
        return None
    return a.evaluate_handle(
        "el => el.closest('li, tr, article, .flex-grid__item') || el.parentElement"
    ).as_element()


def subscribe_forum(page, name: str) -> None:
    """Subscribe to a forum by clicking the subscribe button in its /forums
    directory row — a captured click, no page-jump into the forum. Idempotent:
    no-ops if the row already shows Unsubscribe."""
    row = _forum_row(page, name)
    if row is None:
        raise RuntimeError(f"forum row not found in directory: {name}")
    if _subscribed_state(row):
        return
    btn = row.query_selector("form[action*='subscribe'] button, .subscribe-button")
    if btn is None:
        raise RuntimeError(f"subscribe button not in directory row: {name}")
    btn.click()
    page.wait_for_load_state("networkidle")


def search_forums(page, app_url: str, query: str) -> list[dict]:
    """The forum directory (/forums): name, description, subscribed per forum.

    Postmill's /forums box is the site-wide submission search (pressing Enter
    navigates away from the directory), and the directory itself doesn't filter
    by keyword, so return the whole directory and let the caller's rule match
    `query` against name/description. The reddit tasks are written for exactly
    this: a non-matching row is treated as a fuzzy hit to skip, so surfacing all
    forums is intended, not a leak."""
    page.goto(f"{app_url}/forums", wait_until="networkidle")
    box = page.query_selector("input[name=q], input[type=search]")
    if box is not None:
        box.fill(query)  # show the search gesture; don't submit (that leaves the directory)
    rows: list[dict] = []
    for _ in range(_POLL_ROUNDS):
        rows = _forum_rows(page)
        if rows:
            break
        page.wait_for_timeout(500)
    return rows


def _forum_rows(page) -> list[dict]:
    """Directory rows located from their /f/<name> links; the row container is
    whatever list/table element this build renders, found via closest()."""
    rows: list[dict] = []
    seen: set[str] = set()
    for a in page.query_selector_all("a[href*='/f/']"):
        m = _FORUM_HREF.match(_strip_origin(a.get_attribute("href") or ""))
        if not m or m.group(1) in seen:
            continue
        row = a.evaluate_handle(
            "el => el.closest('li, tr, article, .flex-grid__item') || el.parentElement"
        ).as_element()
        if row is None:
            continue
        seen.add(m.group(1))
        desc = row.query_selector(".forum-list__description, p, small")
        rows.append({
            "name": m.group(1),
            "description": (desc.inner_text() or "").strip() if desc else "",
            "subscribed": _subscribed_state(row),
        })
    return rows
