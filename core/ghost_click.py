"""Ghost cursor wrapper for human-like mouse movements.

Wraps python_ghost_cursor to provide Bezier-curve mouse paths
that bypass Akamai behavioral detection.
"""
from python_ghost_cursor.playwright_sync import create_cursor


def ghost_click(page, selector: str, wait_for_selector: float = 5000, wait_for_click: float = 80):
    """Move mouse along Bezier curve to element, then click."""
    cursor = create_cursor(page)
    cursor.click(selector, wait_for_selector=wait_for_selector, wait_for_click=wait_for_click)


def ghost_click_element(page, element):
    """Move mouse along Bezier curve to ElementHandle, then click."""
    cursor = create_cursor(page)
    cursor.click(element, wait_for_click=80)


def ghost_click_button_by_text(page, button_text: str) -> bool:
    """Find a visible button by its text content and ghost-click it.

    Equivalent to _click_drawer_button() but using ghost cursor
    instead of page.evaluate() JS click.

    Returns True if button found and clicked, False otherwise.
    """
    buttons = page.query_selector_all('button')
    target = None
    for btn in buttons:
        text = btn.text_content().strip()
        if text == button_text and btn.is_visible():
            target = btn  # pick the last visible match (drawer button)

    if target is None:
        return False

    cursor = create_cursor(page)
    cursor.click(target, wait_for_click=80)
    return True
