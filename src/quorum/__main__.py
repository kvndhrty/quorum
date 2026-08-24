"""`python -m quorum` — the same CLI as the `quorum` script.

Exists so the detached task runner (and tests) can re-invoke quorum through
the current interpreter without depending on the console script being on
PATH.
"""

from .cli import app

app()
