"""Workflow-shape invariants for the mechanical gate.

These are properties of the YAML that no unit test on grok_approve_gate.py can
see, and each one corresponds to a defect that minted a false success.
"""

from __future__ import annotations

import re
import shlex
import subprocess
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
    # "Claude Code Review" used to be listed here and never belonged: it
    # produced no required context (live protection requires Forbidden Files,
    # Free public cloud smoke and grok-mechanical-approve), and the workflow
    # was removed in #240 for reporting success without ever reviewing.
    assert {"Public CI", "PR Hygiene"} <= listed
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
    ":", "[", "[[", "]]", "break", "case", "cd", "continue", "declare",
    "done", "echo", "esac", "exit", "export", "false", "fi", "for",
    "function", "getopts", "in", "let", "local", "printf", "pwd", "read",
    "readonly", "return", "select", "set", "shift", "test", "trap", "true",
    "type", "typeset", "umask", "unset", "wait", "{", "}",
})

# Words FOLLOWED BY a command. The word itself is safe, but the next word is
# still a PATH lookup — `exec curl`, `time curl`, `if curl`, `env curl` all
# run curl off PATH. Treating these as terminal is how an unpinned command
# hides in plain sight, so the scan keeps looking past them.
_COMMAND_PREFIXES = frozenset({
    "!", "if", "then", "elif", "else", "while", "until", "do",
    "command", "coproc", "env", "exec", "nohup", "sudo", "time", "xargs",
})

# Constructs that defeat any static reading of the script. In a trust step
# they are themselves the finding, not something to see through.
_OPAQUE = frozenset({"eval", "source", "."})

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=")
# Case labels are replaced BY A SEPARATOR, never deleted: removing the `)`
# without leaving a command boundary meant the arm body on the same line was
# read as arguments to the previous command, hiding it entirely.
_CASE_LABEL = re.compile(r"^(\s*)\(?[^\s()]*\)(\s|$)")
_CASE_LABEL_AFTER_IN = re.compile(r"(\bin\s+)\(?[^\s()]*\)")
_CASE_LABEL_AFTER_SEP = re.compile(r"(;;&?\s*)\(?[^\s()]*\)")
_FUNC_DEF = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)", re.M)
# `$NAME` and `${NAME}` expansions. Whatever LITERAL text remains beside them
# in a command name is still a PATH lookup: with Z unset, `${Z}cat` runs cat.
_EXPANSION = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\$[0-9?@*#$!-]")
_INTERPRETER_C = re.compile(
    r"(?:^|[\s;|&(])(?:\S*/)?(bash|sh|zsh|dash|python3?|perl|ruby|node)\s+"
    r"(?:-\w+\s+)*-c\b"
)
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
            line = _CASE_LABEL.sub(r"\1;\2", line)
            line = _CASE_LABEL_AFTER_IN.sub(r"\1;", line)
            line = _CASE_LABEL_AFTER_SEP.sub(r"\1;", line)
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
    after_prefix = False           # last word was exec/sudo/env/...
    prev = "\n"
    i, n = 0, len(script)

    def flush() -> None:
        nonlocal buf, pending, after_prefix
        if buf:
            word = "".join(buf)
            buf = []
            # A run of assignments may precede the command: `A=1 B=2 cmd`.
            # Prefix words work the same way. Either keeps `pending` armed.
            if _ASSIGNMENT.match(word):
                return
            base = word.rsplit("/", 1)[-1]
            if base in _COMMAND_PREFIXES:
                after_prefix = True
                return
            # `sudo -E curl`, `command -p curl`, `time -p curl`: the option
            # belongs to the prefix, and the real PATH lookup is still ahead.
            # Options are filtered out downstream, so without this the scan
            # reported nothing at all for those forms.
            if after_prefix and word.startswith("-"):
                return
            after_prefix = False
            heads.append(word)
            pending = False

    while i < n:
        char = script[i]

        if mode == "normal" and char == "#" and prev in " \t\n;|&(":
            while i < n and script[i] != "\n":
                i += 1
            continue
        prev = char

        if mode == "single":
            if char == "'":
                mode = "normal"
                if pending:
                    buf.append("\x00")   # quote boundary, see _EXPANSION use
            elif pending:
                buf.append(char)       # a quoted command name still runs
            i += 1
            continue

        if char == "\\":
            i += 2
            continue

        # $(( arithmetic )) evaluates; it is not a command context. Checked
        # before $( or every `$(( i + 1 ))` reads as a call to `i`.
        if char == "$" and script[i + 1:i + 3] == "((":
            close = script.find("))", i + 3)
            i = (close + 2) if close != -1 else n
            continue

        # $(...) and `...` open a fresh command context, even inside quotes.
        # In a COMMAND-NAME position the substitution's OUTPUT is the command,
        # so `$(echo cat)` runs cat while the inner `echo` reads as a harmless
        # builtin. Record the position itself; only the inner scan continues.
        if char == "$" and script[i + 1:i + 2] == "(":
            if pending and not buf:
                heads.append("$(...)")
            flush()
            stack.append(mode)
            mode, pending = "normal", True
            i += 2
            continue
        if char == "`":
            if pending and not buf:
                heads.append("`...`")
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
                if pending:
                    buf.append("\x00")
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

        # NAME=(one two) is an array literal, not a call to its first element.
        if char == "(" and buf and _ASSIGNMENT.match("".join(buf)):
            close = script.find(")", i + 1)
            i = (close + 1) if close != -1 else n
            buf = []
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
            start = i
            while i < n and (script[i] in "<>&" or script[i].isdigit()):
                i += 1
            # `2>&1` / `>&2` already name their destination. Consuming a
            # filename after them ate the next word, and that word was the
            # command: `2>&1 cat > ...` reported nothing at all.
            if "&" not in script[start:i]:
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
    functions = set(_FUNC_DEF.findall(script))
    bare = []
    # `bash -c '...'` and `python3 -c '...'` carry a whole program in a quoted
    # argument that this parser deliberately does not enter. Pinning the
    # interpreter says nothing about what the string inside resolves on PATH,
    # so the construct itself is the finding in a trust step.
    for match in _INTERPRETER_C.finditer(_strip_heredocs(script)):
        bare.append(f"{match.group(1)} -c")
    for head in _command_heads(script):
        if not head:
            continue
        if head in _OPAQUE:
            bare.append(head)          # unreadable by any static check
            continue
        # What survives after removing variable expansions and quote marks is
        # what bash actually looks up. `${Z}cat` with Z unset runs plain `cat`,
        # so treating any head containing `$` as pinned was itself a bypass.
        literal = _EXPANSION.sub("", head).replace("\x00", "")
        if literal != head.replace("\x00", "") and not literal.startswith("/"):
            # The head was assembled from an expansion. With the variable
            # unset bash drops it and resolves whatever literal text is left —
            # or, if nothing is left, the NEXT word — off PATH. Only an
            # expansion that resolves to an absolute path (`$HOME/.grok/bin/
            # grok`) is safe, so everything else is reported rather than
            # assumed pinned. Failing closed here is the point: three review
            # rounds found constructs this parser read wrongly, and every one
            # of them previously came out as "no findings".
            bare.append(head or "<empty>")
            continue
        if literal in _SHELL_BUILTINS or literal in functions:
            continue
        if literal.startswith(("/", "-", "~")):
            continue
        if _ASSIGNMENT.match(literal):
            continue
        bare.append(literal)
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


@pytest.mark.parametrize(
    "script, expected",
    [
        # Round 2. With Z unset these run plain `cat` off PATH; treating any
        # head containing `$` as already-pinned was itself the bypass.
        # Reported as the whole construct: it is the assembly from an
        # expansion that is the finding, not just the literal tail.
        ("${Z}cat /tmp/grok-reply.md", ["${Z}cat"]),
        ('"$Z"cat /tmp/grok-reply.md', ["$Z\x00cat"]),
        # An option belongs to the prefix; the PATH lookup is still ahead.
        ("sudo -E curl -s https://example.invalid", ["curl"]),
        ("command -p curl -s https://example.invalid", ["curl"]),
        ("time -p curl -s https://example.invalid", ["curl"]),
        # A single-line case arm: stripping the label used to erase the
        # boundary too, so the arm body read as arguments to the previous word.
        ('case "$E" in APPROVE) curl -s https://example.invalid ;; esac', ["curl"]),
        ('case "$E" in A) : ;;& B) curl -s https://x ;; esac', ["curl"]),
    ],
)
def test_parser_sees_commands_hidden_by_expansion_options_or_case_arms(script, expected):
    assert _unpinned_commands(script) == expected


@pytest.mark.parametrize(
    "script",
    [
        # Arithmetic EXPANSION is not a command context.
        "N=$(( 1 + 2 ))",
        "/usr/bin/printf '%s' $((1+1))",
        "i=$((i+1))",
        # An array literal is not a call to its first element.
        "ARGS=(one two three)",
        # A function defined here is not a PATH lookup when it is called.
        "post() { /usr/bin/gh api \"$1\"; }\npost /repos/x/y",
        # A label with nothing after it on the line.
        'case "$E" in\n  APPROVE)\n    /usr/bin/jq . ;;\nesac',
        "select x in a b; do /usr/bin/echo \"$x\"; done",
    ],
)
def test_parser_tolerates_more_legitimate_shell(script):
    assert _unpinned_commands(script) == []


@pytest.mark.parametrize(
    "script",
    [
        # Round 3. Every one of these runs a PATH-resolved command in real
        # bash while the round-2 parser reported nothing at all. They assert
        # only that SOMETHING is reported: the point is that the check fails
        # closed on syntax it cannot read confidently, not that it produces a
        # particular string.
        "2>&1 cat > /tmp/sandbox.toml",              # operator ate the command
        ">/tmp/out 2>&1 cat /tmp/reply.md",
        "$X cat /tmp/reply.md",                      # empty expansion head
        "${EMPTY} gh pr comment 1",
        "$'cat' /tmp/reply.md",                      # ANSI-C quoting
        "$(echo cat) /tmp/reply.md",                 # substitution as the head
        "/bin/bash -c 'cat /tmp/reply.md'",          # program inside a string
        "/usr/bin/python3 -c 'import os; os.system(\"cat /tmp/x\")'",
        "X=$(/usr/bin/date)#; cat /tmp/reply.md",    # `)#` is not a comment
    ],
)
def test_parser_fails_closed_on_syntax_it_cannot_read(script):
    assert _unpinned_commands(script), (
        "construct produced no finding; a parser that reads this wrongly must "
        "report, because silence here reads as 'nothing unpinned'"
    )


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


_COMMENT_LINE = re.compile(r"(?m)^[ \t]*#.*$")
_TRAILING_COMMENT = re.compile(r"(?m)(?<=[ \t])#.*$")
# Ways a step can put a directory on PATH for LATER steps. Keyed on the
# mechanism, not on one variable name: `echo "PATH=$HOME/x:$PATH" >>
# $GITHUB_ENV` prepends identically and contains no "GITHUB_PATH" at all.
_EXPORT_PATH = re.compile(r"(?m)^\s*export\s+PATH=")
_PATH_MUTATORS = (
    re.compile(r"GITHUB_PATH"),
    re.compile(r"PATH=.*GITHUB_ENV", re.S),
    _EXPORT_PATH,
)


def _uncommented(run: str) -> str:
    """`run` with shell comments removed.

    The boundary below is a search over step bodies, and a step that merely
    TALKS about GITHUB_PATH in a comment used to satisfy it — which slid the
    boundary later and shrank the enforced set instead of growing it.
    """
    return _TRAILING_COMMENT.sub("", _COMMENT_LINE.sub("", run))


def _steps_after_path_mutation(job: dict) -> list[dict]:
    """Every step that runs after something puts a directory on PATH.

    DERIVED, never enumerated. `Install Grok Build CLI` puts $HOME/.grok/bin on
    PATH straight out of a `curl | bash`, and that takes effect in SUBSEQUENT
    steps — so the exposed set is positional, not a list of names someone
    remembered to update. Enumeration is what let cat/jq/gh sit unpinned beside
    the git/python3/sed an earlier fix did pin, and what left the step writing
    `restrict_network = true` uncovered.

    The EARLIEST mutation wins: taking the first match of a loose substring let
    a later mention move the boundary forward and exempt everything before it.
    """
    steps = job.get("steps") or []
    for i, step in enumerate(steps):
        body = _uncommented(str(step.get("run", "")))
        # GITHUB_PATH / GITHUB_ENV take effect in the NEXT step; an in-body
        # `export PATH=` takes effect immediately, so that step must include
        # ITSELF or it would have exempted its own remaining commands.
        if _EXPORT_PATH.search(body):
            return steps[i:]
        if any(m.search(body) for m in _PATH_MUTATORS):
            return steps[i + 1:]
    return []


def _jobs_with_path_mutation() -> dict[str, dict]:
    """Every job in every workflow that mutates PATH, by "file::job".

    Derived for the same reason the step set is: naming two workflows left
    grok-boundary-test.yml — which runs a deliberately ADVERSARIAL agent and
    publishes its report — resolving base64, chmod and cat off the very PATH
    the `curl | bash` in that job had just prepended to.
    """
    out = {}
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for job_name, job in (_load(path).get("jobs") or {}).items():
            if _steps_after_path_mutation(job):
                out[f"{path.name}::{job_name}"] = job
    return out


@pytest.mark.parametrize(
    "workflow", ["grok-review.yml", "grok.yml", "grok-boundary-test.yml"]
)
def test_grok_invocations_pin_model_and_effort(workflow):
    """Per the operator model ladder, CI must not float on service defaults.

    An unpinned invocation silently re-tiers the moment the service default
    moves, and for grok-boundary-test.yml that would quietly change what the
    containment claim was actually proven against.
    """
    doc = _load(ROOT / ".github" / "workflows" / workflow)
    calls = []
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            # Uncommented step bodies only: a mention in a comment must not
            # read as a pin, and an unquoted invocation must not be skipped.
            body = _uncommented(str(step.get("run", "")))
            for call in re.finditer(r'"?\$HOME/\.grok/bin/grok"?((?:[^\n]*\\\n)*[^\n]*)', body):
                flags = call.group(1)
                # Only AGENT runs carry a model lane. `grok --version`, used by
                # the install step to verify its pin, has no model to pin.
                # Keyed on --version rather than on the presence of
                # --prompt-file: an agent call written another way must still
                # be checked, not silently skipped past the non-vacuity guard.
                if "--version" in flags:
                    continue
                calls.append(flags)
    # Without this the loop below is vacuous whenever the regex matches
    # nothing — a green test that constrains exactly zero invocations.
    assert calls, f"{workflow}: no grok invocation found to check"
    for flags in calls:
        assert "--model grok-4.5" in flags, f"{workflow}: model not pinned"
        assert "--reasoning-effort high" in flags, f"{workflow}: effort not pinned"


@pytest.mark.parametrize(
    "workflow, reply",
    [
        ("grok-review.yml", "/tmp/grok-reply.md"),
        ("grok.yml", "/tmp/grok-reply.md"),
        ("grok-boundary-test.yml", "/tmp/attack-result.md"),
    ],
)
def test_publish_gate_requires_a_parsed_auth_file(workflow, reply):
    """SEC-G12: the step that decides whether a reply is published must not
    accept a gate run that silently skipped two of its four checks. `base64 -d`
    exits 0 on empty input, so an unset GROK_CI_AUTH_JSON reaches that state
    through a restore step that looks like it succeeded."""
    doc = _load(ROOT / ".github" / "workflows" / workflow)
    call = f"/usr/bin/python3 -I - --require-auth {reply}"
    # Uncommented step bodies, not raw file text: a commented-out `# was: ...
    # --require-auth ...` line above a stripped invocation satisfied a plain
    # substring match over the file.
    bodies = [
        _uncommented(str(step.get("run", "")))
        for job in doc["jobs"].values() for step in job.get("steps") or []
    ]
    assert any(call in body for body in bodies), (
        f"{workflow}: publish gate does not pass --require-auth"
    )
    # A step-level condition would disable the gate in CI while every string
    # assertion above still passes.
    gate_steps = [
        step for job in doc["jobs"].values() for step in job.get("steps") or []
        if call in _uncommented(str(step.get("run", "")))
    ]
    for step in gate_steps:
        condition = str(step.get("if", "")).strip()
        assert condition in ("", "steps.resolve.outputs.skip == 'false'"), (
            f"{workflow}: publish gate carries a condition that could disable "
            f"it: {condition!r}"
        )


def _extract_version_check(run: str) -> str:
    """The pin-verification shell from an install step, minus the install.

    Starts at the CAPTURE line, not at the comparison. Slicing from the
    comparison left the capture untested, so inserting
    `INSTALLED="grok $GROK_CLI_VERSION ..."` right after it — a tautology that
    certifies any installed build — passed every assertion here.
    """
    start = run.index('INSTALLED=$("$HOME/.grok/bin/grok" --version)')
    return "set -eu\n" + run[start:]


def _stub_grok_home(tmp_path: Path, version_output: str) -> Path:
    """A $HOME whose .grok/bin/grok prints `version_output`."""
    binary = tmp_path / ".grok" / "bin" / "grok"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(f'#!/bin/sh\nprintf "%s\\n" {shlex.quote(version_output)}\n')
    binary.chmod(0o755)
    return tmp_path


GROK_CLI_WORKFLOWS = ("grok-review.yml", "grok.yml", "grok-boundary-test.yml")


@pytest.mark.parametrize("workflow", GROK_CLI_WORKFLOWS)
def test_grok_cli_install_is_version_pinned(workflow, tmp_path):
    """SEC-G10: an unpinned `curl | bash` installs whatever is current at run
    time, so "the CLI version changed" — the axis grok-boundary-test.yml's own
    header says must re-prove the boundary — could never fire its `paths`
    filter. Pinning makes a CLI change a FILE change, which that filter sees.
    """
    doc = _load(ROOT / ".github" / "workflows" / workflow)
    steps = [s for job in doc["jobs"].values() for s in job.get("steps") or []
             if "x.ai/cli/install.sh" in str(s.get("run", ""))]
    assert steps, f"{workflow}: no CLI install step found to check"
    for step in steps:
        pin = (step.get("env") or {}).get("GROK_CLI_VERSION")
        assert pin, f"{workflow}: install step declares no GROK_CLI_VERSION"
        assert re.fullmatch(r"\d+\.\d+\.\d+", str(pin)), f"{workflow}: {pin!r}"
        run = step["run"]
        assert 'bash -s "$GROK_CLI_VERSION"' in run, (
            f"{workflow}: install does not pass the pin to the installer"
        )
        # BEHAVIOURAL, not a grep: the version check is extracted and executed
        # under bash with synthetic `grok --version` output. An earlier version
        # of this test asserted only that the strings "--version" and
        # "::error::expected grok" appeared, and passed after `exit 1` was
        # removed from the mismatch branch — an annotation is not a failure.
        # The capture line itself is outside the executed region below, so
        # pin it exactly: replacing it with a hardcoded string, or with a
        # PATH-resolved `grok --version`, would otherwise certify any build.
        assert 'INSTALLED=$("$HOME/.grok/bin/grok" --version)' in run, (
            f"{workflow}: version is not captured from the pinned binary"
        )
        check = _extract_version_check(run)
        for installed, should_pass in (
            # No channel tag at all — what a FRESH runner $HOME actually
            # prints, because the [stable]/[alpha] suffix comes from an
            # update-check cache install.sh never writes. Requiring the tag
            # took all three workflows offline; this case pins that lesson.
            (f"grok {pin} (abc1234)", True),
            (f"grok {pin} (abc1234) [stable]", True),
            (f"grok {pin}0 (abc1234)", False),            # X.Y.Z vs X.Y.Z0
            ("grok 9.9.9 (abc1234)", False),
            (f"grok {pin} (abc1234) [alpha]", False),     # wrong channel
        ):
            home = _stub_grok_home(tmp_path / installed.replace(" ", "_").replace("/", "_"),
                                   installed)
            rc = subprocess.run(
                ["bash", "-c", check],
                env={"GROK_CLI_VERSION": str(pin), "HOME": str(home),
                     "PATH": "/usr/bin:/bin"},
                capture_output=True, text=True,
            ).returncode
            assert (rc == 0) is should_pass, (
                f"{workflow}: version check returned {rc} for {installed!r}; "
                f"expected {'accept' if should_pass else 'reject'}"
            )


def test_every_grok_workflow_pins_the_same_cli_version():
    """A boundary proven against one build says nothing about another."""
    pins = set()
    for workflow in GROK_CLI_WORKFLOWS:
        doc = _load(ROOT / ".github" / "workflows" / workflow)
        for job in doc["jobs"].values():
            for step in job.get("steps") or []:
                if "x.ai/cli/install.sh" in str(step.get("run", "")):
                    pins.add((step.get("env") or {}).get("GROK_CLI_VERSION"))
    assert len(pins) == 1, f"grok workflows disagree on the CLI version: {pins}"


def test_boundary_test_has_a_trigger_that_does_not_need_a_diff():
    """The pinned CLI covers repo-visible change; the sandbox's server-side
    behaviour and the pinned artifact's availability can still move with no
    diff at all, and the last green run would stand as current evidence."""
    triggers = _triggers(_load(ROOT / ".github" / "workflows" / "grok-boundary-test.yml"))
    assert "schedule" in triggers, "no trigger can fire without a file change"
    assert triggers["schedule"], "schedule declared but empty"


def test_every_path_mutating_job_is_covered():
    """A guard on the guard: if this set silently shrinks, the pinning test
    below keeps passing while covering less."""
    covered = set(_jobs_with_path_mutation())
    assert {
        "grok-review.yml::grok-review",
        "grok.yml::grok",
        "grok-boundary-test.yml::boundary",
    } <= covered, f"a PATH-mutating job dropped out of scope: {sorted(covered)}"


def test_no_unpinned_command_runs_after_path_is_mutated():
    """#263's stated threat model, applied consistently.

    A shadowed binary after this point is not a tidiness problem: a planted
    `cat` in `Configure the sandbox profile` writes restrict_network = false
    and opens the child-process egress the whole design leans on, which defeats
    the leak gate outright because the credential never has to pass through the
    reply at all.
    """
    for label, job in _jobs_with_path_mutation().items():
        for step in _steps_after_path_mutation(job):
            bare = _unpinned_commands(step.get("run", ""))
            assert not bare, (
                f"{label}::{step.get('name')} resolves {sorted(set(bare))} on "
                "PATH after a directory was prepended; use an absolute path"
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


# --- dual-mode auth pins (#350) ----------------------------------------------
# Grok's round-3 review: the dual-mode controls were load-bearing but unpinned
# — removing the empty-key unset, the post-agent needle rebuild, the shape
# guard, or the key-wins ordering left this suite green. Each invariant below
# is one of those controls, asserted over UNCOMMENTED run bodies so a comment
# mentioning the construct cannot satisfy the pin.

_DUAL_MODE_WORKFLOWS = ["grok-review.yml", "grok.yml", "grok-boundary-test.yml"]


def _steps_of(workflow: str):
    doc = _load(ROOT / ".github" / "workflows" / workflow)
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            yield step


@pytest.mark.parametrize("workflow", _DUAL_MODE_WORKFLOWS)
def test_restore_prefers_api_key_over_oauth(workflow):
    """The paid key is the deliberate choice; OAuth is the fallback branch."""
    restores = [
        _uncommented(str(s.get("run", "")))
        for s in _steps_of(workflow)
        if "Restore Grok auth" in str(s.get("name", ""))
    ]
    assert restores, f"{workflow}: restore step missing"
    for body in restores:
        key_branch = body.find('if [ -n "$XAI_API_KEY" ]')
        oauth_branch = body.find('elif [ -n "$GROK_CI_AUTH_JSON" ]')
        assert key_branch != -1 and oauth_branch != -1, (
            f"{workflow}: restore is not dual-mode"
        )
        assert key_branch < oauth_branch, (
            f"{workflow}: OAuth branch precedes the API key — key-wins ordering lost"
        )


@pytest.mark.parametrize("workflow", _DUAL_MODE_WORKFLOWS)
def test_every_agent_step_unsets_an_empty_api_key(workflow):
    """GitHub exports a missing secret as "" and the CLI then selects ApiKey
    auth and 401s even beside a valid auth.json (measured on 0.2.118/0.2.120).
    """
    for step in _steps_of(workflow):
        body = _uncommented(str(step.get("run", "")))
        if "--prompt-file" not in body:
            continue
        invoke = body.find("--prompt-file")
        guard = body.find('if [ -z "${XAI_API_KEY:-}" ]; then unset XAI_API_KEY; fi')
        assert guard != -1 and guard < invoke, (
            f"{workflow}::{step.get('name')}: agent invocation without the "
            "empty-key unset guard before it"
        )
        trim = body.find("XAI_API_KEY=\"${XAI_API_KEY//$'\\n'/}\"")
        assert trim != -1 and trim < guard, (
            f"{workflow}::{step.get('name')}: no CR/LF trim before the "
            "empty-key guard — the CLI would receive a newline-tainted key"
        )


@pytest.mark.parametrize("workflow", _DUAL_MODE_WORKFLOWS)
def test_publish_gates_rebuild_needles_from_secrets(workflow):
    """After the agent ran, only the secrets are known-live — the on-disk
    auth.json is agent-reachable state. Every gate run against the DEFAULT
    auth path must rebuild it, in both modes: API keys have no shape
    backstop, and OAuth refresh tokens are opaque (not JWT-shaped).
    The positive control is exempt — it feeds an explicit fixture pair.
    """
    found = 0
    for step in _steps_of(workflow):
        body = _uncommented(str(step.get("run", "")))
        if "--require-auth" not in body or "fixture-auth.json" in body:
            continue
        found += 1
        gate_call = body.find("--require-auth")
        key_rebuild = body.find("printf '{\"xai_api_key\": \"%s\"}'")
        oauth_rebuild = body.find('"$GROK_CI_AUTH_JSON" | /usr/bin/base64 -d')
        assert key_rebuild != -1 and key_rebuild < gate_call, (
            f"{workflow}::{step.get('name')}: no API-key needle rebuild before the gate"
        )
        assert oauth_rebuild != -1 and oauth_rebuild < gate_call, (
            f"{workflow}::{step.get('name')}: no OAuth needle rebuild before the gate"
        )
        # ORDERING is load-bearing (round-4 review): restore prefers the key,
        # so the agent only ever held the key — a gate rebuild that prefers
        # OAuth when both secrets exist scans the wrong credential and fails
        # OPEN on the one the agent actually had. Mirror the restore pin.
        key_branch = body.find('if [ -n "${XAI_API_KEY:-}" ]')
        oauth_branch = body.find('elif [ -n "${GROK_CI_AUTH_JSON:-}" ]')
        assert -1 < key_branch < oauth_branch < gate_call, (
            f"{workflow}::{step.get('name')}: gate rebuild does not prefer the "
            "API key before the OAuth blob"
        )
        # And no silent fallthrough: if restore and gate ever diverge, a run
        # with neither secret must refuse rather than certify against
        # agent-reachable on-disk state.
        fallthrough_guard = body.find(
            "No credential secret available to rebuild the needle set"
        )
        assert -1 < fallthrough_guard < gate_call, (
            f"{workflow}::{step.get('name')}: rebuild lacks the fail-closed "
            "else branch before the gate"
        )
        env = step.get("env") or {}
        assert env.get("XAI_API_KEY") == "${{ secrets.XAI_API_KEY }}", (
            f"{workflow}::{step.get('name')}: gate step lacks the key secret"
        )
        assert env.get("GROK_CI_AUTH_JSON") == "${{ secrets.GROK_CI_AUTH_JSON }}", (
            f"{workflow}::{step.get('name')}: gate step lacks the OAuth secret"
        )
    assert found, f"{workflow}: no publish gate step found — pin is vacuous"


@pytest.mark.parametrize("workflow", _DUAL_MODE_WORKFLOWS)
def test_shape_guard_precedes_every_key_json_write(workflow):
    """A key containing a quote or backslash corrupts the printf JSON template,
    so the character-class refusal must run first, at every write site."""
    writes = 0
    for step in _steps_of(workflow):
        body = _uncommented(str(step.get("run", "")))
        start = 0
        while True:
            write = body.find("printf '{\"xai_api_key\": \"%s\"}'", start)
            if write == -1:
                break
            writes += 1
            guard = body.rfind("*[!A-Za-z0-9._-]*", 0, write)
            assert guard != -1, (
                f"{workflow}::{step.get('name')}: xai_api_key JSON write "
                "without a preceding shape guard"
            )
            # Trim must precede the guard: a valid key with a stray trailing
            # newline (echo | gh secret set) must be normalized, not treated
            # as a fatal shape violation that also blocks the OAuth elif.
            trim = body.rfind("XAI_API_KEY=\"${XAI_API_KEY//$'\\n'/}\"", 0, write)
            assert trim != -1, (
                f"{workflow}::{step.get('name')}: xai_api_key write without a "
                "preceding CR/LF trim"
            )
            start = write + 1
    # Round-5 review: without a floor this pin is vacuous the moment every
    # write disappears. Restore + publish gate = at least two per workflow.
    assert writes >= 2, (
        f"{workflow}: expected >=2 shape-guarded key writes, found {writes}"
    )
