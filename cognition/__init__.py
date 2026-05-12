"""Phase 3 cognition layer for KiraOS_Plugin.

Borrows the three-tier model NEKO has validated in production:

  Tier 1 — atomic facts (event_logs in our DB)
  Tier 2 — synthesized reflections (the new `reflections` table)
  Tier 3 — long-term persona (the existing user_profiles table)

Submodules:

  - ``evidence``: pure functions implementing the Evidence RFC. Rein and
    disp have independent half-life clocks and are computed at read
    time, never persisted.

  - ``facts``: fact identity (normalized SHA-256 hash) so the same
    "用户喜欢猫" said three times across a week doesn't insert three
    rows — instead it reinforces one.

  - ``reflection`` and ``reconciler`` arrive in Phase 3b. Phase 3a only
    provisions the math + DB layer + signal wiring so the auditor's
    behaviour stays unchanged until the reconciler lights up.
"""

__all__ = ["evidence", "facts"]
