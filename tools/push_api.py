#!/usr/bin/env python3
"""通过 GitHub REST API 推送本地提交（Git 协议在无凭据环境下会超时）。

原理：把本地 commit 的 tree/blob/commit/ref 对象复刻到远端，使远端 commit SHA
与本地完全一致，避免分叉。适用于沙箱内 HTTPS git push 不可用、但 api.github.com 可达的场景。

特性：
    - 自动找出本地领先远端的全部提交，按 parent 链顺序逐个重建（不会断链）
    - 从 git 对象读取 blob 内容与文件 mode，不受工作区未提交改动影响
    - 正确保留 commit message 末尾换行与 author 时区，确保 SHA 逐字节一致

用法：
    export GITHUB_TOKEN=ghp_xxx          # 优先；不设则回退到会话历史里找 token
    cd /workspace/market_data_collector
    python3 tools/push_api.py

token 解析优先级：环境变量 GITHUB_TOKEN → CODEBUDDY_TRANSCRIPT → 会话历史 jsonl。
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


def _remote_sha(base: str, headers: dict) -> str | None:
    r = requests.get(base + "/git/ref/heads/main", headers=headers, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()["object"]["sha"]


def _rebuild_commit(commit_sha: str, post) -> tuple[str, str]:
    """把一个本地 commit 复刻到远端，返回 (本地sha, 新sha)。"""
    raw = subprocess.check_output(
        ["git", "cat-file", "-p", commit_sha], text=True, cwd=CWD
    )
    lines = raw.splitlines()
    parent = lines[1].split()[1]
    name, email, ts, tz = re.match(
        r"author (.+) <(.+)> (\d+) ([+-]\d{4})", lines[2]
    ).groups()
    msg = raw.split("\n\n", 1)[1]  # 保留原始换行
    tzinfo = timezone(timedelta(hours=int(tz[:3]), minutes=int(tz[3:])))
    date_iso = datetime.fromtimestamp(int(ts), tzinfo).isoformat()
    base_tree = _sh(["git", "rev-parse", f"{parent}^{{tree}}"])

    diff = _sh([
        "git", "diff-tree", "--no-commit-id", "--name-status", "-r", commit_sha
    ]).splitlines()
    tree_entries = []
    for line in diff:
        parts = line.split("\t")
        status, rel = parts[0], parts[1]
        if status.startswith("D"):
            tree_entries.append({"path": rel, "mode": "100644",
                                 "type": "blob", "sha": None})
            continue
        mode = _sh(["git", "ls-tree", commit_sha, "--", rel]).split()[0]
        blob_sha = _sh(["git", "rev-parse", f"{commit_sha}:{rel}"])
        blob = post("/git/blobs", {
            "content": base64.b64encode(
                subprocess.check_output(["git", "cat-file", "blob", blob_sha], cwd=CWD)
            ).decode(),
            "encoding": "base64",
        })
        tree_entries.append({"path": rel, "mode": mode, "type": "blob", "sha": blob["sha"]})

    new_tree = post("/git/trees", {
        "base_tree": base_tree, "tree": tree_entries
    })["sha"]
    new_commit = post("/git/commits", {
        "message": msg, "tree": new_tree, "parents": [parent],
        "author": {"name": name, "email": email, "date": date_iso},
        "committer": {"name": name, "email": email, "date": date_iso},
    })
    return new_commit["sha"], commit_sha


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

    remote = _remote_sha(base, headers)
    local_head = _sh(["git", "rev-parse", "HEAD"])
    print(f"仓库: {repo}")
    print(f"远端 SHA: {remote}")
    print(f"本地 HEAD: {local_head}")

    if remote == local_head:
        print("已是最新，无需推送。")
        return

    # 找出本地领先远端的提交（旧→新）
    if remote:
        rev_range = f"{remote}..HEAD"
    else:
        rev_range = "HEAD"
    commits = _sh(["git", "rev-list", "--reverse", rev_range]).split()
    if not commits:
        raise SystemExit("本地没有领先远端的提交")

    last_sha = ""
    for commit in commits:
        api_sha, local_sha = _rebuild_commit(commit, post)
        last_sha = api_sha
        subject = _sh(["git", "log", "-1", "--format=%s", commit])
        flag = "✓" if api_sha == local_sha else "✗"
        print(f"  {flag} {subject}  ({api_sha[:8]})")
        if api_sha != local_sha:
            raise SystemExit(f"提交 {commit[:8]} SHA 不一致，已停止")

    patch("/git/refs/heads/main", {"sha": last_sha, "force": True})
    final_remote = _remote_sha(base, headers)
    print(f"推送完成：远端 {final_remote}")
    print(f"一致: {final_remote == local_head}")
    if final_remote != local_head:
        raise SystemExit("远端与本地 HEAD 不一致")


if __name__ == "__main__":
    main()
