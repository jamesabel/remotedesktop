"""Semantic-version compatibility policy for client/server connections.

The apps follow semver (https://semver.org): the MAJOR version is the
client/server compatibility contract — two apps with the same major are
expected to interoperate, different majors may not. MINOR versions add
backward-compatible features, PATCH versions fix bugs.

On every connection each side compares its own major against the peer's
(the hello and welcome messages carry `app_version`) and shows a strong
warning on a mismatch. The connection is still allowed — the user may try —
but nothing about it is guaranteed. Peers too old to report a version
(pre-0.19) can't be compared and produce no warning.
"""

import semver


def major_of(version: str) -> int | None:
    """The semver major of `version`, or None when it isn't valid semver
    (including the empty string an old peer implies)."""
    try:
        return semver.Version.parse(version).major
    except (ValueError, TypeError):
        return None


def mismatch_warning(mine: str, theirs: str, peer: str) -> str | None:
    """A strong user-facing warning when the major versions differ, else
    None. `peer` names the other side ("client" or "server")."""
    ours, others = major_of(mine), major_of(theirs)
    if ours is None or others is None or ours == others:
        return None
    return (
        f"WARNING: version mismatch — this app is {mine} but the {peer} is "
        f"{theirs}. Different major versions are not guaranteed to work "
        f"together: you can still connect, but features may fail or behave "
        f"incorrectly. Update both computers to the same version."
    )
