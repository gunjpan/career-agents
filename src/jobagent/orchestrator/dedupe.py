import hashlib
import re

from jobagent.models.job import RawPosting

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    return _NON_ALNUM.sub(" ", text.lower()).strip()


def dedupe_key(p: RawPosting) -> str:
    """company + normalized title + normalized primary location (per CLAUDE.md)."""
    location = p.locations[0] if p.locations else ""
    return f"{_norm(p.company)}|{_norm(p.title)}|{_norm(location)}"


def job_id(key: str) -> str:
    """Short stable id derived from the dedupe key."""
    return hashlib.sha1(key.encode()).hexdigest()[:12]
