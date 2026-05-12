"""Helpers for parsing pipeline.yaml `roles:` and `branches:` blocks.

Both formats supported:
- New (uniform list-of-dicts):
    roles:
      - role: writer
        prompt: agents/x.md
      - role: critic           # repeated entries with same role -> grouped as list
        name: safety
        prompt: ...
      - role: critic
        name: elegance
        prompt: ...

- Old (dict-of-dicts, kept for backward compat):
    roles:
      writer:
        prompt: agents/x.md
      critics:                 # explicit list
        - name: safety
          prompt: ...

Both normalize to the same dict shape that handlers consume:
    {"writer": {"prompt": ...}, "critic": [{"name": "safety", ...}, ...]}

For branches: accept `branch:` (new) or `name:` (old) as the slot key.
"""

from __future__ import annotations


def normalize_roles(roles) -> dict:
    """Return dict-of-dicts (or dict containing list for repeated roles).

    Single-instance roles -> dict value.
    Multi-instance roles (same `role:` key repeated) -> list of dicts value.
    """
    if isinstance(roles, dict):
        return roles  # old format passes through

    grouped: dict = {}
    for entry in roles:
        if not isinstance(entry, dict) or "role" not in entry:
            raise ValueError(f"role entry must be a dict with `role:` field, got {entry!r}")
        rname = entry["role"]
        clean = {k: v for k, v in entry.items() if k != "role"}
        if rname in grouped:
            existing = grouped[rname]
            if isinstance(existing, list):
                existing.append(clean)
            else:
                grouped[rname] = [existing, clean]
        else:
            grouped[rname] = clean
    return grouped


def normalize_branches(branches) -> list[dict]:
    """Return list-of-dicts where each branch has a `name:` field.

    Accepts `branch:` (new) or `name:` (old) as the slot key — normalizes to `name`.
    """
    out: list[dict] = []
    for b in branches:
        if "branch" in b and "name" not in b:
            nb = {k: v for k, v in b.items() if k != "branch"}
            nb["name"] = b["branch"]
            out.append(nb)
        else:
            out.append(b)
    return out
