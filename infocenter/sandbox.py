"""跨场景任务沙箱与通用交付物核验模块（Task Sandbox & Deliverable Verification）。

支持三类工作场景的极简统一抽象：
1. 代码研发（Code）：当项目为 Git 仓库时，自动优先创建轻量级 `git worktree` 隔离沙箱。
2. 科研攻关与商务工作（Research & Business）：当项目为普通目录（如论文手稿、实验数据、商业方案文件夹）时，
   自动创建独立草稿沙箱（`draft-dir`），按需拷入 `deps` 声明的输入文件，完成后通过 `--apply` 将产物写回主目录。
3. 通用交付物核验（Deliverable Verification）：同时支持 `<path>@sha256:<hex>` 精确哈希校验与
   `file:<path>` 交付物存在且非空校验（适用于科研文稿、数据表与商务报告）。
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from infocenter.common import MailError, normalize_case_id

SANDBOX_META_FILE = ".ic-sandbox.json"
SANDBOX_ROOT_NAME = ".ic-sandboxes"


def _resolve_target_path(raw_path: str, base_dir: Path | None = None) -> Path:
    p = Path(raw_path).expanduser()
    if not p.is_absolute() and base_dir is not None:
        return base_dir / p
    return p


def _extract_file_tokens(spec: str) -> list[tuple[str, str, str]]:
    """从 deps 或 acceptance 文本中提取文件校验项。

    返回列表 [(kind, raw_path, extra), ...]：
      - ("sha256", path, expected_hex)
      - ("file", path, "")
    """
    items: list[tuple[str, str, str]] = []
    for raw in (spec or "").replace(";", " ").split():
        token = raw.strip()
        if not token:
            continue
        if "@sha256:" in token:
            fpath, expected_hash = token.split("@sha256:", 1)
            if fpath.strip():
                items.append(("sha256", fpath.strip(), expected_hash.strip()))
        elif token.startswith("file:"):
            fpath = token[len("file:") :].strip()
            if fpath:
                items.append(("file", fpath, ""))
    return items


def verify_deliverables(
    task_id: str,
    acc: str,
    deps: str,
    *,
    base_dir: Path | None = None,
    for_done: bool = False,
) -> tuple[str, str]:
    """核验任务的验收标准与依赖/交付物（兼容代码、科研与商务场景）。"""
    acc = (acc or "").strip()
    deps = (deps or "").strip()
    if not acc and not deps:
        if for_done:
            raise MailError(
                "verification_failed",
                f"任务 {task_id} 开启了 --verify 但未设置 acceptance 或 deps 验收标准，拒绝设为 done",
            )
        raise MailError(
            "verification_failed",
            f"任务 {task_id} 未配置任何验收标准 (acceptance) 或依赖哈希 (deps)，无法自动化硬验证",
        )
    checks = _extract_file_tokens(deps) + [
        item for item in _extract_file_tokens(acc) if item[0] == "file"
    ]
    for kind, fpath, expected_hash in checks:
        p = _resolve_target_path(fpath, base_dir)
        if kind == "sha256":
            if not p.is_file():
                raise MailError("verification_failed", f"依赖文件不存在：{fpath}")
            actual_hash = hashlib.sha256(p.read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                if for_done:
                    raise MailError("verification_failed", f"依赖文件哈希不匹配：{fpath}")
                raise MailError(
                    "verification_failed",
                    f"依赖文件哈希不匹配：{fpath} 预期 {expected_hash[:8]}，实际 {actual_hash[:8]}",
                )
        elif kind == "file":
            if not p.is_file():
                raise MailError("verification_failed", f"交付物文件不存在：{fpath}")
            if p.stat().st_size == 0:
                raise MailError("verification_failed", f"交付物文件为空（0 字节）：{fpath}")
    return acc, deps


def _snapshot_files(root: Path) -> dict[str, str]:
    """记录沙箱内文件的相对路径与 sha256，供 --apply 识别新增或修改的交付物。"""
    snap: dict[str, str] = {}
    if not root.is_dir():
        return snap
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == SANDBOX_META_FILE or rel.startswith(".git/") or rel == ".git":
            continue
        try:
            snap[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return snap


def _try_git_worktree_add(ws: Path, sandbox_dir: Path) -> bool:
    if not (ws / ".git").exists() or not shutil.which("git"):
        return False
    try:
        res = subprocess.run(
            ["git", "-C", str(ws), "worktree", "add", "--detach", str(sandbox_dir), "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return res.returncode == 0 and sandbox_dir.is_dir()
    except (OSError, subprocess.SubprocessError):
        return False


def manage_task_sandbox(
    task_data: dict,
    *,
    workspace_override: str | None = None,
    apply: bool = False,
    clean: bool = False,
) -> dict:
    """创建、归档（--apply）或清理（--clean）跨场景任务沙箱。"""
    tid = normalize_case_id(task_data.get("id") or "")
    raw_ws = workspace_override or task_data.get("workspace") or os.getcwd()
    ws = Path(str(raw_ws)).expanduser().resolve()
    if not ws.is_dir():
        raise MailError("invalid_workspace", f"项目目录不存在：{ws}", 400)
    sandbox_dir = ws / SANDBOX_ROOT_NAME / tid
    meta_path = sandbox_dir / SANDBOX_META_FILE

    if clean:
        mode = "draft-dir"
        if meta_path.is_file():
            try:
                mode = json.loads(meta_path.read_text(encoding="utf-8")).get("mode") or mode
            except (OSError, ValueError):
                pass
        if mode == "git-worktree" and shutil.which("git"):
            try:
                subprocess.run(
                    ["git", "-C", str(ws), "worktree", "remove", "--force", str(sandbox_dir)],
                    capture_output=True,
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        if sandbox_dir.exists():
            shutil.rmtree(sandbox_dir, ignore_errors=True)
        return {
            "task_id": tid,
            "action": "clean",
            "mode": mode,
            "workspace": str(ws),
            "sandbox_dir": str(sandbox_dir),
            "applied_files": [],
        }

    if apply:
        if not sandbox_dir.is_dir():
            raise MailError("sandbox_not_found", f"任务 {tid} 的沙箱不存在：{sandbox_dir}", 404)
        meta: dict = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        baseline: dict[str, str] = meta.get("baseline") or {}
        current = _snapshot_files(sandbox_dir)
        applied: list[str] = []
        for rel, digest in sorted(current.items()):
            if baseline.get(rel) == digest:
                continue
            src = sandbox_dir / rel
            dst = ws / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and not (dst.stat().st_mode & 0o200):
                try:
                    os.chmod(dst, dst.stat().st_mode | 0o200)
                except OSError:
                    pass
            shutil.copy2(src, dst)
            applied.append(rel)
        return {
            "task_id": tid,
            "action": "apply",
            "mode": meta.get("mode") or "draft-dir",
            "workspace": str(ws),
            "sandbox_dir": str(sandbox_dir),
            "applied_files": applied,
        }

    # 创建或复用沙箱
    sandbox_dir.parent.mkdir(parents=True, exist_ok=True)
    if sandbox_dir.is_dir() and meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return {
                "task_id": tid,
                "action": "create",
                "mode": meta.get("mode") or "draft-dir",
                "workspace": str(ws),
                "sandbox_dir": str(sandbox_dir),
                "applied_files": [],
            }
        except (OSError, ValueError):
            pass

    if not sandbox_dir.exists() and _try_git_worktree_add(ws, sandbox_dir):
        mode = "git-worktree"
    else:
        mode = "draft-dir"
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        for _kind, fpath, _extra in _extract_file_tokens(str(task_data.get("deps") or "")):
            src = _resolve_target_path(fpath, ws)
            if src.is_file():
                try:
                    rel = src.resolve().relative_to(ws)
                except ValueError:
                    rel = Path(src.name)
                dst = sandbox_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    baseline = _snapshot_files(sandbox_dir)
    meta_path.write_text(
        json.dumps(
            {
                "task_id": tid,
                "mode": mode,
                "workspace": str(ws),
                "baseline": baseline,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {
        "task_id": tid,
        "action": "create",
        "mode": mode,
        "workspace": str(ws),
        "sandbox_dir": str(sandbox_dir),
        "applied_files": [],
    }
