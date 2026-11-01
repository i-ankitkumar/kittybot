"""Keeping secrets out of messages that reach a human.

A Kittysplit error can contain the kitty URL, and that URL *is* the
credential: anyone holding it can see and edit the kitty. So no message
that is shown to the user — in the dashboard or in Telegram — is allowed
to carry a kitty link.
"""

from __future__ import annotations

import re

#: Anything that looks like a Kittysplit link.
SECRET_RE = re.compile(r"https?://(?:www\.)?kittysplit\.com/\S*", re.IGNORECASE)


def scrub(message: str) -> str:
    """Remove kitty URLs from a message before showing it to the user."""
    return SECRET_RE.sub("[kitty link]", message or "")
