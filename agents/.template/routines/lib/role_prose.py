"""Role prose is domain description, never a stored owner address."""
import re
from owner_term import DEFAULT_OWNER_TERM, owner_term


def validate_role_prose(prose, *roots):
    """Reject known owner terms before any stamp writes.

    Include the historical fallback even when config now names a different
    term: old copied prose must not become acceptable after a config change.
    Neutral generic references remain valid. This is a literal input gate,
    not a natural-language classifier of every possible guessed title.
    """
    terms = {DEFAULT_OWNER_TERM, *(owner_term(root) for root in roots if root)}
    for term in terms:
        term = term.strip()
        if not term or term.casefold() in {"the user", "user", "you", "사용자"}:
            continue
        # Latin word boundaries avoid matching e.g. "boss" in "embossing";
        # Korean particles may immediately follow the configured address.
        tail = r"(?![A-Za-z0-9_])" if term.isascii() else ""
        if re.search(r"(?<!\w)" + re.escape(term) + tail, prose, re.IGNORECASE):
            raise ValueError("Role prose contains an owner address; use a domain-only description")
