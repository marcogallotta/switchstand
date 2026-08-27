"""Reject deterministic skip, focus, and retry escape hatches in tests."""
from __future__ import annotations

import ast
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
PYTHON_FORBIDDEN = {"skip", "skipIf", "skipUnless", "skipTest", "expectedFailure"}
JS_DISABLED = re.compile(r"\b(?:test|it|describe)\s*\.\s*(only|skip|todo|fixme)\s*\(")
RETRIES = re.compile(r"\bretries\s*:\s*([^,}\n]+)")
RERUN_FLAGS = re.compile(r"--(?:reruns?|retries)(?:\s|=)")


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def python_findings(path: Path, text: str) -> list[str]:
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: invalid Python test syntax"]
    findings = []
    for node in ast.walk(tree):
        target = node.func if isinstance(node, ast.Call) else node
        name = _call_name(target)
        if name in PYTHON_FORBIDDEN:
            findings.append(f"{path}:{getattr(node, 'lineno', 1)}: prohibited Python {name}")
    return findings


def text_findings(path: Path, text: str) -> list[str]:
    findings = [
        f"{path}:{text.count(chr(10), 0, match.start()) + 1}: prohibited JavaScript {match.group(1)}"
        for match in JS_DISABLED.finditer(text)
    ]
    findings.extend(
        f"{path}:{text.count(chr(10), 0, match.start()) + 1}: retries must be zero"
        for match in RETRIES.finditer(text)
        if match.group(1).strip() != "0"
    )
    findings.extend(
        f"{path}:{text.count(chr(10), 0, match.start()) + 1}: rerun/retry option is prohibited"
        for match in RERUN_FLAGS.finditer(text)
    )
    return findings


def scan(paths: list[Path]) -> list[str]:
    findings = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            findings.extend(python_findings(path, text))
        else:
            findings.extend(text_findings(path, text))
    return findings


def default_paths() -> list[Path]:
    tests = [path for path in (ROOT / "tests").rglob("*") if path.suffix in {".py", ".js"}]
    controls = [ROOT / "playwright.config.js", ROOT / "package.json"]
    controls.extend((ROOT / ".github" / "workflows").glob("*.yml"))
    return sorted([*tests, *controls])


def main() -> int:
    findings = scan(default_paths())
    for finding in findings:
        print(f"ERROR: {finding}", file=sys.stderr)
    return bool(findings)


if __name__ == "__main__":
    raise SystemExit(main())
