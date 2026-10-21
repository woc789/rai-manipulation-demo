"""Read-only heuristic release audit. Never prints matched credential values."""

import argparse
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "model_key": re.compile(rb"\bsk-[A-Za-z0-9_-]{24,}\b"),
    "github_token": re.compile(
        rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"
    ),
    "aws_access_id": re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
    "private_key": re.compile(rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
}


def scan_text(data):
    if b"\0" in data:
        return []
    return [name for name, pattern in PATTERNS.items() if pattern.search(data)]


def private_path(path):
    name = Path(path).name.lower()
    return (
        name == ".env"
        or (name.endswith(".env") and not name.endswith(".example"))
        or name.endswith((".sqlite", ".sqlite3", ".db", ".pem", ".key", ".pth", ".pt"))
    )


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def audit(root, history=False):
    result = dict(
        files_checked=0, history_blobs_checked=0, findings=[], path_warnings=[]
    )

    def inspect(location, data, size):
        if size > 100 * 1024 * 1024:
            result["findings"].append(dict(location=location, kind="oversize_git_blob"))
        for kind in scan_text(data):
            result["findings"].append(dict(location=location, kind=kind))

    names = set(
        git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        .decode()
        .split("\0")
    )
    for name in sorted(names - {""}):
        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        result["files_checked"] += 1
        if private_path(name):
            result["findings"].append(
                dict(location=name, kind="private_or_runtime_file")
            )
        size = path.stat().st_size
        data = path.read_bytes() if size <= 100 * 1024 * 1024 else b""
        inspect(name, data, size)
        if path.suffix in {".sh", ".py", ".yaml"} and re.search(
            rb"/home/[A-Za-z0-9_-]+/", data
        ):
            result["path_warnings"].append(name)
    if history:
        objects = git(root, "rev-list", "--objects", "--all").decode().splitlines()
        process = subprocess.Popen(
            ["git", "-C", str(root), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        try:
            for entry in objects:
                oid, _, name = entry.partition(" ")
                process.stdin.write((oid + "\n").encode())
                process.stdin.flush()
                header = process.stdout.readline().split()
                size = int(header[2])
                data = process.stdout.read(size)
                process.stdout.read(1)
                if header[1] != b"blob":
                    continue
                result["history_blobs_checked"] += 1
                inspect(f"history:{oid[:12]}:{name}", data, size)
                if private_path(name):
                    result["findings"].append(
                        dict(
                            location=f"history:{oid[:12]}:{name}",
                            kind="historical_private_or_runtime_file",
                        )
                    )
        finally:
            process.stdin.close()
            process.stdout.close()
            process.wait(timeout=10)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional redacted JSON report; use ignored release-artifacts/",
    )
    args = parser.parse_args()
    result = audit(ROOT, args.history)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("启发式检查不保证识别所有秘密；发现真实凭据先轮换，不自动改写 Git 历史。")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return int(bool(result["findings"]))


if __name__ == "__main__":
    raise SystemExit(main())
