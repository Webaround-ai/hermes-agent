"""Tier 4 in code: terminal commands Iollo refuses without asking anyone or any model.

Two checks run over every command variant the fork's own detector builds
(``tools.approval_detection._deny_command_variants``: ANSI/escape/empty-quote/``$IFS`` normalization,
wrapper-stripped executable projections such as ``sudo dd`` -> ``dd``, ``bash -c`` payloads):

1. ``tier4.commands`` regexes from ``permissions.yaml`` (``re.search``, case-insensitive, multiline;
   projections start at the command word, so ``^`` anchors a command).
2. The targets of destructive file-system verbs (rm, mv, truncate, find -delete, ``>`` ...), resolved with
   ``realpath`` against the command's working directory (following ``cd``). Destroying or overwriting a path
   outside the workspace, the scratch roots (``scratch_roots``) and the write roots is a hard block; so is deleting the
   workspace itself or its versions/trash store. Any terminal write inside a write root that is not the
   workspace is blocked with a pointer to the versioned file tools.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
import threading
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from .policy import Settings

DELETE, OVERWRITE, WRITE = "delete", "overwrite", "write"

_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "|&", ";;", "\n"}
_REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<>"}
_WRAPPERS = {"sudo", "doas", "env", "nohup", "time", "command", "builtin", "exec", "nice", "ionice",
             "timeout", "stdbuf", "xargs", "caffeinate", "setsid", "chroot"}
_WRAPPER_OPTS_WITH_ARG = {"-u", "--user", "-g", "--group", "-n", "-I", "-P", "-L", "-s", "-d", "-E", "-c",
                          "--adjustment", "-o", "-i", "-e", "-C", "-h", "--host", "-p", "--prompt"}
_DELETE_VERBS = {"rm", "rmdir", "unlink", "shred", "srm"}
_METADATA_VERBS = {"chmod", "chown", "chgrp", "chflags", "xattr"}
_COPY_VERBS = {"cp", "install", "ln", "ditto"}
_CREATE_VERBS = {"touch", "mkdir"}
_DEV_ALLOWED = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero", "/dev/random", "/dev/urandom")
_UNRESOLVED = None

# Quoted arguments are data unless a shell -c/eval wrapper executes them or a double-quoted
# span contains command substitution. Keep those execution contexts visible to the policy.
_SQUOTE = r"'[^'\\]*'"
_DQUOTE = r'"(?:[^"\\]|\\.)*"'
_QUOTE_SPAN = re.compile(f"{_SQUOTE}|{_DQUOTE}", re.S)
_EXEC_C = re.compile(r"(?:^|[\s;&|(])(?:(?:[\w./-]*/)?(?:bash|sh|zsh|dash|ksh)\s+(?:-\S+\s+)*-\w*c|eval)$", re.I)
_SUBSHELL = re.compile(r"\$\(|`|<\(")


def _unquote_for_scan(command: str) -> str:
    """Mask quoted data, recursively scanning shell -c/eval arguments and keeping substitutions.

    Malformed quoting keeps the raw command so it cannot weaken the hard-block scan.
    """
    try:
        shlex.split(command)
    except ValueError:
        return command
    parts = []
    last = 0
    for m in _QUOTE_SPAN.finditer(command):
        parts.append(command[last:m.start()])
        span = m.group(0)
        inner = span[1:-1]
        if _EXEC_C.search(command[:m.start()].rstrip()):
            parts.append(" " + _unquote_for_scan(inner) + " ")
        elif span[0] == '"' and _SUBSHELL.search(inner):
            parts.append(" " + inner + " ")
        else:
            parts.append(" ")
        last = m.end()
    parts.append(command[last:])
    return "".join(parts)


@dataclass(frozen=True)
class Target:
    kind: str                  # delete | overwrite | write
    path: Optional[str]        # realpath, or None when it cannot be resolved (variables, substitutions)
    verb: str


def command_variants(command: str) -> List[str]:
    """The raw command plus the fork's normalized/projection variants (deduplicated, order kept)."""
    variants = [command]
    try:
        from tools.approval_detection import _deny_command_variants
        variants.extend(_deny_command_variants(command))
    except Exception:  # detector unavailable or choked: the raw text still gets checked
        pass
    return list(dict.fromkeys(v for v in variants if isinstance(v, str) and v.strip()))


def match_tier4_command(command: str, settings: Settings) -> Optional[str]:
    """The ``action`` of the first ``tier4.commands`` rule matching any variant, else None."""
    for variant in command_variants(command):
        scan = _unquote_for_scan(variant)
        for rule in settings.policy.tier4_commands:
            if rule.regex.search(scan):
                return rule.action
    return None


def _tokens(text: str) -> List[str]:
    text = text.replace("`", " ; ")
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:  # unbalanced quotes: fall back to plain words
        return re.sub(r"([;&|()<>])", r" \1 ", text).split()


def _segments(tokens: List[str]) -> List[Tuple[List[str], str]]:
    """Split into simple commands, each with the separator that FOLLOWS it."""
    out, current = [], []
    for token in tokens:
        if token in _SEPARATORS:
            out.append((current, token))
            current = []
        else:
            current.append(token)
    out.append((current, ""))
    return [(seg, sep) for seg, sep in out if seg]


def _strip_wrappers(words: List[str]) -> Tuple[List[str], bool]:
    """Drop ``VAR=x`` prefixes and wrapper commands; returns (words, fed_by_xargs)."""
    fed = False
    i = 0
    while i < len(words):
        word = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
            i += 1
            continue
        base = os.path.basename(word)
        if base not in _WRAPPERS:
            break
        fed = fed or base == "xargs"
        i += 1
        positional = 1 if base in {"timeout", "chroot"} else 0
        while i < len(words):
            nxt = words[i]
            if nxt == "--":
                i += 1
                break
            if nxt.startswith("-"):
                i += 2 if nxt in _WRAPPER_OPTS_WITH_ARG else 1
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", nxt):
                i += 1
                continue
            if positional:
                positional -= 1
                i += 1
                continue
            break
    return words[i:], fed


def _operands(args: List[str], opts_with_arg: Iterable[str] = ()) -> List[str]:
    out, i, ended = [], 0, False
    opts_with_arg = set(opts_with_arg)
    while i < len(args):
        arg = args[i]
        if not ended and arg == "--":
            ended = True
        elif not ended and arg.startswith("-") and arg != "-":
            if arg in opts_with_arg:
                i += 1
        else:
            out.append(arg)
        i += 1
    return out


_fold = threading.local()


def _hermes_home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.path.expanduser("~/.hermes")


def _resolve(raw: str, cwd: Optional[str]) -> List[Optional[str]]:
    """Resolve one operand to realpaths (globs expanded); ``[None]`` when it cannot be resolved."""
    if getattr(_fold, "active", False) and (raw == "~/.hermes" or raw.startswith("~/.hermes/")):
        # The fork's normalization folds the absolute HERMES_HOME to ``~/.hermes``; undo that on its variants
        # (the raw command is analysed as written, so a literal ``~/.hermes`` still resolves under $HOME).
        raw = _hermes_home() + raw[len("~/.hermes"):]
    expanded = os.path.expanduser(os.path.expandvars(raw))
    if any(ch in expanded for ch in "$`") or "(" in expanded:
        return [_UNRESOLVED]
    if not os.path.isabs(expanded):
        if cwd is None:
            return [_UNRESOLVED]
        expanded = os.path.join(cwd, expanded)
    if glob.has_magic(expanded):
        matches = glob.glob(expanded)
        if matches:
            return [os.path.realpath(m) for m in matches]
        prefix = re.split(r"[*?\[]", expanded, maxsplit=1)[0]
        return [os.path.join(os.path.realpath(os.path.dirname(prefix) or "/"), "__glob__")]
    return [os.path.realpath(expanded)]


def _exists_kind(path: Optional[str]) -> str:
    return OVERWRITE if path is not None and os.path.lexists(path) else WRITE


def _copy_dest(targets: List[Target], verb: str, sources: List[str], dest: str, cwd: Optional[str]) -> None:
    """A copy/move destination: an existing file is overwritten; into a directory, each source's name counts."""
    for path in _resolve(dest, cwd):
        if path is not None and os.path.isdir(path):
            for source in sources or [""]:
                child = os.path.join(path, os.path.basename(source.rstrip("/")) or "__new__")
                targets.append(Target(_exists_kind(child), child, verb))
        else:
            targets.append(Target(_exists_kind(path), path, verb))


def _segment_targets(verb: str, args: List[str], cwd: Optional[str], fed: bool) -> List[Target]:
    targets: List[Target] = []

    def add(kind: str, raws: Iterable[str]) -> None:
        for raw in raws:
            for path in _resolve(raw, cwd):
                targets.append(Target(kind if kind != "auto" else _exists_kind(path), path, verb))

    if verb in _DELETE_VERBS:
        ops = _operands(args, {"-n", "-s", "--iterations", "--size"})
        add(DELETE, ops)
        if fed and not ops and cwd is not None:  # `... | xargs rm`: names come from stdin, relative to cwd
            targets.append(Target(DELETE, os.path.join(cwd, "__stdin__"), verb))
        elif fed and not ops:
            targets.append(Target(DELETE, _UNRESOLVED, verb))
    elif verb == "mv":
        ops = _operands(args, {"-t", "--target-directory", "-S", "--suffix"})
        dest = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-t", "--target-directory")), None)
        sources = ops if dest is not None else ops[:-1]
        add(DELETE, sources)
        if dest is not None or len(ops) >= 2:
            _copy_dest(targets, verb, sources, dest if dest is not None else ops[-1], cwd)
    elif verb == "truncate":
        add(OVERWRITE, _operands(args, {"-s", "--size", "-r", "--reference"}))
    elif verb == "find":
        starts = []
        for arg in args:
            if arg.startswith(("-", "(", "!")):
                break
            starts.append(arg)
        joined = " ".join(args)
        if "-delete" in args or re.search(r"-(?:exec|execdir|ok|okdir)\s+(?:\S*/)?(?:rm|shred|unlink)\b", joined):
            add(DELETE, starts or ["."])
    elif verb == "rsync":
        ops = _operands(args, {"-e", "--rsh", "--exclude", "--include", "--filter", "-f"})
        if ops:
            deleting = any(a.startswith(("--delete", "--remove-source-files")) for a in args)
            add(DELETE if deleting else "auto", ops[-1:])
            if any(a.startswith("--remove-source-files") for a in args):
                add(DELETE, ops[:-1])
    elif verb in _COPY_VERBS:
        ops = _operands(args, {"-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "--owner",
                               "-g", "--group"})
        dest = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-t", "--target-directory")), None)
        if dest is not None:
            _copy_dest(targets, verb, ops, dest, cwd)
        elif len(ops) >= 2:
            _copy_dest(targets, verb, ops[:-1], ops[-1], cwd)
    elif verb == "tee":
        add("auto", _operands(args))
    elif verb == "sed" and any(a == "-i" or a.startswith(("-i", "--in-place")) for a in args):
        ops = _operands(args, {"-e", "-f", "--expression", "--file", "-l"})
        has_script_opt = any(a in ("-e", "-f", "--expression", "--file") for a in args)
        add(OVERWRITE, ops if has_script_opt else ops[1:])
    elif verb in _METADATA_VERBS:
        ops = _operands(args, {"-h"} if verb != "xattr" else {"-w", "-d"})
        add(OVERWRITE, ops if verb in {"xattr"} else ops[1:])
    elif verb in _CREATE_VERBS:
        add(WRITE, _operands(args, {"-m", "--mode", "-d", "-r", "-t"}))
    elif verb == "dd":
        add(OVERWRITE, [a[3:] for a in args if a.startswith("of=")])
    return targets


def terminal_targets(command: str, cwd: Optional[str]) -> List[Target]:
    """Every file-system target the command destroys or writes, across all variants."""
    found: List[Target] = []
    for index, variant in enumerate(command_variants(command)):
        _fold.active = index > 0
        try:
            found.extend(_variant_targets(variant, cwd))
        finally:
            _fold.active = False
    return list(dict.fromkeys(found))


def _variant_targets(variant: str, cwd: Optional[str]) -> List[Target]:
    found: List[Target] = []
    tokens = _tokens(variant)
    local_cwd = cwd
    segments = _segments(tokens)
    for index, (words, separator) in enumerate(segments):
        # Redirections belong to the segment; collect and drop them from the argv.
        argv: List[str] = []
        i = 0
        while i < len(words):
            word = words[i]
            if word in _REDIRECTS and i + 1 < len(words):
                target = words[i + 1]
                if not target.startswith("&") and target not in _DEV_ALLOWED and not target.startswith("/dev/fd/"):
                    for path in _resolve(target, local_cwd):
                        found.append(Target(_exists_kind(path), path, "redirect"))
                i += 2
                continue
            if word in {"<", "<<", "<<<", ">&", "<&"}:
                i += 2
                continue
            argv.append(word)
            i += 1
        argv, fed = _strip_wrappers(argv)
        if not argv:
            continue
        verb, args = os.path.basename(argv[0]), argv[1:]
        if verb in {"cd", "pushd"}:
            dest = _operands(args)
            local_cwd = _resolve(dest[0], local_cwd)[0] if dest else os.path.realpath(os.path.expanduser("~"))
            continue
        found.extend(_segment_targets(verb, args, local_cwd, fed))
        # `find X | xargs rm`: the find start paths are what gets deleted.
        if verb == "find" and separator == "|" and index + 1 < len(segments):
            nxt, nxt_fed = _strip_wrappers(list(segments[index + 1][0]))
            if nxt_fed and nxt and os.path.basename(nxt[0]) in _DELETE_VERBS | {"mv"}:
                starts = []
                for arg in args:
                    if arg.startswith(("-", "(", "!")):
                        break
                    starts.append(arg)
                for raw in starts or ["."]:
                    for path in _resolve(raw, local_cwd):
                        found.append(Target(DELETE, path, os.path.basename(nxt[0])))
    return list(dict.fromkeys(found))


def _inside(path: str, root: Optional[str]) -> bool:
    if not root:
        return False
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def zone(path: Optional[str], settings: Settings) -> Tuple[str, Optional[str]]:
    """("workspace" | "store" | "root" | "scratch" | "outside" | "unknown", matching root)."""
    if path is None:
        return "unknown", None
    for store in (settings.versions, settings.trash):
        if store and _inside(path, os.path.realpath(store)):
            return "store", store
    if settings.workspace and _inside(path, settings.workspace):
        return "workspace", settings.workspace
    for root in settings.write_roots:
        if _inside(path, root):
            return "root", root
    for scratch in settings.scratch_roots:  # tier 1 inside, but never the scratch root itself
        if _inside(path, scratch) and path != scratch:
            return "scratch", scratch
    if path in _DEV_ALLOWED:
        return "scratch", None
    return "outside", None


def _action(target: Target) -> str:
    what = {DELETE: "deleting", OVERWRITE: "overwriting", WRITE: "writing"}[target.kind]
    return f"{what} {target.path}" if target.path else f"{what} a path Iollo could not resolve"


def tier4_message(action: str) -> str:
    return f"Stopped before {action}: Iollo never does this."


def root_pointer_message(path: str, root: str) -> str:
    return (f"Stopped before changing {path} from the terminal: files in {root} are changed only with "
            "write_file or patch (so every change is versioned) and removed with files_trash.")


def check_terminal(command: str, cwd: Optional[str], settings: Settings) -> Optional[str]:
    """The block message for a terminal command, or None when nothing is hard-blocked."""
    action = match_tier4_command(command, settings)
    if action:
        return tier4_message(action)
    pointer: Optional[str] = None
    for target in terminal_targets(command, cwd):
        where, root = zone(target.path, settings)
        if where == "workspace":
            if target.kind == DELETE and target.path == settings.workspace:
                return tier4_message(_action(target))
            continue
        if where == "scratch":
            continue
        if where == "store":
            if target.kind != WRITE:
                return tier4_message(_action(target))
            continue
        if where == "root":
            pointer = pointer or root_pointer_message(target.path or "", root or "")
            continue
        if where == "unknown":
            if target.kind == DELETE:
                return tier4_message(_action(target))
            continue
        if target.kind in (DELETE, OVERWRITE):
            return tier4_message(_action(target))
    return pointer
