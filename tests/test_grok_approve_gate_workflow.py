"""Workflow-shape invariants for the mechanical gate.

These are properties of the YAML that no unit test on grok_approve_gate.py can
see, and each one corresponds to a defect that minted a false success.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GATE_YML = ROOT / ".github" / "workflows" / "grok-approve-gate.yml"
REVIEW_YML = ROOT / ".github" / "workflows" / "grok-review.yml"


def _load(path: Path) -> dict:
    # PyYAML parses the `on:` key as the boolean True.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    return doc.get("on") or doc[True]


@pytest.fixture(scope="module")
def gate() -> dict:
    return _load(GATE_YML)


@pytest.fixture(scope="module")
def review() -> dict:
    return _load(REVIEW_YML)


def test_concurrency_group_is_keyed_on_head_sha_for_every_trigger(gate):
    """Keying pull_request on PR number and check_suite on SHA put concurrent
    runs in different groups, so they never cancelled each other and a stale
    snapshot could publish success over a veto that had already landed."""
    group = gate["concurrency"]["group"]
    assert "pull_request.number" not in group, (
        "PR-number keying reintroduces the split-group race"
    )
    for expr in ("pull_request.head.sha", "workflow_run.head_sha"):
        assert expr in group, f"{expr} missing from concurrency group: {group}"


def test_ci_completion_uses_workflow_run_not_check_suite(gate):
    """check_suite does NOT trigger a workflow when the suite was created by
    GitHub Actions, and every CI suite here is Actions-created. Measured on this
    branch: 40 runs, 0 check_suite. A check_suite trigger is dead code that
    reads like a working re-evaluation path."""
    triggers = _triggers(gate)
    assert "check_suite" not in triggers
    listed = set(triggers["workflow_run"]["workflows"])
    # Every workflow producing a required context must re-trigger the gate.
    assert {"Public CI", "PR Hygiene", "Claude Code Review"} <= listed
    assert "Grok Code Review" in listed


def test_gate_does_not_wake_for_ci_completing_on_main(gate):
    """workflow_run branch filters match the TRIGGERING run's head branch. CI
    completing on main starts a gate run that resolves main's SHA, finds no open
    PR and skips — pure waste, and it got worse when the workflows list was
    widened. PR-branch CI must still fire, since that is the only case that can
    produce a decision."""
    wr = _triggers(gate)["workflow_run"]
    assert wr.get("branches-ignore") == ["main"]
    # A `branches` allowlist would be the wrong shape here: it would silently
    # drop every future PR branch that does not match the pattern.
    assert "branches" not in wr


def test_gate_timeout_covers_the_api_budget(gate):
    """Three observation rounds of ~8 calls at a 15s API timeout is ~360s; at
    timeout-minutes: 5 the job could be killed mid-decision."""
    assert gate["jobs"]["gate"]["timeout-minutes"] >= 10


def test_gate_reevaluates_on_review_submission(gate):
    """Without this the veto is advisory: once a success check run exists for a
    SHA, a later REQUEST_CHANGES fires nothing and the green stays visible."""
    types = _triggers(gate)["pull_request_review"]["types"]
    assert "submitted" in types
    assert "dismissed" in types


def test_review_trigger_resolves_a_head_sha(gate):
    """Adding the trigger without widening the resolver's case statement makes
    the job start and then skip: no SHA, no re-evaluation, veto still advisory."""
    steps = gate["jobs"]["gate"]["steps"]
    resolver = next(s for s in steps if s.get("id") == "meta")
    assert "pull_request|pull_request_review)" in resolver["run"]


def test_check_run_is_posted_under_the_app_not_github_token(gate):
    """A required context is matched by NAME. Posting with GITHUB_TOKEN puts the
    check under the GitHub Actions integration, which ANY same-repo workflow can
    also post as — so protection cannot tell the real gate from a spoof."""
    steps = gate["jobs"]["gate"]["steps"]
    minter = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/create-github-app-token")
    )
    assert minter["with"]["app-id"] == "${{ vars.GROK_APP_ID }}"

    runner = next(s for s in steps if s.get("name") == "Run mechanical gate")
    assert runner["env"]["APP_TOKEN"] == "${{ steps.app-token.outputs.token }}", (
        "the gate must receive the App token, or it can only post red"
    )


def test_gate_loads_its_script_from_the_default_branch_only(gate):
    """A PR-head fallback here would let any PR ship its own approver."""
    steps = gate["jobs"]["gate"]["steps"]
    checkout = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["ref"] == "${{ steps.meta.outputs.default_branch }}"


def test_review_workflow_defangs_the_marker_before_embedding_the_reply(review):
    """Load-bearing, not belt-and-braces: _has_marker() still accepts a BARE
    marker line, so this sed is the only thing closing that echo case."""
    steps = review["jobs"]["grok-review"]["steps"]
    post = next(s for s in steps if s.get("name") == "Post review")
    run = post["run"]
    defang = run.index("/usr/bin/sed -i 's/grok-mechanical-eligibility")
    stamp = run.index('echo "<!-- grok-mechanical-eligibility: APPROVE -->"')
    assert defang < stamp, "defang must run before the workflow stamps its own marker"


# Words bash resolves itself — builtins and reserved words. None of them go
# through PATH, so a planted binary cannot intercept them.
_SHELL_BUILTINS = frozenset({
    ":", "[", "[[", "]]", "break", "case", "cd", "continue", "done", "echo",
    "esac", "exit", "export", "false", "fi", "for", "function", "getopts",
    "in", "local", "printf", "pwd", "read", "readonly", "return", "set",
    "shift", "test", "trap", "true", "type", "umask", "unset", "wait",
    "{", "}",
})

# Words FOLLOWED BY a command. The word itself is safe, but the next word is
# still a PATH lookup — `exec curl`, `time curl`, `if curl`, `env curl` all
# run curl off PATH. Treating these as terminal is how an unpinned command
# hides in plain sight, so the scan keeps looking past them.
_COMMAND_PREFIXES = frozenset({
    "!", "if", "then", "elif", "else", "while", "until", "do",
    "command", "env", "exec", "nohup", "sudo", "time", "xargs",
})

# Constructs that defeat any static reading of the script. In a trust step
# they are themselves the finding, not something to see through.
_OPAQUE = frozenset({"eval", "source", "."})

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=")
_CASE_LABEL = re.compile(r"^(\s*)\(?[^\s()]*\)(\s)")
_CASE_LABEL_AFTER_IN = re.compile(r"(\bin\s+)\(?[^\s()]*\)")
_CASE_LABEL_AFTER_SEP = re.compile(r"(;;\s*)\(?[^\s()]*\)")
_HEREDOC = re.compile(r"<<-?\s*([\'\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredocs(script: str) -> str:
    """Drop heredoc BODIES, keeping the line that opens them.

    A heredoc body is data, not shell. Parsing it as shell is not merely noisy:
    a single apostrophe in the body flips the scanner into quoted mode for the
    entire rest of the step, which hides every command after it.
    """
    lines, out, i = script.splitlines(), [], 0
    while i < len(lines):
        out.append(lines[i])
        match = _HEREDOC.search(lines[i])
        i += 1
        if match:
            terminator = match.group(2)
            while i < len(lines) and lines[i].strip() != terminator:
                i += 1
            i += 1  # and the terminator line itself
    return "\n".join(out)


def _strip_case_labels(script: str) -> str:
    """`APPROVE)` is a pattern, not a command.

    Labels appear at the start of a line in the multi-line form, but also
    straight after `in` or `;;` when the whole `case` sits on one line.
    """
    out, in_case = [], False
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("case ") or stripped == "case":
            in_case = True
        if in_case:
            line = _CASE_LABEL.sub(r"\1\2", line)
            line = _CASE_LABEL_AFTER_IN.sub(r"\1", line)
            line = _CASE_LABEL_AFTER_SEP.sub(r"\1", line)
        if re.search(r"\besac\b", stripped):
            in_case = False
        out.append(line)
    return "\n".join(out)


def _command_heads(script: str) -> list[str]:
    """Every word bash would resolve as a command in `script`.

    Quote-aware, and it descends into `$(...)` and backticks: a planted PATH
    binary intercepts a command substitution exactly as it intercepts a
    pipeline segment, so both have to be walked.
    """
    script = _strip_case_labels(_strip_heredocs(script))

    heads: list[str] = []
    buf: list[str] = []
    stack: list[str] = []          # quote mode to restore when $(...) closes
    mode = "normal"
    pending = True                 # the next word is a command head
    prev = "\n"
    i, n = 0, len(script)

    def flush() -> None:
        nonlocal buf, pending
        if buf:
            word = "".join(buf)
            buf = []
            # A run of assignments may precede the command: `A=1 B=2 cmd`.
            # Prefix words work the same way. Either keeps `pending` armed.
            if _ASSIGNMENT.match(word):
                return
            base = word.rsplit("/", 1)[-1]
            if base in _COMMAND_PREFIXES:
                return
            heads.append(word)
            pending = False

    while i < n:
        char = script[i]

        if mode == "normal" and char == "#" and prev in " \t\n;|&()":
            while i < n and script[i] != "\n":
                i += 1
            continue
        prev = char

        if mode == "single":
            if char == "'":
                mode = "normal"
            elif pending:
                buf.append(char)       # a quoted command name still runs
            i += 1
            continue

        if char == "\\":
            i += 2
            continue

        # $(...) and `...` open a fresh command context, even inside quotes.
        if char == "$" and script[i + 1:i + 2] == "(":
            flush()
            stack.append(mode)
            mode, pending = "normal", True
            i += 2
            continue
        if char == "`":
            flush()
            stack.append(mode)
            mode, pending = "normal", True
            i += 1
            continue
        # ${VAR} / ${VAR#glob} is an expansion, not a command group.
        if char == "$" and script[i + 1:i + 2] == "{":
            depth, j = 0, i + 1
            while j < n:
                if script[j] == "{":
                    depth += 1
                elif script[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if pending:
                buf.append(script[i:j + 1])
            i = j + 1
            continue

        if mode == "double":
            if char == '"':
                mode = "normal"
            elif pending:
                buf.append(char)
            i += 1
            continue

        if char in "'\"":
            mode = "single" if char == "'" else "double"
            i += 1
            continue

        # (( arithmetic )) evaluates, it does not run a command.
        if char == "(" and script[i + 1:i + 2] == "(":
            close = script.find("))", i + 2)
            i = (close + 2) if close != -1 else n
            flush()
            pending = False
            continue

        # `name()` is a function definition, not an invocation of `name`.
        if char == "(" and buf and script[i + 1:i + 2] == ")":
            buf = []
            pending = True
            i += 2
            continue

        if char == ")" and stack:
            flush()
            mode = stack.pop()
            pending = False
            i += 1
            continue

        if char in "|&;\n(){}":
            flush()
            pending = True
            i += 1
            continue

        # Redirections: consume the operator AND its target, and do not let
        # either be mistaken for a command. `> /tmp/out curl` still runs curl.
        if char in "<>" or (char.isdigit() and script[i + 1:i + 2] in ("<", ">")):
            if buf and not buf[-1].isdigit():
                flush()
            else:
                buf = []
            while i < n and (script[i] in "<>&" or script[i].isdigit()):
                i += 1
            while i < n and script[i] in " \t":
                i += 1
            while i < n and script[i] not in " \t\n;|&<>()":
                i += 1
            continue

        if char in " \t":
            flush()
            i += 1
            continue

        if pending:
            buf.append(char)
        i += 1

    flush()
    return heads


def _unpinned_commands(script: str) -> list[str]:
    """Command heads bash would look up on PATH rather than run by path."""
    bare = []
    for head in _command_heads(script):
        if not head:
            continue
        if head in _OPAQUE:
            bare.append(head)          # unreadable by any static check
            continue
        if head in _SHELL_BUILTINS:
            continue
        if head.startswith(("/", "$", "-", "\"", "'", "~")):
            continue
        if _ASSIGNMENT.match(head):
            continue
        bare.append(head)
    return bare


# --- the parser behind the pinning check ------------------------------------
# This parser is load-bearing: if it mis-reads a construct, the pinning test
# passes while a PATH-resolved command sits in a trust step. Every HIDES case
# below was a working bypass found by an adversarial review of the first
# version, and every FALSE-POSITIVE case is a legitimate construct that must
# stay usable — a tripwire that blocks correct edits is a tripwire that gets
# deleted.


@pytest.mark.parametrize(
    "script, expected",
    [
        # bash runs `cmd` after any run of assignments
        ('GH_TOKEN="$APP_TOKEN" gh api repos/x/y', ["gh"]),
        ("A=1 B=2 curl -s https://example.invalid", ["curl"]),
        # quoting the command name changes nothing about the PATH lookup
        ("'gh' pr comment 1 --body-file /tmp/x", ["gh"]),
        ('"gh" pr comment 1', ["gh"]),
        # words that take a command as their argument
        ("exec curl -s https://example.invalid", ["curl"]),
        ("time curl -s https://example.invalid", ["curl"]),
        ("sudo curl -s https://example.invalid", ["curl"]),
        ("/usr/bin/env gh api x", ["gh"]),
        ("if gh api x; then /usr/bin/echo hi; fi", ["gh"]),
        # leading redirection does not consume the command
        ("> /tmp/out curl -s https://example.invalid", ["curl"]),
        # command substitution, including backticks
        ("LIVE=`curl -s https://example.invalid`", ["curl"]),
        ('LIVE="$(gh api repos/x/y --jq .head.sha)"', ["gh"]),
        # unreadable by any static check, so the construct is itself a finding
        ('eval "$SOMETHING"', ["eval"]),
    ],
)
def test_parser_sees_commands_that_hide_from_a_naive_scan(script, expected):
    assert _unpinned_commands(script) == expected


def test_parser_sees_past_a_heredoc_body_containing_an_apostrophe():
    """An unbalanced quote inside heredoc DATA used to flip the scanner into
    quoted mode for the rest of the step, hiding every later command."""
    script = (
        "/usr/bin/cat <<'EOF' > /tmp/body.md\n"
        "don't parse this line as shell\n"
        "EOF\n"
        'gh pr comment "$NUM" --body-file /tmp/body.md\n'
    )
    assert _unpinned_commands(script) == ["gh"]


@pytest.mark.parametrize(
    "script",
    [
        "/usr/bin/mkdir -p ${HOME}/.grok",
        'post() { /usr/bin/gh api "$1"; }',
        "if (( n > 1 )); then /usr/bin/cat /tmp/f; fi",
        "2>/dev/null /usr/bin/gh api x",
        "/usr/bin/gh api x > /tmp/out 2>&1",
        'case "$E" in APPROVE) /usr/bin/jq . ;; *) : ;; esac',
        "printf '%s' \"$BYTES\" | /usr/bin/python3 -I - /tmp/reply.md",
        "NOTE=\"${NOTE_LINE#*$'\\t'}\"",
        "/usr/bin/cat <<'EOF' > \"$HOME/.grok/sandbox.toml\"\n"
        "[profiles.ci]\nextends = \"read-only\"\nEOF",
    ],
)
def test_parser_does_not_flag_legitimate_pinned_constructs(script):
    assert _unpinned_commands(script) == []


def test_no_workflow_persists_the_checkout_credential():
    """actions/checkout defaults to writing the installation token into
    .git/config as an http extraheader. Several of these jobs then hand that
    tree to an agent whose reply is published, and grok-boundary-test.yml hands
    it to a deliberately ADVERSARIAL one. Asserted across every workflow rather
    than the two that prompted the fix: the gap was never that a specific file
    was wrong, it was that the default is unsafe here and nothing checked."""
    offenders = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        doc = _load(path)
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if not str(step.get("uses", "")).startswith("actions/checkout"):
                    continue
                if (step.get("with") or {}).get("persist-credentials") is not False:
                    offenders.append(f"{path.name}::{job_name}")
    assert not offenders, (
        f"checkout persists the token in {offenders}; "
        "set persist-credentials: false"
    )


def _steps_after_path_mutation(job: dict) -> list[dict]:
    """Every step that runs after something prepends to PATH via GITHUB_PATH.

    DERIVED, never enumerated. `Install Grok Build CLI` puts $HOME/.grok/bin on
    PATH straight out of a `curl | bash`, and GITHUB_PATH takes effect in
    SUBSEQUENT steps — so the exposed set is positional, not a list of names
    someone remembered to update. Enumeration is exactly what let cat/jq/gh sit
    unpinned beside the git/python3/sed an earlier fix did pin, and what left
    the step that writes `restrict_network = true` uncovered.
    """
    steps = job["steps"]
    for i, step in enumerate(steps):
        if "GITHUB_PATH" in str(step.get("run", "")):
            return steps[i + 1:]
    raise AssertionError("no step appends to GITHUB_PATH — boundary undefined")


def test_no_unpinned_command_runs_after_path_is_mutated(review):
    """#263's stated threat model, applied consistently.

    A shadowed binary after this point is not a tidiness problem: a planted
    `cat` in `Configure the sandbox profile` writes restrict_network = false
    and opens the child-process egress the whole design leans on, which defeats
    the leak gate outright because the credential never has to pass through the
    reply at all.
    """
    jobs = {
        "grok-review.yml": review["jobs"]["grok-review"],
        "grok.yml": _load(ROOT / ".github" / "workflows" / "grok.yml")["jobs"]["grok"],
    }
    for name, job in jobs.items():
        for step in _steps_after_path_mutation(job):
            bare = _unpinned_commands(step.get("run", ""))
            assert not bare, (
                f"{name}::{step.get('name')} resolves {sorted(set(bare))} on "
                "PATH after $HOME/.grok/bin was prepended; use an absolute path"
            )


def test_leak_gate_steps_pin_path_binaries_and_pipe_isolated(review):
    """#252: after GITHUB_PATH prepends $HOME/.grok/bin, gate/parser steps must
    not resolve git/python3 via PATH, and the leak gate must not re-open a
    /tmp file after hashing (pipe + python3 -I)."""
    grok_yml = (ROOT / ".github" / "workflows" / "grok.yml").read_text(encoding="utf-8")
    review_yml = REVIEW_YML.read_text(encoding="utf-8")
    for name, text in (("grok.yml", grok_yml), ("grok-review.yml", review_yml)):
        assert "/usr/bin/git fetch" in text, f"{name}: git fetch must be path-pinned"
        assert "/usr/bin/git show" in text, f"{name}: git show must be path-pinned"
        assert "/usr/bin/python3 -I -" in text, (
            f"{name}: leak gate must run as isolated stdin script"
        )
        # Unpinned forms that #246 closed on boundary-test and #252 extends here.
        assert "python3 /tmp/leak-gate.py" not in text, (
            f"{name}: must not exec a /tmp gate file via unpinned python3"
        )
    # Parser path in grok-review: pinned interpreter, no PATH `cut`/`sed`,
    # default parser via pipe (no /tmp re-open after git show).
    post = next(s for s in review["jobs"]["grok-review"]["steps"] if s.get("name") == "Post review")
    run = post["run"]
    assert "/usr/bin/python3 -I" in run
    assert "| cut " not in run and " | cut" not in run
    assert "/usr/bin/sed -i" in run, "defang sed must be path-pinned"
    # Bare `sed` (not /usr/bin/sed) must not appear as a command — PATH plant.
    assert not any(
        line.lstrip().startswith("sed ") for line in run.splitlines()
    ), "Post review must not resolve bare sed on PATH"
    assert "PARSER_BYTES=$(/usr/bin/git show" in run or 'PARSER_BYTES=$(/usr/bin/git show' in run
    assert "printf '%s' \"$PARSER_BYTES\" | /usr/bin/python3 -I -" in run
    assert "/tmp/parse_grok_verdict.py" not in run, (
        "default parser must not re-open trusted bytes from /tmp"
    )


def test_app_tokens_are_minted_least_privilege(gate, review):
    """create-github-app-token with no permission-* input mints a token
    carrying EVERY installation permission — including checks:write for the
    reviewer, which is the trust root this whole design rests on."""
    gate_step = next(
        s for s in gate["jobs"]["gate"]["steps"]
        if str(s.get("uses", "")).startswith("actions/create-github-app-token")
    )
    assert gate_step["with"].get("permission-checks") == "write"
    assert gate_step["with"].get("permission-administration") == "read"
    # PR write is now required — the gate also submits the mechanical APPROVE
    # and dismisses its own stale ones. Everything else stays unrequested.
    assert set(gate_step["with"]) <= {
        "app-id", "private-key", "owner", "repositories",
        "permission-checks", "permission-administration",
        "permission-pull-requests",
    }, "the gate token must not quietly grow new scopes"

    review_step = next(
        s for s in review["jobs"]["grok-review"]["steps"]
        if str(s.get("uses", "")).startswith("actions/create-github-app-token")
    )
    assert review_step["with"].get("permission-pull-requests") == "write"
    assert "permission-checks" not in review_step["with"], (
        "the reviewer must not be able to post the mechanical check"
    )


# --- /grok-review comment re-trigger ---------------------------------------
# Eligibility is bound to the head SHA, so after agents push fixes the previous
# stamp is worthless. This command is how a review gets re-earned without
# close/reopen or dismissing a review that was never superseded.


def _resolve_step(review: dict) -> dict:
    return next(
        s for s in review["jobs"]["grok-review"]["steps"] if s.get("id") == "resolve"
    )


def test_comment_command_is_a_trigger(review):
    assert _triggers(review)["issue_comment"]["types"] == ["created"]


def test_command_is_restricted_to_collaborators(review):
    """This is a public repo and each review burns metered subscription time,
    so a drive-by commenter must not be able to spend it."""
    cond = review["jobs"]["grok-review"]["if"]
    assert "author_association" in cond
    for role in ("OWNER", "MEMBER", "COLLABORATOR"):
        assert role in cond
    assert "github.event.issue.pull_request" in cond, "must be a PR comment"


def test_command_match_is_exact_not_substring(review):
    """Prose merely mentioning the command must not spend a metered review."""
    run = _resolve_step(review)["run"]
    assert '"$BODY" != "/grok-review"' in run, "needs an exact trimmed comparison"


def test_review_runs_against_the_current_head_sha(review):
    """issue_comment carries no PR head. Reviewing a stale SHA would stamp
    eligibility the gate then rejects for being bound to the wrong commit."""
    assert ".head.sha" in _resolve_step(review)["run"]
    checkout = next(
        s for s in review["jobs"]["grok-review"]["steps"]
        if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["ref"] == "${{ steps.resolve.outputs.sha }}"


def test_repeat_command_is_deduped_by_head_sha(review):
    """Without this a repeated command double-bills for an identical diff."""
    run = _resolve_step(review)["run"]
    assert 'infektydgrokreviewer[bot]' in run
    assert "select(.commit_id==$sha)" in run
    # Piped to jq: `gh api --jq` rejects --arg, which would leave the result
    # empty and silently disable the dedup.
    assert "| jq --arg sha" in run
    # An unparseable result must skip, not proceed.
    assert "''|*[!0-9]*)" in run


def test_draft_and_fork_prs_are_skipped(review):
    run = _resolve_step(review)["run"]
    assert "is a draft" in run
    assert "fork PRs are out of scope" in run


def test_the_command_is_acknowledged(review):
    """A command that silently does nothing is indistinguishable from a broken
    workflow — the requester must learn it was accepted or skipped."""
    ack = next(
        s for s in review["jobs"]["grok-review"]["steps"]
        if s.get("name") == "Acknowledge the command"
    )
    run = ack["run"]
    # Accepted -> a reaction on the command comment.
    assert "/reactions" in run and "content=eyes" in run
    # Rejected -> an explicit reply carrying the reason.
    assert "skipped" in run and "steps.resolve.outputs.reason" in run
    # Must report either way, including when Resolve itself failed.
    assert ack["if"].startswith("always()")


def test_app_token_mint_is_gated_on_the_resolver(review):
    """Otherwise a skipped command still mints an installation token."""
    step = next(
        s for s in review["jobs"]["grok-review"]["steps"]
        if str(s.get("uses", "")).startswith("actions/create-github-app-token")
    )
    assert "steps.resolve.outputs.skip" in step["if"]
    assert "vars.GROK_APP_ID" in step["if"]


def test_metered_steps_never_run_on_a_skip(review):
    """The whole point of the guards is that a rejected command costs nothing."""
    steps = review["jobs"]["grok-review"]["steps"]
    for name in ("Run Grok review", "Post review", "Build review prompt"):
        step = next(s for s in steps if s.get("name") == name)
        assert step["if"] == "steps.resolve.outputs.skip == 'false'", name


def _code(run: str) -> str:
    """Shell body with comment lines stripped.

    Absence assertions must not be satisfied or defeated by prose: several of
    these comments legitimately name the very construct being banned.
    """
    return "\n".join(
        ln for ln in run.splitlines() if not ln.lstrip().startswith("#")
    )


def _step(review: dict, name: str) -> dict:
    return next(
        s for s in review["jobs"]["grok-review"]["steps"] if s.get("name") == name
    )


def test_review_is_submitted_bound_to_the_reviewed_sha(review):
    """`gh pr review` has no commit flag and the REST default is "most recent
    commit AT SUBMISSION TIME", so a push landing during the review would bind
    the review — and its eligibility marker — to code the model never saw.
    Pinning the CHECKOUT is not enough; this pins the review RECORD."""
    run = _code(_step(review, "Post review")["run"])
    assert "gh pr review" not in run, "gh pr review cannot set commit_id"
    assert "commit_id: $c" in run
    assert "/reviews" in run and "gh api -X POST" in run


def test_submission_aborts_if_head_moved_during_the_review(review):
    """Belt to the commit_id brace: a review bound to a stale SHA is useless,
    so say so visibly rather than posting one nobody can act on."""
    run = _step(review, "Post review")["run"]
    assert '"$LIVE" != "$SHA"' in run
    assert "refusing to submit" in run


def test_resolve_step_does_not_reference_its_own_outputs(review):
    """The steps context only holds COMPLETED steps. Referencing
    steps.resolve.outputs.* from inside `resolve` expands empty, which under
    `set -euo pipefail` kills the step and silently disables auto-review."""
    resolve = _code(_resolve_step(review)["run"])
    assert "steps.resolve.outputs" not in resolve, (
        "resolve cannot read its own outputs"
    )
    assert "github.event.pull_request.number" in resolve, (
        "the pull_request path must take the PR number from the payload"
    )


def test_dedup_counts_dismissed_reviews_too(review):
    """Agents can dismiss reviews. Keying dedup on non-dismissed ones would let
    dismiss + /grok-review re-roll the model on identical code until it agrees."""
    run = _code(_resolve_step(review)["run"])
    assert 'select(.state!="DISMISSED")' not in run
    assert "select(.commit_id==$sha)" in run


def test_gate_app_token_no_longer_carries_pr_write(gate, review):
    """The mechanical APPROVE moved to the relay USER token, because an App's
    approval does not satisfy required_approving_review_count (measured on
    #243). So the App token drops PR write entirely: it posts the check and
    reads protection, nothing else. The reviewer keeps PR write and still must
    not gain checks:write — separate capabilities, separate tokens."""
    gate_step = next(
        s for s in gate["jobs"]["gate"]["steps"]
        if str(s.get("uses", "")).startswith("actions/create-github-app-token")
    )
    assert gate_step["with"].get("permission-checks") == "write"
    assert gate_step["with"].get("permission-administration") == "read"
    assert "permission-pull-requests" not in gate_step["with"]

    review_step = next(
        s for s in review["jobs"]["grok-review"]["steps"]
        if str(s.get("uses", "")).startswith("actions/create-github-app-token")
    )
    assert review_step["with"].get("permission-pull-requests") == "write"
    assert "permission-checks" not in review_step["with"]


def test_relay_approve_token_is_passed_to_the_gate(gate):
    """A user with write access is the only identity whose approval counts."""
    runner = next(
        s for s in gate["jobs"]["gate"]["steps"]
        if s.get("name") == "Run mechanical gate"
    )
    assert runner["env"]["RELAY_APPROVE_TOKEN"] == "${{ secrets.RELAY_APPROVE_TOKEN }}"
    # It must be a distinct secret, not reused from the App token.
    assert runner["env"]["RELAY_APPROVE_TOKEN"] != runner["env"]["APP_TOKEN"]


def test_workflow_github_token_stays_read_only_on_prs(gate):
    """The mechanical approval must come from the App, never GITHUB_TOKEN."""
    assert gate["permissions"]["pull-requests"] == "read"
