import json
import logging
from typing import Optional


logger = logging.getLogger("minnid")

# SEC-015: cap a single JSON-RPC request body at 1 MiB to bound peak memory
# and protect the embedder from caller-controlled mega-payloads.
SOCKET_BODY_LIMIT = 1_048_576  # 1 MiB


def parse_request(data: bytes) -> Optional[dict]:
    """Parse a JSON-RPC request from raw bytes."""
    try:
        text = data.decode("utf-8").strip()
        if not text:
            return None
        return json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Invalid request: %s", exc)
        return None

