import json
from typing import get_args

import pytest
from ringleader.analysis import taxonomy
from ringleader.analysis.facts import ErrorCategory
from ringleader.analysis.signals import NON_AGENT_FAILURE_CATEGORIES
from ringleader.analysis.taxonomy import (
    benign_exit,
    cap_error,
    categorize_error,
    command_class,
    error_line,
    error_signature,
    exit_code,
    input_hash,
    is_code_file,
    mcp_parts,
    normalize,
    patch_path,
    preview,
    rule_message_kind,
    tool_kind,
)

CATEGORY_SAMPLES = {
    "interruption": "[Request interrupted by user for tool use]",
    "user_rejected": "The user doesn't want to proceed with this tool use. The tool use was rejected",
    "permission_denied": "Permission for this action was denied by the Claude Code auto mode classifier",
    "hook_blocked": "PreToolUse:Bash hook error: [~/.claude/hooks/x.sh]: BLOCKED",
    "harness_blocked": "Error: Blocked: sleep 60 followed by: tail -5 out.log. To wait, use Monitor",
    "sibling_cancelled": "<tool_use_error>Cancelled: parallel tool call Bash(ls) errored",
    "subagent_error": "Error: Concurrent subagent limit reached. You can run 8 subagents at once.",
    "timeout": "Command timed out after 2m 0.0s",
    "file_too_large": "File content (25282 tokens) exceeds maximum allowed tokens (25000).",
    "bad_path": "Error: File does not exist. Note: your current working directory is /w.",
    "fetch_failed": "Error: Claude Code is unable to fetch from www.example.com",
    "file_not_read": "<tool_use_error>File has not been read yet. Read it first</tool_use_error>",
    "file_modified_since_read": "File has been modified since read, either by the user or a linter",
    "string_not_found": "<tool_use_error>String to replace not found in file.</tool_use_error>",
    "no_changes": "No changes to make: old_string and new_string are exactly the same.",
    "tool_not_found": "Exit code 127\n(eval):1: command not found: foo",
    "input_validation": 'InputValidationError: [{"expected": "string"}]',
    "syntax_error": '  File "x.py", line 3\nSyntaxError: invalid syntax',
    "runtime_error": "Exit code 1\nTraceback (most recent call last):\n  File \"x\"\nKeyError: 'a'",
    "exit_code": "Exit code 2\nsomething went sideways",
}


def test_category_table_matches_the_samples():
    assert [name for name, _ in taxonomy.ERROR_CATEGORIES] == list(CATEGORY_SAMPLES)


@pytest.mark.parametrize(("category", "sample"), CATEGORY_SAMPLES.items())
def test_each_category_matches_its_sample(category, sample):
    assert categorize_error(sample) == category


def test_traceback_is_a_runtime_error():
    assert categorize_error("Traceback (most recent call last):\n  boom") == "runtime_error"


@pytest.mark.parametrize(
    ("text", "category"),
    [
        (
            "apply_patch verification failed: Failed to find expected lines in a.py",
            "string_not_found",
        ),
        ("apply_patch verification failed: invalid patch: two ops", "input_validation"),
        ("collab spawn failed: agent limit reached", "subagent_error"),
        ("aborted by user after 12.3s", "interruption"),
        (
            "Error: Permission for this tool use was denied. The tool use was rejected",
            "permission_denied",
        ),
        (
            "Permission to use Bash with command cd a &&\ngit push has been denied.",
            "permission_denied",
        ),
        (
            "Error: m is temporarily unavailable (timed out), so auto mode cannot determine the safety of Bash",
            "permission_denied",
        ),
        ("The user doesn't want to take this action right now. STOP", "user_rejected"),
        (
            "Error: This session is isolated in the worktree /w/.claude/worktrees/x",
            "harness_blocked",
        ),
        ("Sibling tool call errored", "sibling_cancelled"),
        (
            "Error: Error running task subagent: The child completion could not be persisted",
            "subagent_error",
        ),
        ("Error: File content (938.6KB) exceeds maximum allowed size (256KB).", "file_too_large"),
        ("Error: EISDIR: illegal operation on a directory, read '/w'", "bad_path"),
        ("Error: HTTP 422: Unprocessable Entity", "fetch_failed"),
        (
            "MCP error -32602: Input validation error: Invalid arguments for tool x",
            "input_validation",
        ),
        ("Error: Command failed (exit code: 101)\n   Compiling x v0.1.0", "exit_code"),
    ],
)
def test_calibrated_error_phrasings(text, category):
    assert categorize_error(text) == category


def test_an_http_status_inside_command_output_is_not_a_fetch_failure():
    assert (
        categorize_error("Exit code 1\n=== probe ===\nfetcher: upstream returned HTTP 403")
        == "exit_code"
    )


def test_plain_output_has_no_category():
    assert categorize_error("all good here") is None


@pytest.mark.parametrize(
    ("command", "cls", "kind"),
    [
        ("cd x && grep foo", "grep", "search"),
        ("FOO=1 uv run pytest -q", "pytest", "validate"),
        ("timeout 30 npx tsc", "tsc", "validate"),
        ("cat a | grep b", "cat", "read"),
        ("python3 -m mypy ringleader", "mypy", "validate"),
        ("export X=1; just check", "just", "validate"),
        ("/usr/bin/rg needle .", "rg", "search"),
        ("git status", "git", "shell"),
        ("echo 'a | b' && ls", "echo", "shell"),
        ("(cd sub && pytest)", "pytest", "validate"),
        ("for f in *.py; do ruff check $f; done", "ruff", "validate"),
        ("X=$(git rev-parse HEAD) pytest", "pytest", "validate"),
        ("{ cd a; just check; }", "just", "validate"),
        ("! grep -q x f", "grep", "search"),
        ("sudo -n pytest", "pytest", "validate"),
        ("uv run --with rich pytest", "pytest", "validate"),
        ("cat a 2>&1", "cat", "read"),
        ("cat > f <<'EOF'\nhi\nEOF", "cat", "write"),
        ("pnpm check-all 2>&1 | tail -10", "pnpm", "validate"),
        ("pnpm --filter @scope/api type-check", "pnpm", "validate"),
        ("bun run test:integration", "bun", "validate"),
        ("yarn lint", "yarn", "validate"),
        ("npx vitest run", "vitest", "validate"),
        ("just test-integration", "just", "validate"),
        ("pnpm install", "pnpm", "shell"),
        ("npm run checkout", "npm", "shell"),
        ("just checkout-branch", "just", "shell"),
        ("yarn testdata", "yarn", "shell"),
        ("npm run test:unit", "npm", "validate"),
    ],
)
def test_command_class_and_shell_kind(command, cls, kind):
    assert command_class("Bash", command) == cls
    assert tool_kind("Bash", command) == kind


def test_command_class_strips_prelude_and_wrappers():
    assert command_class("Bash", "cd /x && FOO=1 uv run pytest -q | tail -5; echo done") == "pytest"


def test_unbalanced_quotes_still_classify():
    assert command_class("Bash", "grep 'unterminated foo") == "grep"


def test_command_class_without_command_is_the_tool():
    assert command_class("Read", None) == "Read"


@pytest.mark.parametrize(
    ("tool", "kind"),
    [
        ("Read", "read"),
        ("NotebookRead", "read"),
        ("Grep", "search"),
        ("Glob", "search"),
        ("LS", "search"),
        ("WebSearch", "search"),
        ("WebFetch", "search"),
        ("ToolSearch", "search"),
        ("Edit", "write"),
        ("Write", "write"),
        ("MultiEdit", "write"),
        ("NotebookEdit", "write"),
        ("mcp__jina__search_web", "mcp"),
        ("Skill", "skill"),
        ("Task", "agent"),
        ("Agent", "agent"),
        ("TodoWrite", "other"),
    ],
)
def test_tool_kind_by_name(tool, kind):
    assert tool_kind(tool, None) == kind


def test_bash_without_command_is_shell():
    assert tool_kind("Bash", None) == "shell"


def test_mcp_parts_split_server_and_tool():
    assert mcp_parts("mcp__plugin_telegram_telegram__reply") == (
        "plugin_telegram_telegram",
        "reply",
    )
    assert mcp_parts("Bash") is None


def test_error_signature_uses_the_error_line():
    text = "Exit code 1\nfoo.py:3: error: bad"
    assert error_line(text) == "foo.py:3: error: bad"
    assert (
        error_signature("Bash", "mypy", "exit_code", text)
        == "mypy|exit_code|foo.py:<n>: error: bad"
    )


def test_traceback_signature_uses_its_last_line():
    text = 'Exit code 1\nTraceback (most recent call last):\n  File "x.py", line 3\nValueError: bad'
    assert error_line(text) == "ValueError: bad"
    assert error_signature("Bash", "python", "runtime_error", text) == (
        "python|runtime_error|valueerror: bad"
    )


def test_error_line_falls_back_past_the_exit_code():
    assert error_line("Exit code 3\nsomething odd") == "something odd"


def test_signature_without_category_says_error():
    assert error_signature("Read", "Read", None, "nope") == "Read|error|nope"


def test_grep_exit_one_without_output_is_benign():
    assert benign_exit("grep needle f", 1, "Exit code 1")
    assert benign_exit("diff a b", 1, "Exit code 1\n< a\n---\n> b")
    assert not benign_exit("diff a b", 1, "Exit code 1\ndiff: missing: No such file")
    assert not benign_exit("grep needle f", 2, "Exit code 2")
    assert not benign_exit("pytest", 1, "Exit code 1")


def test_git_grep_exit_one_without_output_is_benign():
    assert benign_exit("git grep needle", 1, "Exit code 1")
    assert benign_exit("git -C repo grep -n needle", 1, "Exit code 1")
    assert not benign_exit("git grep needle", 1, "Exit code 1\nfatal: not a git repository")
    assert not benign_exit("git status", 1, "Exit code 1")


@pytest.mark.parametrize(
    "command",
    [
        "ls src; grep -n needle src/a.py",
        "echo ---\ngit grep needle",
        "make || grep needle out.txt",
        "grep -rn needle src | head -20",
        "ls src; echo ===; grep -n needle src/a.py | sort | head -5",
    ],
)
def test_compound_command_ending_in_a_search_that_exits_one_is_benign(command):
    assert benign_exit(command, 1, "Exit code 1\nsrc/a.py\nlisting output")


@pytest.mark.parametrize(
    "command",
    [
        "cat app.log | rg panic",
        "npm test | grep -v WARN",
        "pytest -q 2>&1 | grep -E 'passed|failed'",
    ],
)
def test_a_search_fed_by_a_pipe_is_benign_only_without_output(command):
    assert benign_exit(command, 1, "Exit code 1")
    assert benign_exit(command, 1, "Exit code 1\n3 failed, 10 passed") is False


@pytest.mark.parametrize(
    ("command", "code"),
    [
        ("make && grep needle out.txt", 1),
        ("ls src; grep -n needle src/a.py", 2),
        ("grep -n needle src/a.py; ls src", 1),
        ("cat missing.toml | head -30", 1),
        ("make && grep needle out.txt | head", 1),
        ("grep needle out.txt | xargs rm", 1),
    ],
)
def test_compound_search_exit_is_an_error_after_and_or_a_trailing_command(command, code):
    assert benign_exit(command, code, f"Exit code {code}\nmake: *** [all] Error 1") is False


def test_a_compound_command_led_by_grep_keeps_the_no_output_rule():
    assert benign_exit("grep -q x f && echo found", 1, "Exit code 1")


@pytest.mark.parametrize(
    "command",
    [
        "cat > notes.py <<'EOF'\nx = 1 > 0\nEOF",
        "echo done | tee -a progress.txt",
        "git diff > /tmp/change.patch",
        "cd sub && printf 'a' >> out.txt",
        "printf x 1> out.txt",
        "cmd &> out.log",
        "cmd >| out.txt",
    ],
)
def test_shell_file_writes_are_writes(command):
    assert tool_kind("Bash", command) == "write"


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("python - <<'EOF'\nprint(1 > 0)\nEOF", "shell"),
        ("cat <<'EOF'\nhello > world\nEOF", "shell"),
        ("ls src > /dev/null", "search"),
        ("make 2> err.log", "shell"),
        ("echo oops > /dev/stderr", "shell"),
        ("echo 'a << b'\nrg foo src > hits.txt\nb\nls", "write"),
        ("ls src 2>&1", "search"),
        ("echo x | tee /dev/null", "shell"),
        ("pytest -q 2>&1 | tee test.log", "validate"),
    ],
)
def test_redirects_to_null_stderr_and_heredoc_bodies_are_not_writes(command, kind):
    assert tool_kind("Bash", command) == kind


def test_heredoc_bodies_are_not_commands():
    command = "cat <<'EOF'\nrm -rf x; grep y\nEOF"
    assert (command_class("Bash", command), tool_kind("Bash", command)) == ("cat", "shell")


def test_execute_is_a_shell_tool_and_apply_patch_is_a_write():
    assert tool_kind("Execute", "pytest -q") == "validate"
    assert tool_kind("Execute", "rg needle src") == "search"
    assert command_class("Execute", "uv run mypy") == "mypy"
    assert tool_kind("ApplyPatch", None) == "write"


def test_execute_failures_report_their_exit_code():
    assert exit_code("Error: Command failed (exit code: 1)\n") == 1
    assert benign_exit("rg needle src", 1, "Error: Command failed (exit code: 1)")


def test_patch_path_is_the_first_file_the_patch_touches():
    patch = "*** Begin Patch\n*** Update File: /w/src/a.rs\n@@\n-x\n+y\n*** Add File: /w/b.rs\n"
    assert patch_path(patch) == "/w/src/a.rs"
    assert patch_path("*** Begin Patch\n*** Delete File: old.py\n*** End Patch") == "old.py"
    assert patch_path("no patch header") is None


def test_normalize_collapses_paths_uuids_and_lines():
    a = "Error in /Users/a/proj/x.py line 12: id f0e67102-7cc1-4455-b572-c0cc2c21a4ac"
    b = "Error in /home/b/other/y.py line 99: id d1702469-0775-4bb1-a2c5-46c0a5e070ba"
    assert normalize(a) == normalize(b)
    assert normalize("at /tmp/abc/x:3:4  deadbeefcafe") == "at <tmp>:<n> <hex>"
    assert normalize("~/x/y.txt failed") == "<path> failed"


def test_normalize_caps_length():
    assert len(normalize("word " * 200)) == 200


def test_cap_error_keeps_head_and_tail():
    text = "h" * 600 + "m" * 3000 + "t" * 400
    assert cap_error(text) == "h" * 600 + "\n…\n" + "t" * 400
    assert cap_error("short") == "short"


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("src/app.py", True),
        ("pyproject.toml", True),
        ("README.md", False),
        ("notes.txt", False),
        ("guide.rst", False),
        ("docs/api/schema.json", False),
        ("data/rows.csv", False),
    ],
)
def test_is_code_file(path, code):
    assert is_code_file(path) is code


@pytest.mark.parametrize(
    ("text", "origin", "slash", "interrupt", "kind"),
    [
        ("do it", "peer", None, False, "other"),
        ("[Request interrupted by user]", "human", None, True, "other"),
        ("<command-name>/commit</command-name>", "human", "/commit", False, "new_task"),
        ("ok go ahead", "human", None, False, "approval"),
        ("yes but rename it first", "human", None, False, None),
        ("please refactor the scanner", "human", None, False, None),
    ],
)
def test_rule_message_kind(text, origin, slash, interrupt, kind):
    assert rule_message_kind(text, origin, slash, interrupt) == kind


def test_input_hash_ignores_key_order():
    assert input_hash({"a": 1, "b": 2}) == input_hash({"b": 2, "a": 1})


def test_preview_flattens_and_truncates():
    assert preview("a\n  b", 10) == "a b"
    assert preview("x" * 20, 10) == "x" * 9 + "…"


def test_fingerprint_material_covers_tables_and_caps():
    material = json.loads(taxonomy.fingerprint_material())
    assert material["caps"]["command"] == 500
    assert "grep" in material["sets"]["benign_exit1_commands"]
    assert material["error_categories"][0][0] == "interruption"


def test_externally_modified_file_is_a_stale_read():
    sample = 'File "/w/a.py" has been modified externally since it was last read'
    assert categorize_error(sample) == "file_modified_since_read"


def test_generic_categories_can_be_skipped():
    assert categorize_error("ValueError: boom", generic=False) is None
    assert categorize_error("Exit code 2\nboom", generic=False) is None
    assert categorize_error("File has not been read yet", generic=False) == "file_not_read"


def test_traceback_signature_uses_the_exception_line_not_the_output_tail():
    text = (
        'Exit code 1\nTraceback (most recent call last):\n  File "x.py", line 3, in f\n'
        "    boom()\nKeyError: 'a'\nmake: *** [test] Error 1"
    )
    assert error_line(text) == "KeyError: 'a'"


def test_starts_with_marker_ignores_quoted_mentions():
    markers = taxonomy.INTERRUPT_MARKERS
    assert taxonomy.starts_with_marker("  [Request interrupted by user]", markers)
    assert taxonomy.starts_with_marker("<tool_use_error>[Request interrupted by user]", markers)
    assert not taxonomy.starts_with_marker("grep: [Request interrupted by user]", markers)


def test_error_categories_match_the_category_type():
    named = [name for name, _pattern in taxonomy.ERROR_CATEGORIES]
    assert named == list(get_args(ErrorCategory.__value__))
    assert set(NON_AGENT_FAILURE_CATEGORIES) | set(taxonomy.GENERIC_CATEGORIES) <= set(named)
