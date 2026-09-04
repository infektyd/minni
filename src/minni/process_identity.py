"""Best-effort OS process naming for the long-lived daemon."""

import logging

PROCESS_NAME = "minni"
logger = logging.getLogger(__name__)


def set_process_identity(name: str = PROCESS_NAME) -> bool:
    """Set the native process title; return whether the API call succeeded.

    Use the packaged extension to manage platform-specific argv storage and
    buffer lifetime. Never write into interpreter-owned memory ourselves or
    change Python's sys.argv, which startup argument parsing still needs.
    Naming failure must not stop the daemon from serving its socket.
    """
    if not isinstance(name, str) or not name or "\x00" in name:
        return False
    try:
        import setproctitle

        setproctitle.setproctitle(name)
    except Exception as exc:
        logger.warning("Could not set daemon process title: %s", exc)
        return False
    return True
