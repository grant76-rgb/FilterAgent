"""Best-effort Windows toast notification for long-running runs. A no-op if
the optional `plyer` dependency isn't installed or the platform backend
fails (e.g. no notification server available)."""


def notify(title: str, message: str):
    try:
        from plyer import notification
        notification.notify(title=title, message=message, timeout=10)
    except Exception:
        pass
