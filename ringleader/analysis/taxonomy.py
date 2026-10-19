from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import PurePosixPath

from . import facts
from .facts import ErrorCategory, MessageKind, MessageOrigin, ToolKind

ERROR_CATEGORIES: tuple[tuple[ErrorCategory, re.Pattern[str]], ...] = (
    (
        "interruption",
        re.compile(
            r"\[Request interrupted by user|\binterrupted by user\b|^aborted by user\b",
            re.IGNORECASE,
        ),
    ),
    (
        "user_rejected",
        re.compile(
            r"doesn't want to (?:proceed with this tool use|take this action)"
            r"|\bUser rejected tool use\b"
        ),
    ),
    (
        "permission_denied",
        re.compile(
            r"Permission (?:for this \w+(?: \w+)? was denied|to use \S+ [\s\S]*?denied)"
            r"|\bpermission denied\b|Operation not permitted|\bEACCES\b|\bEPERM\b"
            r"|auto mode cannot determine the safety|auto mode classifier gave no verdict",
            re.IGNORECASE,
        ),
    ),
    (
        "hook_blocked",
        re.compile(r"\w+:\w+ hook error|\bhook\b.*\bblock|\bBlocked by\b.*\bhook\b", re.IGNORECASE),
    ),
    (
        "harness_blocked",
        re.compile(r"\bBlocked: (?:standalone )?sleep \d+|\bis isolated in the worktree\b"),
    ),
    (
        "sibling_cancelled",
        re.compile(r"\bSibling tool call errored\b|\bCancelled: parallel tool call\b"),
    ),
    (
        "subagent_error",
        re.compile(
            r"Error running task subagent|Concurrent subagent limit reached"
            r"|Fork is not available inside a forked worker|Subagent process exited"
            r"|collab spawn failed"
        ),
    ),
    ("timeout", re.compile(r"\btimed out\b|\bETIMEDOUT\b|deadline exceeded", re.IGNORECASE)),
    ("file_too_large", re.compile(r"exceeds maximum allowed (?:tokens|size)")),
    ("bad_path", re.compile(r"\b(?:File|Path) does not exist\b|\bENOENT\b|\bEISDIR\b")),
    (
        "fetch_failed",
        re.compile(
            r"(?m)\bunable to fetch from\b|^(?:Error: )?HTTP [45]\d\d\b|domains are not accessible"
        ),
    ),
    ("file_not_read", re.compile(r"File has not been read yet")),
    (
        "file_modified_since_read",
        re.compile(r"File has been modified since read|has been modified externally since it was"),
    ),
    (
        "string_not_found",
        re.compile(r"String to replace not found|Failed to find expected lines"),
    ),
    ("no_changes", re.compile(r"No changes to make")),
    (
        "tool_not_found",
        re.compile(
            r"No such tool available|Unknown skill\b|Agent type '[^']*' not found"
            r"|\bcommand not found\b"
        ),
    ),
    (
        "input_validation",
        re.compile(
            r"InputValidationError|Invalid tool parameters|Input validation error|invalid patch:"
        ),
    ),
    (
        "syntax_error",
        re.compile(
            r"\bSyntaxError\b|\bsyntax error\b|\bparse error\b|unexpected token", re.IGNORECASE
        ),
    ),
    (
        "runtime_error",
        re.compile(
            r"Traceback \(most recent call last\)|\b[A-Z][A-Za-z]*(?:Error|Exception)\b"
            r"|\bpanic:|Segmentation fault"
        ),
    ),
    (
        "exit_code",
        re.compile(r"(?m)^(?:Error: )?Exit code -?\d+|Command failed \(exit code: -?\d+\)"),
    ),
)

READ_TOOLS = frozenset({"Read", "NotebookRead", "view_image"})
SEARCH_TOOLS = frozenset(
    {"Grep", "Glob", "LS", "WebSearch", "WebFetch", "ToolSearch", "web_search"}
)
PATCH_TOOLS = frozenset({"ApplyPatch", "apply_patch"})
WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"}) | PATCH_TOOLS
SHELL_TOOLS = frozenset({"Bash", "Execute", "exec_command"})
SKILL_TOOLS = frozenset({"Skill"})
AGENT_TOOLS = frozenset(
    {
        "Task",
        "Agent",
        "spawn_agent",
        "send_message",
        "followup_task",
        "wait_agent",
        "close_agent",
        "interrupt_agent",
        "list_agents",
    }
)
CODEX_SHELL_CALLS = frozenset({"exec_command", "shell", "shell_command", "local_shell"})
CODEX_SCRIPT_CALLS = frozenset({"exec", "wait"})
CODEX_SHELLS = frozenset({"bash", "zsh", "sh", "dash", "fish", "ksh"})
CODEX_SHELL_FLAGS = frozenset({"-c", "-lc"})
PARSED_CMD_KINDS: dict[str, ToolKind] = {"read": "read", "search": "search", "list_files": "search"}
CODEX_OK_STATUSES = frozenset({"completed"})
CODEX_REJECTED_STATUSES = frozenset({"declined"})
CODEX_OUTPUT_HEADER = re.compile(
    r"\A(?:(?:Exit code:|Chunk ID:|Wall time:|Process exited with code|Process running"
    r"|Original token count:)[^\n]*\n)+Output:[^\S\n]*\n?"
)
CODEX_OUTPUT_EXIT = re.compile(r"(?m)^(?:Exit code:|Process exited with code) (-?\d+)\s*$")
CODEX_FAILED_OUTPUT = re.compile(
    r"^(?:failed in sandbox|execution error|error:|Script failed|aborted by user"
    r"|apply_patch verification failed|collab spawn failed)",
    re.IGNORECASE,
)
CODEX_INJECTED_PREFIXES: tuple[str, ...] = ("<skill>", "<environment_context>")
READ_COMMANDS = frozenset(
    {"cat", "head", "tail", "less", "more", "bat", "nl", "wc", "stat", "file"}
)
SEARCH_COMMANDS = frozenset(
    {"grep", "egrep", "rg", "ag", "ack", "ugrep", "find", "fd", "ls", "tree", "locate", "which"}
)
BENIGN_EXIT1_COMMANDS = frozenset({"grep", "rg", "diff", "test", "[", "cmp"})
TRAILING_SEARCH_COMMANDS = frozenset({"grep", "egrep", "fgrep", "rg"})
PIPE_FILTERS = frozenset({"head", "tail", "sort", "uniq", "wc", "cut", "tr", "column", "nl"})
GIT_VALUE_FLAGS = frozenset({"-C", "-c"})
NULL_SINKS = frozenset({"/dev/null", "/dev/stderr", "/dev/stdout", "/dev/tty"})
WRITE_REDIRECTS = frozenset({">", ">>", "&>", "&>>", ">|"})
OUTPUT_PREFIXED_COMMANDS = frozenset({"diff", "cmp"})
COMMAND_WRAPPERS = frozenset(
    {
        "uv run",
        "uvx",
        "npx",
        "bunx",
        "pnpm exec",
        "poetry run",
        "env",
        "time",
        "sudo",
        "nohup",
        "timeout",
        "nice",
        "exec",
        "command",
    }
)
WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "uv run": frozenset({"--with", "--from", "--project", "--directory", "--python"}),
    "uvx": frozenset({"--with", "--from", "--python"}),
    "env": frozenset({"-u"}),
    "sudo": frozenset({"-u", "-g"}),
    "timeout": frozenset({"-s", "-k"}),
    "nice": frozenset({"-n"}),
}
GENERIC_CATEGORIES: frozenset[ErrorCategory] = frozenset({"runtime_error", "exit_code"})
LEADING_SYNTAX = frozenset({"(", "{", "!", "do", "then", "else"})
HEADER_KEYWORDS = frozenset({"for", "select", "case", "if", "elif", "while", "until", "(("})
CLOSING_BRACKETS = frozenset({")", "))", "}"})
CLOSING_WORDS = frozenset({"done", "fi", "esac"})
PRELUDE_COMMANDS = frozenset({"cd", "pushd", "export", "set", "source"})
COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "|&", ";;"})
VALIDATION_PATTERN = re.compile(
    r"^(?:pytest|py\.test|python[\d.]* -m (?:pytest|mypy|ruff|unittest)"
    r"|just (?:check|test|lint|typecheck)(?:[:-][\w:-]*)?"
    r"|(?:npm|pnpm|yarn|bun)(?: (?:--filter|-F|-C|--prefix|--dir) \S+)* (?:run )?"
    r"(?:test|check|lint|type-?check|typecheck|verify|build)(?:[:-][\w:-]*)?"
    r"|cargo (?:test|check|clippy|build|nextest)|go (?:test|vet|build)|make (?:test|check)"
    r"|mypy|ruff|tsc|eslint|vitest|jest|biome|pyright|basedpyright|playwright test"
    r"|swift (?:build|test)|xcodebuild)(?:\s|$)"
)
ERROR_LINE_PATTERN = re.compile(
    r"Traceback \(most recent call last\)|\w*(?:Error|Exception)\b|\berror\b|\bFAIL(?:ED)?\b"
    r"|\bfatal\b|No such file|not found|\bcannot\b|\bdenied\b|\bpanic:",
    re.IGNORECASE,
)
EXIT_CODE_LINE = re.compile(
    r"(?m)^(?:Error: )?(?:Exit code (-?\d+)|Command failed \(exit code: (-?\d+)\))\s*$"
)
TRACEBACK_CHAIN = ("During handling of the above exception", "The above exception was")
TRACEBACK_LINE = re.compile(r"Traceback \(most recent call last\)")
HEREDOC_START = re.compile(
    r"\"[^\"\n]*\"|'[^'\n]*'|(?<!<)<<(?!<)-?\s*(['\"]?)(?P<word>[A-Za-z_][\w-]*)\1"
)
PATCH_FILE_LINE = re.compile(r"(?m)^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$")
ACK_PATTERN = re.compile(
    r"(?:(?:ok(?:ay)?|k|yes|yep|yeah|y|sure|go|ahead|do|it|proceed|continue|lgtm|sounds|good"
    r"|perfect|great|thanks|thank|you|ty|approved?|ship|nice|cool|please|pls|fine|correct"
    r"|right|exactly|start|done)[\s!.,]*)+",
    re.IGNORECASE,
)
ACK_MAX_WORDS = 4
INTERRUPT_MARKERS: tuple[str, ...] = (
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
)
REJECTION_MARKERS: tuple[str, ...] = (
    "The user doesn't want to proceed with this tool use",
    "User rejected tool use",
)
TOOL_USE_ERROR_MARKER = "<tool_use_error>"
TOOL_USE_ERROR_TAGS = re.compile(r"</?tool_use_error>")
COMMAND_NAME_PATTERN = re.compile(r"<command-name>\s*(/?[^<\s]+)\s*</command-name>")
COMMAND_OUTPUT_MARKERS: tuple[str, ...] = (
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<bash-stdout>",
    "<bash-stderr>",
)
CODE_FILE_EXCLUDED_SUFFIXES = frozenset({".md", ".txt", ".rst", ".csv", ".tsv", ".log"})
CODE_FILE_EXCLUDED_DIRS = frozenset({"docs"})
SIGNATURE_CHARS = 200
INPUT_PREVIEW_CHARS = 200
PYTHON_PATTERN = re.compile(r"python[\d.]*")
ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
DURATION_PATTERN = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
NORMALIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:/private)?/(?:tmp|var/folders)(?:/[\w.@+~-]+)*/?"), "<tmp>"),
    (re.compile(r"(?<![\w.<])~?/[\w.@+~-]+(?:/[\w.@+~-]*)*"), "<path>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b"), "<hex>"),
    (re.compile(r"\bline \d+"), "line <n>"),
    (re.compile(r":\d+(?::\d+)?\b"), ":<n>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def categorize_error(text: str, *, generic: bool = True) -> ErrorCategory | None:
    return next(
        (
            name
            for name, pattern in ERROR_CATEGORIES
            if (generic or name not in GENERIC_CATEGORIES) and pattern.search(text)
        ),
        None,
    )


def starts_with_marker(text: str, markers: tuple[str, ...]) -> bool:
    lead = TOOL_USE_ERROR_TAGS.sub("", text.lstrip(), count=1).lstrip()
    return lead.startswith(markers)


def _without_heredoc_bodies(command: str) -> str:
    kept: list[str] = []
    pending: list[str] = []
    for line in command.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending = pending[1:]
            continue
        kept.append(line)
        pending = [m.group("word") for m in HEREDOC_START.finditer(line) if m.group("word")]
    return "\n".join(kept)


def _tokens(command: str) -> list[str]:
    flat = _without_heredoc_bodies(command).replace("\n", " ; ")
    lexer = shlex.shlex(flat, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return re.findall(r"&&|\|\||[;|&]|[^\s;|&]+", flat)


def _split(tokens: list[str]) -> list[tuple[str | None, list[str]]]:
    segments: list[tuple[str | None, list[str]]] = [(None, [])]
    for token in tokens:
        if token in COMMAND_SEPARATORS:
            segments.append((token, []))
        else:
            segments[-1][1].append(token)
    return [(joiner, segment) for joiner, segment in segments if segment]


def _wrapper_at(tokens: list[str], i: int) -> str | None:
    pair = " ".join(tokens[i : i + 2])
    if pair in COMMAND_WRAPPERS:
        return pair
    return tokens[i] if tokens[i] in COMMAND_WRAPPERS else None


def _skip_options(tokens: list[str], i: int, wrapper: str) -> int:
    value_flags = WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in value_flags else 1
    if wrapper == "timeout" and i < len(tokens) and DURATION_PATTERN.match(tokens[i]):
        i += 1
    return i


def _unwrap(tokens: list[str]) -> list[str]:
    i = 0
    while i < len(tokens):
        if ASSIGNMENT_PATTERN.match(tokens[i]):
            i += 1
            continue
        wrapper = _wrapper_at(tokens, i)
        if wrapper is None:
            break
        i = _skip_options(tokens, i + len(wrapper.split()), wrapper)
    rest = tokens[i:]
    return [PurePosixPath(rest[0]).name or rest[0], *rest[1:]] if rest else []


def _prelude(segment: list[str]) -> bool:
    return segment[0] in PRELUDE_COMMANDS or all(ASSIGNMENT_PATTERN.match(t) for t in segment)


def _drop_substitutions(tokens: list[str]) -> list[str]:
    kept: list[str] = []
    depth = 0
    for i, token in enumerate(tokens):
        opens = token == "(" and i > 0 and tokens[i - 1].endswith("$")
        if depth or opens:
            depth += (token == "(") - (token == ")")
        elif not (token.endswith("$") and i + 1 < len(tokens) and tokens[i + 1] == "("):
            kept.append(token)
    return kept


def _strip_syntax(segment: list[str]) -> list[str]:
    if segment[0] in HEADER_KEYWORDS or (len(segment) == 1 and segment[0] in CLOSING_WORDS):
        return []
    start = next(
        (i for i, token in enumerate(segment) if token not in LEADING_SYNTAX), len(segment)
    )
    end = len(segment)
    while end > start and segment[end - 1] in CLOSING_BRACKETS:
        end -= 1
    return segment[start:end]


def _joined_segments(command: str) -> list[tuple[str | None, list[str]]]:
    split = _split(_drop_substitutions(_tokens(command)))
    cleaned = [(joiner, _strip_syntax(segment)) for joiner, segment in split]
    segments = [(joiner, segment) for joiner, segment in cleaned if segment]
    start = next(
        (i for i, (_, segment) in enumerate(segments) if not _prelude(segment)), len(segments)
    )
    unwrapped = ((joiner, _unwrap(segment)) for joiner, segment in segments[start:])
    return [(joiner, segment) for joiner, segment in unwrapped if segment]


def _segment_tokens(command: str) -> list[list[str]]:
    return [segment for _, segment in _joined_segments(command)]


def _class_of(segment: list[str]) -> str:
    match segment:
        case [python, "-m", module, *_] if PYTHON_PATTERN.fullmatch(python):
            return module
        case _:
            return segment[0]


def command_class(tool: str, command: str | None) -> str:
    segments = _segment_tokens(command) if command else []
    return _class_of(segments[0]) if segments else tool


def patch_path(patch: str | None) -> str | None:
    found = PATCH_FILE_LINE.search(patch) if patch else None
    return found.group(1) if found else None


def codex_command(argv: object) -> str | None:
    if isinstance(argv, str):
        return argv or None
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return None
    match argv:
        case [shell, flag, script] if (
            PurePosixPath(shell).name in CODEX_SHELLS and flag in CODEX_SHELL_FLAGS
        ):
            return str(script)
        case _:
            return shlex.join(argv)


def parsed_cmd_kind(parsed: object) -> ToolKind | None:
    entries = parsed if isinstance(parsed, list) else []
    kinds = [
        PARSED_CMD_KINDS.get(str(e.get("type"))) if isinstance(e, dict) else None for e in entries
    ]
    if not kinds or None in kinds:
        return None
    return "read" if all(k == "read" for k in kinds) else "search"


def codex_kind(command: str | None, parsed: object) -> ToolKind:
    found = tool_kind("exec_command", command)
    refined = parsed_cmd_kind(parsed)
    return refined if refined is not None and found in {"shell", "read", "search"} else found


def output_exit_code(text: str) -> int | None:
    match = CODEX_OUTPUT_EXIT.search(text)
    return int(match.group(1)) if match else None


def mcp_parts(tool: str) -> tuple[str, str] | None:
    if not tool.startswith("mcp__"):
        return None
    server, _, name = tool[len("mcp__") :].partition("__")
    return (server, name) if server and name else None


def _redirects_to_file(segment: list[str], i: int) -> bool:
    before = segment[i - 1] if i else ""
    target = segment[i + 1] if i + 1 < len(segment) else ""
    return (
        segment[i] in WRITE_REDIRECTS
        and not ((before.isdigit() and before != "1") or before == "&")
        and bool(target)
        and not target.startswith("&")
        and target not in NULL_SINKS
    )


def _writes_file(segment: list[str]) -> bool:
    tees = segment[0] == "tee" and any(
        not token.startswith("-") and token not in NULL_SINKS for token in segment[1:]
    )
    return tees or any(_redirects_to_file(segment, i) for i in range(len(segment)))


def _shell_kind(command: str | None) -> ToolKind:
    segments = _segment_tokens(command) if command else []
    if not segments:
        return "shell"
    if VALIDATION_PATTERN.match(" ".join(segments[0])):
        return "validate"
    if any(_writes_file(segment) for segment in segments):
        return "write"
    cls = _class_of(segments[0])
    if cls in READ_COMMANDS:
        return "shell" if any(token.startswith("<<") for token in segments[0]) else "read"
    return "search" if cls in SEARCH_COMMANDS else "shell"


def tool_kind(tool: str, command: str | None) -> ToolKind:
    simple: tuple[tuple[frozenset[str], ToolKind], ...] = (
        (READ_TOOLS, "read"),
        (SEARCH_TOOLS, "search"),
        (WRITE_TOOLS, "write"),
        (SKILL_TOOLS, "skill"),
        (AGENT_TOOLS, "agent"),
    )
    found = next((kind for names, kind in simple if tool in names), None)
    if found is not None:
        return found
    if tool in SHELL_TOOLS:
        return _shell_kind(command)
    return "mcp" if mcp_parts(tool) else "other"


def exit_code(text: str) -> int | None:
    match = EXIT_CODE_LINE.search(text)
    return int(match.group(1) or match.group(2)) if match else None


def _git_subcommand(segment: list[str]) -> str | None:
    if segment[0] != "git":
        return None
    i = 1
    while i < len(segment) and segment[i].startswith("-"):
        i += 2 if segment[i] in GIT_VALUE_FLAGS else 1
    return segment[i] if i < len(segment) else None


def _searches(segment: list[str]) -> bool:
    return segment[0] in TRAILING_SEARCH_COMMANDS or _git_subcommand(segment) == "grep"


def _trailing_search(joined: list[tuple[str | None, list[str]]], silent: bool) -> bool:
    if len(joined) <= 1:
        return False
    i = len(joined) - 1
    while i > 0 and joined[i][0] == "|" and joined[i][1][0] in PIPE_FILTERS:
        i -= 1
    joiner, segment = joined[i]
    return joiner != "&&" and (silent or joiner != "|") and _searches(segment)


def _exit1_class(segment: list[str]) -> str:
    return "grep" if _git_subcommand(segment) == "grep" else _class_of(segment)


def benign_exit(command: str | None, exit_code: int | None, output: str) -> bool:
    joined = _joined_segments(command) if command else []
    if exit_code != 1 or not joined:
        return False
    rest = [
        line
        for line in TOOL_USE_ERROR_TAGS.sub("", output).splitlines()
        if line.strip() and not EXIT_CODE_LINE.match(line.strip())
    ]
    if _trailing_search(joined, not rest):
        return True
    command_class = _exit1_class(joined[0][1])
    if command_class not in BENIGN_EXIT1_COMMANDS:
        return False
    return not rest or (
        command_class in OUTPUT_PREFIXED_COMMANDS
        and not any(line.startswith(f"{command_class}:") for line in rest)
    )


def normalize(text: str) -> str:
    out = text.lower()
    for pattern, placeholder in NORMALIZERS:
        out = pattern.sub(placeholder, out)
    return out.strip()[:SIGNATURE_CHARS]


def error_line(error_text: str) -> str | None:
    raw = [
        line
        for line in TOOL_USE_ERROR_TAGS.sub("", error_text).splitlines()
        if line.strip() and not EXIT_CODE_LINE.match(line.strip())
    ]
    lines = [line.strip() for line in raw]
    trace = next((i for i in reversed(range(len(raw))) if TRACEBACK_LINE.search(raw[i])), None)
    if trace is not None:
        tail = (line for line in raw[trace + 1 :] if not line[:1].isspace())
        return next(
            (line for line in tail if not line.startswith(TRACEBACK_CHAIN)), lines[-1]
        ).strip()
    return next((line for line in lines if ERROR_LINE_PATTERN.search(line)), None) or next(
        iter(lines), None
    )


def error_signature(
    tool: str, command_class: str, category: ErrorCategory | None, error_text: str | None
) -> str | None:
    if error_text is None and category is None:
        return None
    line = error_line(error_text) if error_text else None
    return f"{command_class or tool}|{category or 'error'}|{normalize(line or '')}"


def cap_error(text: str) -> str:
    if len(text) <= facts.ERROR_HEAD_CHARS + facts.ERROR_TAIL_CHARS:
        return text
    return f"{text[: facts.ERROR_HEAD_CHARS]}\n…\n{text[-facts.ERROR_TAIL_CHARS :]}"


def is_code_file(path: str) -> bool:
    pure = PurePosixPath(path)
    if pure.suffix.lower() in CODE_FILE_EXCLUDED_SUFFIXES:
        return False
    return not any(part in CODE_FILE_EXCLUDED_DIRS for part in pure.parts[:-1])


def rule_message_kind(
    text: str, origin: MessageOrigin, slash_command: str | None, is_interrupt: bool
) -> MessageKind | None:
    if origin != "human" or is_interrupt:
        return "other"
    if slash_command:
        return "new_task"
    stripped = text.strip()
    if len(stripped.split()) <= ACK_MAX_WORDS and ACK_PATTERN.fullmatch(stripped):
        return "approval"
    return None


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def input_hash(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def printable(text: str) -> str:
    return "".join(c for c in " ".join(text.split()) if c.isprintable())


def printable_lines(text: str) -> str:
    return "\n".join("".join(c for c in line if c.isprintable()) for line in text.splitlines())


def preview(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[: limit - 1]}…"


def _pattern(pattern: re.Pattern[str]) -> list[object]:
    return [pattern.pattern, pattern.flags]


def fingerprint_material() -> str:
    material = {
        "traceback_chain": list(TRACEBACK_CHAIN),
        "error_categories": [[name, _pattern(p)] for name, p in ERROR_CATEGORIES],
        "sets": {
            name: sorted(value)
            for name, value in {
                "read_tools": READ_TOOLS,
                "search_tools": SEARCH_TOOLS,
                "write_tools": WRITE_TOOLS,
                "shell_tools": SHELL_TOOLS,
                "skill_tools": SKILL_TOOLS,
                "agent_tools": AGENT_TOOLS,
                "codex_shell_calls": CODEX_SHELL_CALLS,
                "codex_script_calls": CODEX_SCRIPT_CALLS,
                "codex_shells": CODEX_SHELLS,
                "codex_shell_flags": CODEX_SHELL_FLAGS,
                "codex_ok_statuses": CODEX_OK_STATUSES,
                "codex_rejected_statuses": CODEX_REJECTED_STATUSES,
                "read_commands": READ_COMMANDS,
                "search_commands": SEARCH_COMMANDS,
                "benign_exit1_commands": BENIGN_EXIT1_COMMANDS,
                "trailing_search_commands": TRAILING_SEARCH_COMMANDS,
                "pipe_filters": PIPE_FILTERS,
                "git_value_flags": GIT_VALUE_FLAGS,
                "null_sinks": NULL_SINKS,
                "write_redirects": WRITE_REDIRECTS,
                "patch_tools": PATCH_TOOLS,
                "output_prefixed_commands": OUTPUT_PREFIXED_COMMANDS,
                "command_wrappers": COMMAND_WRAPPERS,
                "generic_categories": GENERIC_CATEGORIES,
                "leading_syntax": LEADING_SYNTAX,
                "header_keywords": HEADER_KEYWORDS,
                "closing_brackets": CLOSING_BRACKETS,
                "closing_words": CLOSING_WORDS,
                "prelude_commands": PRELUDE_COMMANDS,
                "command_separators": COMMAND_SEPARATORS,
                "code_file_excluded_suffixes": CODE_FILE_EXCLUDED_SUFFIXES,
                "code_file_excluded_dirs": CODE_FILE_EXCLUDED_DIRS,
            }.items()
        },
        "patterns": {
            name: _pattern(p)
            for name, p in {
                "validation": VALIDATION_PATTERN,
                "error_line": ERROR_LINE_PATTERN,
                "exit_code_line": EXIT_CODE_LINE,
                "traceback_line": TRACEBACK_LINE,
                "heredoc_start": HEREDOC_START,
                "patch_file_line": PATCH_FILE_LINE,
                "ack": ACK_PATTERN,
                "tool_use_error_tags": TOOL_USE_ERROR_TAGS,
                "command_name": COMMAND_NAME_PATTERN,
                "python": PYTHON_PATTERN,
                "assignment": ASSIGNMENT_PATTERN,
                "duration": DURATION_PATTERN,
                "codex_output_exit": CODEX_OUTPUT_EXIT,
                "codex_output_header": CODEX_OUTPUT_HEADER,
                "codex_failed_output": CODEX_FAILED_OUTPUT,
            }.items()
        },
        "wrapper_value_flags": {k: sorted(v) for k, v in WRAPPER_VALUE_FLAGS.items()},
        "parsed_cmd_kinds": PARSED_CMD_KINDS,
        "normalizers": [[*_pattern(p), placeholder] for p, placeholder in NORMALIZERS],
        "markers": {
            "interrupt": list(INTERRUPT_MARKERS),
            "rejection": list(REJECTION_MARKERS),
            "tool_use_error": TOOL_USE_ERROR_MARKER,
            "command_output": list(COMMAND_OUTPUT_MARKERS),
            "codex_injected": list(CODEX_INJECTED_PREFIXES),
        },
        "caps": {
            "command": facts.COMMAND_CHARS,
            "error_head": facts.ERROR_HEAD_CHARS,
            "error_tail": facts.ERROR_TAIL_CHARS,
            "final_text": facts.FINAL_TEXT_CHARS,
            "message": facts.MESSAGE_CHARS,
            "rebuild_read_ratio": facts.REBUILD_READ_RATIO,
            "signature": SIGNATURE_CHARS,
            "input_preview": INPUT_PREVIEW_CHARS,
            "ack_max_words": ACK_MAX_WORDS,
        },
    }
    return json.dumps(material, sort_keys=True, ensure_ascii=False)
