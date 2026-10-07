## Decisions

- **D1. "Explicit" means the caller passed `idle`.** Python compares by
  identity against `DEFAULT_IDLE_POLICY` (moved to `_lifecycle_base`, still
  re-exported by `_sandbox_base`), so `idle=IdlePolicy()` written by the
  caller is explicit. TypeScript treats `idle: undefined` (or absent) as the
  default; `{}` is explicit. `None` / `null` keeps disabling auto-suspend.
- **D2. Kill and no-lifecycle launches drop the default when it does not
  fit** (`300 >= bound`). The platform ends the MicroVM (or `rayd` kills it)
  before the window can elapse, so a policy adds nothing; dropping it
  matches E2B, where a sandbox simply ends at its timeout. Clamping was
  rejected: a window just under the timeout would buy a suspend/resume cycle
  (≈ 140 s of compute) for nothing.
- **D3. Pause launches lower the default to 60 s** (the `idlePolicy`
  minimum, `IDLE_MAX_IDLE_MIN_SECONDS`) when 300 s does not fit under the
  cap. Pause needs the platform idle to suspend without a client; 60 s is
  the closest to E2B's "pause at the timeout" and always fits because the
  cap is at least 120 s.
- **D4. Explicit values keep today's validation and messages.**
- **D5. Pools are unchanged**: a pool needs a policy (`PoolConfig` refuses
  `None`), so its own validation stays.
