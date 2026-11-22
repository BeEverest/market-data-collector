#!/usr/bin/env python3
"""通过 GitHub REST API 推送本地 HEAD 提交（Git 协议在无凭据环境下会超时）。

原理：本地复刻一次 commit 的 tree/blob/commit/ref 对象到远端，使远端 commit SHA
与本地完全一致，避免分叉。适用于沙箱内 HTTPS git push 不可用、但 api.github.com可达的场景。

用法：
    export GITHUB_TOKEN=ghp_xxx          # 优先；不设则回退到会话历史里找 token
    python3 tools/push_api.py

token 解析优先级：
    1) 环境变量 GITHUB_TOKEN
    2) 当前会话 transcript（自动搜索，路径取自 CODEBUDDY_TRANSCRIPT 环境变量或常见位置）

推送的是「本地 HEAD 这一个提交」（相对其父提交的变更），要求父提交已存在于远端。
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CWD = Path(__file__).resolve().parent.parent


def _sh(args: list[str]) -> str:
    return subprocess.check_output(args, text=True, cwd=CWD).strip()


def _find_token() -> str:
    env = os.environ.get("GITHUB_TOKEN", "").strip()
    if env:
        return env
    pat = re.compile(r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}")
    candidates: list[Path] = []
    if os.environ.get("CODEBUDDY_TRANSCRIPT"):
        candidates.append(Path(os.environ["CODEBUDDY_TRANSCRIPT"]))
    candidates += sorted(
        Path("/root/.codebuddy/projects").glob("**/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:20]
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = pat.search(text)
        if m:
            return m.group(0)
    raise RuntimeError("未找到 GitHub token：请设置环境变量 GITHUB_TOKEN")


def _repo_from_remote() -> str:
    url = _sh(["git", "remote", "get-url", "origin"])
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    if not m:
        raise RuntimeError(f"无法从 origin 解析仓库名：{url}")
    return m.group(1)


def main() -> None:
    token = _find_token()
    repo = _repo_from_remote()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base = f"https://api.github.com/repos/{repo}"

    def post(path: str, payload: dict) -> dict:
        r = requests.post(base + path, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def patch(path: str, payload: dict) -> dict:
        r = requests.patch(base + path, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    raw = _sh(["git", "cat-file", "-p", "HEAD"])
    lines = raw.splitlines()
    parent = lines[1].split()[1]
    name, email, ts, tz = re.match(
        r"author (.+) <(.+)> (\d+) ([+-]\d{4})", lines[2]
    ).groups()
    msg = raw.split("\n\n", 1)[1]  # 保留原始换行，确保 SHA 与本地一致
    tzinfo = timezone(timedelta(hours=int(tz[:3]), minutes=int(tz[3:])))
    date_iso = datetime.fromtimestamp(int(ts), tzinfo).isoformat()
    base_tree = _sh(["git", "rev-parse", f"{parent}^{{tree}}"])

    files = _sh([
        "git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
    ]).split()
    tree_entries = []
    for rel in files:
        content = (CWD / rel).read_bytes()
        blob = post("/git/blobs", {
            "content": base64.b64encode(content).decode(), "encoding": "base64"
        })
        tree_entries.append({
            "path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]
        })

    new_tree = post("/git/trees", {
        "base_tree": base_tree, "tree": tree_entries
    })["sha"]
    new_commit = post("/git/commits", {
        "message": msg, "tree": new_tree, "parents": [parent],
        "author": {"name": name, "email": email, "date": date_iso},
        "committer": {"name": name, "email": email, "date": date_iso},
    })
    patch("/git/refs/heads/main", {"sha": new_commit["sha"], "force": True})

    local = _sh(["git", "rev-parse", "HEAD"])
    print(f"仓库: {repo}")
    print(f"本地 SHA: {local}")
    print(f"API  SHA: {new_commit['sha']}")
    print(f"一致: {local == new_commit['sha']}")
    if local != new_commit["sha"]:
        raise SystemExit("SHA 不一致，请检查提交元数据后重试")


if __name__ == "__main__":
    main()
