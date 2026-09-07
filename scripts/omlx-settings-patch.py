#!/usr/bin/env python3
"""Converge one oMLX ``settings.json`` key before ``omlx-cli serve`` starts.

Why this exists: ``memory.prefill_safe_zone_ratio`` has no ``omlx-cli serve``
flag and no admin-API setter (checked against oMLX 0.6.4: ``cli.py`` exposes
only ``--memory-guard`` / ``--memory-guard-gb``; ``admin/routes.py`` never
touches the key). The only way to set it is the file, so ``omlx-serve.sh``
runs this before ``exec`` so every launchd (re)start converges on the value
the repo declares. ``GlobalSettings.save_cli_overrides`` re-reads the file
before it saves, so an edit made here survives the server's own startup write.

Contract (deliberately narrow):

* Idempotent: exits 0 without touching the file when the value already
  matches.
* Fail-open: any error (missing file, bad JSON, unwritable dir) prints to
  stderr and exits 0. ``com.syntax922.ig88.omlx`` is ``KeepAlive``; a
  non-zero exit here would crash-loop inference over a settings nit.
* Atomic: writes a temp file in the same directory and ``os.replace``s it,
  after copying the original to ``settings.json.bak-<UTC stamp>`` (same
  backup convention the operator already uses in ``~/.omlx``).
* Never prints the file: ``settings.json`` carries ``auth.secret_key``.
  Only the one key and its old/new value are logged.

Usage: ``omlx-settings-patch.py <path> <dotted.key> <value>`` where value
parses as JSON (``0.95``, ``true``, ``"2GB"``). Range-checks nothing beyond
JSON parsing; oMLX validates on load (``prefill_safe_zone_ratio`` must be in
[0.5, 0.99]) and refuses to start on an out-of-range value, which is loud in
``logs/omlx.err.log`` — preferable to silently running the wrong number.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time


def _set_dotted(root: dict, dotted: str, value: object) -> object:
    node = root
    parts = dotted.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    old = node.get(parts[-1])
    node[parts[-1]] = value
    return old


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: omlx-settings-patch.py <settings.json> <dotted.key> <json-value>",
            file=sys.stderr,
        )
        return 0  # fail-open: never block the serve
    path, dotted, raw = argv[1], argv[2], argv[3]
    tag = "omlx-settings-patch"
    try:
        value = json.loads(raw)
    except ValueError as exc:
        print(f"{tag}: value {raw!r} is not JSON ({exc}); leaving {path} alone", file=sys.stderr)
        return 0
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"{tag}: cannot read {path} ({exc}); leaving it alone", file=sys.stderr)
        return 0
    if not isinstance(data, dict):
        print(f"{tag}: {path} top level is not an object; leaving it alone", file=sys.stderr)
        return 0

    old = _set_dotted(data, dotted, value)
    if old == value:
        print(f"{tag}: {dotted} already {value!r}; no write", file=sys.stderr)
        return 0

    try:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        shutil.copy2(path, f"{path}.bak-{stamp}")
        directory = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".json", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"{tag}: write failed ({exc}); {path} unchanged", file=sys.stderr)
        return 0
    print(f"{tag}: {dotted} {old!r} -> {value!r} (backup {path}.bak-{stamp})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
