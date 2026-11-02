"""`linx ask`: plain English in, one LinX command out, run only after you confirm.

Two interpreters, tried in order:
1. Rules: common phrasings map straight to commands. Instant, offline, no model needed.
2. A local model (optional): any OpenAI-compatible chat endpoint, e.g. Ollama on this laptop
   (`LINX_LLM_URL=http://127.0.0.1:11434/v1`, `LINX_LLM_MODEL=qwen2.5:3b`). It is shown the command
   list and must answer with JSON `{"argv": [...], "explain": "..."}`.

Whatever the source, the result must be a LinX command on the allowlist that LinX's own argument
parser accepts. It never reaches a shell: the argv goes straight to `linx.cli.main`. Owner actions
(approve, reject, halt, resume, retry, memory put) need you to type `yes` in full.
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

# First argv token -> may it change state that only the owner should change?
ALLOWED: dict[str, bool] = {
    "status": False,
    "systems": False,
    "doctor": False,
    "selftest": False,
    "pending": False,
    "unisync": False,
    "notify": False,
    "memory": False,
    "nightly": False,
    "approve": True,
    "reject": True,
    "halt": True,
    "resume": True,
}
OWNER_SUB = {("nightly", "retry"), ("memory", "put"), ("memory", "consolidate")}
# Commands that read or write the ledger take `--ledger` here (after the command, before any subcommand).
LEDGER_CMDS = {"status", "memory", "nightly", "halt", "resume", "pending", "approve", "reject", "notify"}

EXAMPLES = [
    ("what's going on", ["status"]),
    ("what's waiting for me", ["pending"]),
    ("approve the o0 audit", ["approve", "<task id>"]),
    ("reject T2 because it's too risky", ["reject", "T2", "--reason", "it's too risky"]),
    ("stop everything, the build is broken", ["halt", "--reason", "the build is broken"]),
    ("start again", ["resume"]),
    ("run the nightly", ["nightly", "run"]),
    ("show this morning's report", ["nightly", "digest"]),
    ("retry sebastian B59", ["nightly", "retry", "sebastian", "B59"]),
    ("send the report to my phone", ["notify"]),
    ("check everything works", ["selftest"]),
    ("any memory conflicts?", ["memory", "check"]),
    ("what is build_cmd", ["memory", "get", "build_cmd"]),
    ("remember goal is ship linx", ["memory", "put", "goal", "ship linx", "--as", "jonny"]),
]


@dataclass(frozen=True)
class Plan:
    argv: list[str]
    explain: str
    source: str  # "rules" or "model"

    @property
    def owner_action(self) -> bool:
        if not self.argv:
            return False
        sub = self.argv[1] if len(self.argv) > 1 else ""
        return ALLOWED.get(self.argv[0], False) or (self.argv[0], sub) in OWNER_SUB


class AskError(ValueError):
    """The request could not be turned into an allowed LinX command."""


# ── rules ────────────────────────────────────────────────────────────────────


def _reason(text: str, default: str) -> str:
    m = re.search(r"(?:because|since|reason:?|,)\s+(.+)$", text, re.I)
    return m.group(1).strip().rstrip(".") if m else default


def _task(text: str, pending: Sequence[tuple[str, str, str]]) -> str | None:
    """A task id named outright, or the one pending task whose title/requester best matches the words."""
    for tid, _title, _who in pending:
        if tid.lower() in text.lower():
            return tid
    m = re.search(r"\b(T\d+|[A-Za-z][\w-]*/[\w./-]+)\b", text)  # explicit ids only: T12, nightly/...
    words = {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2} - {
        "approve",
        "reject",
        "the",
        "task",
        "please",
        "because",
        "that",
        "this",
        "too",
        "risky",
    }
    scored = []
    for tid, title, who in pending:
        hay = set(re.findall(r"[a-z0-9]+", f"{tid} {title} {who}".lower()))
        scored.append((len(words & hay), tid))
    scored.sort(reverse=True)
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return m.group(1) if m and not pending else None


RuleFn = Callable[[re.Match[str], str, Sequence[tuple[str, str, str]]], list[str] | None]
RULES: list[tuple[re.Pattern[str], RuleFn, str]] = []


def rule(pattern: str, explain: str) -> Callable[[RuleFn], RuleFn]:
    def wrap(fn: RuleFn) -> RuleFn:
        RULES.append((re.compile(pattern, re.I), fn, explain))
        return fn

    return wrap


@rule(r"\b(approve|ok|accept)\b", "approve a pending task")
def _approve(m: re.Match[str], t: str, pending: Sequence[tuple[str, str, str]]) -> list[str] | None:
    tid = _task(t, pending)
    return ["approve", tid] if tid else None


@rule(r"\b(reject|deny|decline|refuse)\b", "reject a pending task")
def _reject(m: re.Match[str], t: str, pending: Sequence[tuple[str, str, str]]) -> list[str] | None:
    tid = _task(t, pending)
    return ["reject", tid, "--reason", _reason(t, "rejected via linx ask")] if tid else None


@rule(r"\b(halt|stop|freeze|kill switch|pause)\b", "halt LinX (kill switch)")
def _halt(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["halt", "--reason", _reason(t, "halted via linx ask")]


@rule(r"\b(resume|unfreeze|unpause|start (again|back up)|carry on)\b", "lift a halt")
def _resume(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["resume"]


@rule(r"\bretry\s+([\w-]+)\s+([A-Za-z]+[\w-]*\d[\w-]*)", "retry a parked nightly item")
def _retry(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["nightly", "retry", m.group(1).lower(), m.group(2)]


@rule(r"\b(phone|notify|push)\b", "push the digest to your phone")
def _notify(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["notify"]


@rule(r"\b(digest|morning report|report|what happened (last night|overnight))\b", "show the nightly digest")
def _digest(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["nightly", "digest"]


@rule(r"\bplan\b.*\b(tonight|nightly)\b", "post tonight's backlog tasks")
def _plan(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["nightly", "plan"]


@rule(r"\b(run|start|kick off|do)\b.*\bnightly\b|\bnightly\b.*\b(now|run)\b", "run the nightly loop now")
def _nightly(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["nightly", "run"]


@rule(r"\bremember\s+([\w./-]+)\s+(?:is|=|as)\s+(.+)$", "write a fact to shared memory")
def _remember(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["memory", "put", m.group(1), m.group(2).strip().rstrip("."), "--as", "jonny"]


@rule(r"\bwhat(?:'s| is)\s+(?:the\s+)?([\w]+[/_.][\w./-]+|[\w]+_[\w]+)\b", "read a fact from shared memory")
def _get(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["memory", "get", m.group(1)]


@rule(r"\b(memory|conflict|conflicts|stale)\b", "check shared memory")
def _memcheck(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["memory", "check"]


@rule(r"\b(waiting|pending|approvals?|needs? me|to approve)\b", "list tasks waiting for you")
def _pending(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["pending"]


@rule(r"\b(self ?test|check (everything|it all|the setup)|does it work|is it working)\b", "run the selftest")
def _selftest(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["selftest"]


@rule(r"\b(doctor|checkouts?|which repos|repos (present|missing))\b", "check repo checkouts")
def _doctor(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["doctor"]


@rule(r"\b(list|show)\b.*\bsystems\b|\bsystems\b", "list all systems")
def _systems(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["systems"]


@rule(r"\b(status|going on|overview|how are things|what's up|dashboard|health)\b", "show fleet status")
def _status(m: re.Match[str], t: str, p: Sequence[tuple[str, str, str]]) -> list[str] | None:
    return ["status"]


def from_rules(text: str, pending: Sequence[tuple[str, str, str]] = ()) -> Plan | None:
    t = text.strip()
    for pattern, fn, explain in RULES:
        m = pattern.search(t)
        if m:
            argv = fn(m, t, pending)
            if argv:
                return Plan(argv, explain, "rules")
    return None


# ── local model ──────────────────────────────────────────────────────────────


def _system_prompt(pending: Sequence[tuple[str, str, str]]) -> str:
    lines = [
        "You turn the user's request into exactly one LinX command for Jonny's agent system.",
        'Answer with JSON only: {"argv": ["<command>", "<arg>", ...], "explain": "<one short sentence>"}.',
        'If nothing fits, answer {"argv": [], "explain": "<why>"}. Never invent other commands.',
        "Allowed commands (argv after `linx`):",
        "  status | systems | doctor | selftest | pending | unisync | notify",
        "  approve TASK [--note TEXT] | reject TASK --reason TEXT | halt --reason TEXT | resume",
        "  nightly run|plan|digest | nightly work --as AGENT | nightly retry SYSTEM ITEM",
        "  memory check | memory get SCOPE/KEY | memory history SCOPE/KEY | memory put KEY VALUE --as jonny",
        "Examples:",
    ]
    lines += [f'  "{q}" -> {json.dumps(a)}' for q, a in EXAMPLES]
    if pending:
        lines.append("Tasks waiting for approval: " + "; ".join(f"{t} ({title})" for t, title, _ in pending))
    return "\n".join(lines)


Opener = Callable[..., Any]


def from_model(
    text: str,
    url: str,
    model: str,
    pending: Sequence[tuple[str, str, str]] = (),
    opener: Opener | None = None,
    timeout: float = 60.0,
) -> Plan:
    from linx.live import check_url

    endpoint = check_url(url) + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _system_prompt(pending)},
            {"role": "user", "content": text},
        ],
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
        reply = json.loads(resp.read())
    content = str(reply["choices"][0]["message"]["content"])
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise AskError(f"the model did not answer with JSON: {content[:200]!r}")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise AskError(f"the model's JSON is invalid: {exc}") from exc
    argv = data.get("argv")
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise AskError("the model's argv is not a list of strings")
    if not argv:
        raise AskError(f"no matching command: {data.get('explain', '')}")
    return Plan(argv, str(data.get("explain", "")), "model")


# ── validation and the whole flow ────────────────────────────────────────────


def validate(argv: Sequence[str], parse: Callable[[list[str]], Any]) -> None:
    """Allowlisted first token, no recursion, and LinX's own parser must accept it."""
    if not argv or argv[0] not in ALLOWED:
        raise AskError(f"not an allowed LinX command: {' '.join(argv) or '(empty)'}")
    if any(a.startswith("--registry") for a in argv):
        raise AskError("--registry is not allowed from linx ask")
    try:
        parse(list(argv))
    except SystemExit as exc:
        raise AskError(f"LinX would not accept: linx {' '.join(argv)}") from exc


def with_ledger(argv: list[str], ledger: str | None) -> list[str]:
    if not ledger or argv[0] not in LEDGER_CMDS or "--ledger" in argv:
        return argv
    return [argv[0], "--ledger", ledger, *argv[1:]]


def interpret(
    text: str,
    *,
    parse: Callable[[list[str]], Any],
    pending: Sequence[tuple[str, str, str]] = (),
    llm_url: str = "",
    llm_model: str = "",
    opener: Opener | None = None,
) -> Plan:
    plan = from_rules(text, pending)
    if plan is None:
        if not llm_url:
            raise AskError(
                "I don't know that one yet. Try e.g. "
                + ", ".join(f'"{q}"' for q, _ in EXAMPLES[:6])
                + ". (Set LINX_LLM_URL to a local model to understand more.)"
            )
        plan = from_model(text, llm_url, llm_model or "qwen2.5:3b", pending, opener)
    validate(plan.argv, parse)
    return plan
