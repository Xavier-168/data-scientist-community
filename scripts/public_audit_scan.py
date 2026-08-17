#!/usr/bin/env python3
"""Redacted secret/privacy scanner for source trees and Git history.

The scanner never prints matched values. Findings contain only a rule name,
severity, file, line number, and a short one-way fingerprint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator


DEFAULT_SKIP_DIRS = {
    ".auth",
    ".claude",
    ".git",
    ".playwright-browsers",
    ".pytest_cache",
    ".runtime-cache",
    ".signing-keys",
    ".venv",
    ".worktrees",
    "__pycache__",
    # 本地构建产物与运行状态（均被 .gitignore 排除，不进入发布源码树）
    "build",
    "dist",
    "downloads",
    "node_modules",
    "resources",
    "target",
    "updates",
}
FORBIDDEN_SUFFIXES = {
    ".app",
    ".cer",
    ".dmg",
    ".mobileprovision",
    ".p12",
    ".pfx",
    ".pem",
    ".pyc",
    ".pyo",
    ".so",
}
TEXT_SKIP_NAMES = {
    ".community-staging.json",
    ".public-export.json",
    "LICENSE",
    "Cargo.lock",
    "EXPORT_PROVENANCE.json",
    "PUBLIC_EXPORT_MANIFEST.json",
    "package-lock.json",
    "SBOM.spdx.json",
}
SAFE_EMAILS = {
    "licensing@fsf.org",
    "copyright@fsf.org",
}
PLACEHOLDER_WORDS = {
    "change-me",
    "demo",
    "example",
    "placeholder",
    "redacted",
    "replace-me",
    "test",
    "your-app-id",
    "your-app-secret",
    "your-token",
}


@dataclass(frozen=True)
class Finding:
    severity: str
    rule: str
    path: str
    line: int
    fingerprint: str
    source: str


def _fingerprint(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:12]


def _is_probably_binary(raw: bytes) -> bool:
    if b"\x00" in raw[:8192]:
        return True
    sample = raw[:8192]
    if not sample:
        return False
    control = sum(byte < 9 or 13 < byte < 32 for byte in sample)
    return control / len(sample) > 0.08


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {char: value.count(char) for char in set(value)}
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _safe_placeholder(value: str) -> bool:
    lowered = value.strip().strip("<>{}[]()'\"").lower()
    return not lowered or any(word in lowered for word in PLACEHOLDER_WORDS)


def _safe_public_literal(value: str, line: str) -> bool:
    """Suppress values that are public integrity material or obvious repo paths."""
    # "signature": "..." 与 *signature = "..." 都是公开完整性材料（签名/校验和
    # 本就随清单分发），不是机密
    if re.search(r"(?i)[\"']?signature[\"']?\s*[:=]", line):
        return True
    if "/" not in value:
        return False
    suffix = Path(value).suffix.lower()
    return suffix in {
        ".cfg",
        ".dylib",
        ".html",
        ".js",
        ".json",
        ".mjs",
        ".py",
        ".toml",
        ".ts",
        ".tsx",
    }


def _content_rules(private_domains: list[str]) -> list[tuple[str, str, re.Pattern[str]]]:
    rules: list[tuple[str, str, re.Pattern[str]]] = [
        ("critical", "private_key", re.compile(re.escape("BEGIN " + "PRIVATE KEY"))),
        ("critical", "provider_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,})\b")),
        ("critical", "aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
        ("high", "authorization_bearer", re.compile(r"(?i)Authorization\s*[:=]\s*['\"]?Bearer\s+[A-Za-z0-9._~+/-]{12,}")),
        ("high", "generic_secret", re.compile(r"(?i)(?:api[_-]?key|app[_-]?secret|client[_-]?secret|admin[_-]?secret|access[_-]?token|refresh[_-]?token|password)\s*[:=]\s*['\"]([^'\"\s]{8,})")),
        ("high", "feishu_app_id", re.compile(r"\bcli_[A-Za-z0-9]{10,}\b")),
        ("high", "absolute_macos_user_path", re.compile(re.escape("/Users" + "/") + r"[^/\s'\"`]+/")),
        ("high", "absolute_windows_user_path", re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s'\"`]+\\")),
        ("medium", "email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
        ("medium", "mainland_phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ]
    for domain in private_domains:
        normalized = domain.strip().lower()
        if normalized:
            rules.append(("high", "private_domain", re.compile(re.escape(normalized), re.I)))
    return rules


def _scan_text(text: str, path: str, source: str, private_domains: list[str]) -> Iterator[Finding]:
    rules = _content_rules(private_domains)
    metadata_file = Path(path).name in TEXT_SKIP_NAMES
    skip_entropy = metadata_file or path.endswith("_public_keys.json")
    for line_number, line in enumerate(text.splitlines(), start=1):
        for severity, rule, pattern in rules:
            if metadata_file and rule in {"email", "feishu_app_id", "generic_secret", "mainland_phone"}:
                continue
            for match in pattern.finditer(line):
                value = match.group(1) if match.lastindex else match.group(0)
                if rule == "email" and value.lower() in SAFE_EMAILS:
                    continue
                if rule in {"generic_secret", "feishu_app_id", "authorization_bearer"} and _safe_placeholder(value):
                    continue
                yield Finding(severity, rule, path, line_number, _fingerprint(value), source)
        if skip_entropy:
            continue
        for match in re.finditer(r"['\"]([A-Za-z0-9_+=/.-]{28,})['\"]", line):
            value = match.group(1)
            if _safe_placeholder(value) or _safe_public_literal(value, line) or value.startswith(("http://", "https://")):
                continue
            if _entropy(value) >= 4.35:
                yield Finding("medium", "high_entropy_literal", path, line_number, _fingerprint(value), source)


def _allowed_binary_map(values: list[str]) -> dict[str, str]:
    allowed: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise ValueError("--allowed-binary must use path=sha256")
        path, digest = raw.split("=", 1)
        normalized = path.strip().replace(os.sep, "/")
        digest = digest.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"invalid SHA-256 for allowed binary: {normalized}")
        allowed[normalized] = digest
    return allowed


def _manifest_allowed_binaries(root: Path) -> dict[str, str]:
    """Load the candidate's audited binary allowlist when present."""
    marker = root / "COMMUNITY_EDITION"
    manifest_path = root / "PUBLIC_EXPORT_MANIFEST.json"
    if not marker.is_file() or not manifest_path.is_file():
        return {}
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    values = []
    for item in payload.get("approved_binary_assets", []):
        if not isinstance(item, dict):
            raise ValueError("approved_binary_assets entries must be objects")
        values.append(f"{item.get('destination', '')}={item.get('sha256', '')}")
    return _allowed_binary_map(values)


def _iter_tree_files(root: Path) -> Iterator[Path]:
    for current_root, dirs, files in os.walk(root):
        dirs[:] = sorted(item for item in dirs if item not in DEFAULT_SKIP_DIRS)
        for name in sorted(files):
            yield Path(current_root) / name


def scan_tree(root: Path, private_domains: list[str], allowed_binaries: dict[str, str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_tree_files(root):
        relative = path.relative_to(root).as_posix()
        suffix = path.suffix.lower()
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        binary = _is_probably_binary(raw)
        if suffix in FORBIDDEN_SUFFIXES:
            findings.append(Finding("critical", "forbidden_file_type", relative, 0, digest[:12], "tree"))
            continue
        if binary:
            expected = allowed_binaries.get(relative)
            if expected != digest:
                findings.append(Finding("high", "unapproved_binary", relative, 0, digest[:12], "tree"))
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(Finding("high", "non_utf8_file", relative, 0, digest[:12], "tree"))
            continue
        findings.extend(_scan_text(text, relative, "tree", private_domains))
    return findings


def _git_blob_records(repo: Path) -> Iterator[tuple[str, int, str]]:
    objects = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    checked = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize) %(rest)"],
        cwd=repo,
        check=True,
        input=objects,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    for line in checked.splitlines():
        parts = line.split(" ", 3)
        if len(parts) < 3 or parts[1] != "blob":
            continue
        oid, _kind, size_text = parts[:3]
        path = parts[3] if len(parts) == 4 and parts[3] else f"blob:{oid[:12]}"
        yield oid, int(size_text), path


def scan_git_history(
    repo: Path,
    private_domains: list[str],
    allowed_binaries: dict[str, str],
) -> list[Finding]:
    findings: list[Finding] = []
    for oid, size, path in _git_blob_records(repo):
        normalized_path = path.replace(os.sep, "/")
        suffix = Path(path).suffix.lower()
        if suffix in FORBIDDEN_SUFFIXES:
            findings.append(Finding("critical", "history_forbidden_file_type", path, 0, oid[:12], "git-history"))
            continue
        if size > 2 * 1024 * 1024:
            expected = allowed_binaries.get(normalized_path)
            if expected:
                raw = subprocess.run(
                    ["git", "cat-file", "blob", oid],
                    cwd=repo,
                    check=True,
                    stdout=subprocess.PIPE,
                ).stdout
                if hashlib.sha256(raw).hexdigest() == expected:
                    continue
            findings.append(Finding("medium", "history_large_blob", path, 0, oid[:12], "git-history"))
            continue
        raw = subprocess.run(
            ["git", "cat-file", "blob", oid],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        if _is_probably_binary(raw):
            digest = hashlib.sha256(raw).hexdigest()
            if allowed_binaries.get(normalized_path) == digest:
                continue
            findings.append(Finding("medium", "history_binary_blob", path, 0, oid[:12], "git-history"))
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(_scan_text(text, path, "git-history", private_domains))
    author_emails = subprocess.run(
        ["git", "log", "--all", "--format=%H%x09%ae"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    for row in author_emails.splitlines():
        commit, _, email = row.partition("\t")
        if email and not email.endswith("@users.noreply.github.com"):
            findings.append(Finding("high", "git_author_email", f"commit:{commit[:12]}", 0, _fingerprint(email), "git-history"))
    return findings


def _dedupe(findings: Iterable[Finding]) -> list[Finding]:
    unique = {
        (item.severity, item.rule, item.path, item.line, item.fingerprint, item.source): item
        for item in findings
    }
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(unique.values(), key=lambda item: (order.get(item.severity, 9), item.path, item.line, item.rule))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--git-history", action="store_true")
    parser.add_argument("--forbid-domain", action="append", default=[])
    parser.add_argument("--allowed-binary", action="append", default=[])
    parser.add_argument("--json-output")
    parser.add_argument("--fail-on", choices=("critical", "high", "medium", "low", "never"), default="high")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    allowed_binaries = _manifest_allowed_binaries(root)
    allowed_binaries.update(_allowed_binary_map(args.allowed_binary))
    findings = scan_tree(root, args.forbid_domain, allowed_binaries)
    if args.git_history:
        findings.extend(scan_git_history(root, args.forbid_domain, allowed_binaries))
    findings = _dedupe(findings)

    payload = {
        "schema_version": 1,
        "root": str(root),
        "git_history_scanned": bool(args.git_history),
        "counts": {
            severity: sum(item.severity == severity for item in findings)
            for severity in ("critical", "high", "medium", "low")
        },
        "findings": [asdict(item) for item in findings],
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.json_output:
        Path(args.json_output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)

    if args.fail_on == "never":
        return 0
    threshold = {"critical": 0, "high": 1, "medium": 2, "low": 3}[args.fail_on]
    return 1 if any({"critical": 0, "high": 1, "medium": 2, "low": 3}[item.severity] <= threshold for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
