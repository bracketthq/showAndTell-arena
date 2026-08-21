"""ONLYOFFICE's browser plane: driving a spreadsheet inside the editor iframe.

Document Server renders the sheet in a nested frame, so every operation here
resolves that frame first — a locator against the top document finds nothing.
There is no login: the connector's editor page authenticates the session by
its document URL.
"""
from __future__ import annotations

EDITOR_FRAME = "iframe[name='frameEditor'], iframe#frameEditor"
CELL_INPUT = "#ce-cell-content, .ce-input"
# The hidden textarea that receives keyboard input for the selected worksheet
# cell.  Name Box navigation is asynchronous in Document Server: Enter can
# leave the Name Box focused for a short (and occasionally unbounded) interval,
# which makes the next real OS keystrokes overwrite the cell reference instead
# of editing the selected cell.
CELL_EDIT_INPUT = ("#area_id, #area_id_parent textarea, "
                   "#area_id_main textarea, #editor_sdk textarea")
# Document Server moved the Name Box from a wrapper to a bare input (9.4), so
# the wrapper form is tried first and the element itself is the fallback --
# never one comma-joined selector, which resolves in DOM order and would pick
# whichever shape happened to come first.
NAME_BOX = ("#ce-cell-name input, #ce-cellname input",
            "#ce-cell-name, #ce-cellname")
# DS raises its own warnings ("This file is opened from a server backup
# copy...") as alerts behind a full-viewport mask.
MODAL_ALERT = ".asc-window[role='alertdialog']"
# Community Document Server shows this first-use teaching card shortly after
# the grid becomes interactive. It is not an ARIA alertdialog, but it still
# covers cells and makes the editor ignore clicks beneath it.
FIRST_USE_TIP_BUTTON = ".synch-tip-root .btn-div"


def login(page, app_url: str, creds: dict) -> None:
    """Open the editor. The connector's URL is the credential."""
    del creds
    page.goto(app_url, wait_until="domcontentloaded")
    ready(page)


def editor(page):
    """The editing surface, once Document Server has mounted it."""
    page.locator(EDITOR_FRAME).first.wait_for(timeout=60_000)
    frame = page.frame_locator(EDITOR_FRAME).first
    frame.locator("#ws-canvas-outer, #ws-canvas").first.wait_for(timeout=60_000)
    return frame


def _dismiss_modal_alerts(frame) -> None:
    """Dismiss Document Server warnings without accepting their side effects."""
    for _ in range(3):
        alert = frame.locator(MODAL_ALERT)
        if not alert.count():
            break
        current = alert.first
        # Some save-failure warnings say that OK will open Download As. The
        # title-bar close control is still a native Document Server dismissal,
        # but unlike OK it only clears the mask and keeps the sheet in front.
        close = current.locator(".tools .tool.close, .tool.close")
        try:
            control = close.first if close.count() and close.first.is_visible() \
                else current.locator(".footer button, button, .dlg-btn").first
            control.click(timeout=10_000)
        except Exception as exc:                                # noqa: BLE001
            # Louder than the silent wrong-cell writes a live mask produces.
            raise RuntimeError(
                "a Document Server dialog is covering the editor and could not "
                f"be dismissed: {exc}") from exc


def ready(page) -> None:
    """Block until the spreadsheet can actually be driven.

    Mounting the editor is not the same as being able to use it, and the gap is
    silent: under a modal mask every selector still resolves and the Name Box
    still accepts a cell reference, but the grid never receives the navigation,
    so a value is typed into whichever cell was already selected and nothing
    reports a thing.  Clearing what is modal is therefore part of being ready.
    """
    frame = editor(page)
    _dismiss_modal_alerts(frame)

    # The tip is mounted asynchronously, after the canvas and Name Box are
    # already ready. Give that one known overlay a short chance to appear;
    # once visible, dismiss it through its own control so later cell clicks
    # reach the grid. A workbook/profile that has seen it before simply pays
    # the bounded wait and continues.
    tip = frame.locator(FIRST_USE_TIP_BUTTON)
    try:
        tip.first.wait_for(state="visible", timeout=2_500)
    except Exception:                                        # noqa: BLE001
        return
    # The card can also tear itself down between that wait and the click
    # landing. ready() runs after every replay navigation, outside the
    # gesture recovery wrapper — a tip that is gone is a cleared editor,
    # so only one that is still covering cells may end the replay.
    try:
        tip.first.click(timeout=5_000)
    except Exception as exc:                                 # noqa: BLE001
        if tip.count():
            raise RuntimeError(
                "the first-use tip is covering the editor and could not "
                f"be dismissed: {exc}") from exc


def _name_box(frame):
    for selector in NAME_BOX:
        candidate = frame.locator(selector)
        if candidate.count():
            return candidate.first
    raise RuntimeError("the editor has no Name Box to navigate with")


def _focus_cell_editor(frame) -> None:
    """Put subsequent keyboard input on the selected worksheet cell."""
    edit = frame.locator(CELL_EDIT_INPUT)
    if not edit.count():
        raise RuntimeError("the editor has no worksheet cell input")
    edit.first.focus()
    if not edit.first.evaluate("el => el === el.ownerDocument.activeElement"):
        raise RuntimeError("the worksheet cell input did not receive focus")


def select_cell(page, reference: str) -> None:
    """Go to a cell by its reference, the way the Name Box does."""
    frame = editor(page)
    # Save warnings can appear while another application is in front. If the
    # Name Box is disabled beneath that mask, the Enter used to commit an edit
    # would accept the warning instead and can open a full-screen Download As
    # panel. Clear alerts before interpreting the disabled box as an edit.
    _dismiss_modal_alerts(frame)
    name_box = _name_box(frame)
    # Generated replays preserve a human double-click as two semantic cell
    # selections.  Re-submitting an already-active reference through the Name
    # Box can race Document Server's focus handoff: the literal reference is
    # then committed into the cell and Enter advances to the row below.  Treat
    # selection as idempotent and only restore the worksheet keyboard target.
    if name_box.input_value().strip().upper() == reference.strip().upper():
        _focus_cell_editor(frame)
        return
    if not name_box.is_enabled():
        # A real canvas click both commits an active edit and moves to another
        # cell. Semantic replay uses the Name Box for the second effect, but
        # Document Server disables that box until the first effect completes.
        # Enter commits through the currently focused editor control; selecting
        # the recorded reference immediately afterwards makes Enter's own
        # movement irrelevant while preserving the typed value.
        page.keyboard.press("Enter")
        deadline = 100
        while deadline and not name_box.is_enabled():
            page.wait_for_timeout(50)
            deadline -= 1
        if not name_box.is_enabled():
            raise RuntimeError(
                "the ONLYOFFICE Name Box stayed disabled after committing "
                "the active cell edit")
    name_box.fill(reference)
    name_box.press("Enter")
    _focus_cell_editor(frame)


def type_value(page, value: str) -> None:
    frame = editor(page)
    frame.locator(CELL_INPUT).first.fill(str(value))
    frame.locator(CELL_INPUT).first.press("Enter")


def cell_value(page, reference: str) -> str:
    """What the formula bar shows for a cell.

    Read as a form value: the bar is a <textarea>, whose rendered text is its
    markup default and stays empty however the editor fills it.
    """
    select_cell(page, reference)
    return editor(page).locator(CELL_INPUT).first.input_value().strip()


__all__ = ["cell_value", "editor", "login", "ready", "select_cell", "type_value"]
