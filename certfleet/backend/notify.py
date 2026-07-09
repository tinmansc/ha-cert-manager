"""Send notifications to the Home Assistant UI via the Supervisor's Core API proxy.

Uses SUPERVISOR_TOKEN — injected automatically by the Supervisor into
every add-on's environment — instead of a user-managed long-lived access
token. There's no credential to generate, paste into a config file, or
accidentally leak. (An earlier prototype script in this project's history
did exactly that with a hand-pasted HA token, which is part of why it's
no longer used — see RELEASE_CHECKLIST.md.)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_SUPERVISOR_CORE_URL = "http://supervisor/core/api/services/persistent_notification/create"


def notify_ha(title: str, message: str, notification_id: str = "certfleet") -> bool:
    """Create or update a persistent notification in the HA UI (bell icon).

    Reusing the same notification_id updates the existing card instead of
    stacking a new one on every call — callers should pass a stable,
    purpose-specific id per notification type (e.g. one for deploy
    results, a different one for cert-read failures) so unrelated events
    don't clobber each other.

    Returns True if the call reached Home Assistant successfully. Never
    raises — a notification failure should never break the underlying
    operation it was reporting on.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return False
    try:
        req = urllib.request.Request(
            _SUPERVISOR_CORE_URL,
            data=json.dumps({
                "title": title,
                "message": message,
                "notification_id": notification_id,
            }).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False
