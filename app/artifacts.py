"""产物清单、摘要、原子写与下载路径约束。

产物写入 attempt 独享目录；文件先写临时名再同目录原子重命名，最后写 manifest。
发布前由监督进程校验清单、文件尺寸与摘要；数据库发布在受租约保护的事务中进行。
文件与 SQLite 不是同一个事务 —— 这个间隙的语义见 docs/architecture.md。
"""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUFFER = 1 << 20


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(BUFFER)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """同目录临时名 + os.replace，避免半个文件被读到。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_bytes(
        path, json.dumps(obj, ensure_ascii=True, sort_keys=True, indent=1).encode("utf-8")
    )


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@dataclass
class FileEntry:
    path: str     # 相对产物目录的 POSIX 路径
    sha256: str
    size: int


@dataclass
class OutputBundle:
    """一个逻辑产物：单文件（gif/zip/json）或多文件目录（帧集合）。"""

    kind: str                     # params | frames | motion_plan | styled_frames | preview_gif | final_zip
    relative_path: str            # 相对 attempt 工作目录
    is_dir: bool
    files: list[FileEntry]
    total_size: int
    manifest: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "relative_path": self.relative_path,
            "is_dir": self.is_dir,
            "files": [{"path": f.path, "sha256": f.sha256, "size": f.size} for f in self.files],
            "total_size": self.total_size,
            "manifest": self.manifest,
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "OutputBundle":
        return OutputBundle(
            kind=obj["kind"],
            relative_path=obj["relative_path"],
            is_dir=bool(obj["is_dir"]),
            files=[FileEntry(f["path"], f["sha256"], f["size"]) for f in obj["files"]],
            total_size=int(obj["total_size"]),
            manifest=obj.get("manifest", {}),
        )


def bundle_single_file(kind: str, path: Path, work_dir: Path, manifest: dict[str, Any]) -> OutputBundle:
    rel = path.relative_to(work_dir).as_posix()
    entry = FileEntry(path=path.name if path.parent == work_dir else rel,
                      sha256=sha256_file(path), size=path.stat().st_size)
    if path.parent != work_dir:
        entry = FileEntry(path=rel, sha256=entry.sha256, size=entry.size)
    return OutputBundle(
        kind=kind, relative_path=rel, is_dir=False, files=[entry],
        total_size=entry.size, manifest=manifest,
    )


def bundle_directory(kind: str, dir_path: Path, work_dir: Path, manifest: dict[str, Any]) -> OutputBundle:
    entries: list[FileEntry] = []
    for p in sorted(dir_path.rglob("*")):
        if p.is_file() and not p.name.endswith(".tmp") and ".tmp-" not in p.name:
            entries.append(
                FileEntry(
                    path=p.relative_to(dir_path).as_posix(),
                    sha256=sha256_file(p),
                    size=p.stat().st_size,
                )
            )
    return OutputBundle(
        kind=kind,
        relative_path=dir_path.relative_to(work_dir).as_posix(),
        is_dir=True,
        files=entries,
        total_size=sum(e.size for e in entries),
        manifest=manifest,
    )


def bundle_digest(bundle: OutputBundle) -> str:
    """产物记录的 sha256 字段：单文件=文件摘要；目录=清单规范化摘要。"""
    if not bundle.is_dir:
        return bundle.files[0].sha256 if bundle.files else ""
    canonical = json.dumps(
        [{"path": f.path, "sha256": f.sha256, "size": f.size} for f in bundle.files],
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_bundle(work_dir: Path, bundle: OutputBundle) -> list[str]:
    """校验文件存在、尺寸、摘要。返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    base = work_dir / bundle.relative_path
    if not bundle.files:
        problems.append(f"{bundle.kind}: 清单为空")
        return problems
    if bundle.is_dir:
        if not base.is_dir():
            return [f"{bundle.kind}: 目录缺失 {bundle.relative_path}"]
        for fe in bundle.files:
            fp = base / fe.path
            if not fp.is_file():
                problems.append(f"{bundle.kind}: 文件缺失 {fe.path}")
                continue
            if fp.stat().st_size != fe.size:
                problems.append(f"{bundle.kind}: 尺寸不符 {fe.path}")
                continue
            if sha256_file(fp) != fe.sha256:
                problems.append(f"{bundle.kind}: 摘要不符 {fe.path}")
    else:
        if not base.is_file():
            return [f"{bundle.kind}: 文件缺失 {bundle.relative_path}"]
        fe = bundle.files[0]
        if base.stat().st_size != fe.size:
            problems.append(f"{bundle.kind}: 尺寸不符 {bundle.relative_path}")
        elif sha256_file(base) != fe.sha256:
            problems.append(f"{bundle.kind}: 摘要不符 {bundle.relative_path}")
    return problems


def verify_published_artifact(artifacts_root: Path, rel_path: str, sha256: str,
                              size_bytes: int, is_dir: bool, manifest: dict[str, Any]) -> list[str]:
    """按已发布记录校验磁盘现状（下载/预检用）。相对路径必须约束在产物根目录内。"""
    target = resolve_under_root(artifacts_root, rel_path)
    if target is None:
        return [f"路径越界或不存在: {rel_path}"]
    if is_dir:
        problems: list[str] = []
        if not target.is_dir():
            return [f"目录缺失: {rel_path}"]
        total = 0
        for fe in manifest.get("files", []):
            fp = target / fe["path"]
            if not fp.is_file():
                problems.append(f"文件缺失: {fe['path']}")
                continue
            total += fp.stat().st_size
            if fp.stat().st_size != fe["size"]:
                problems.append(f"尺寸不符: {fe['path']}")
            elif sha256_file(fp) != fe["sha256"]:
                problems.append(f"摘要不符: {fe['path']}")
        if total != size_bytes:
            problems.append(f"总尺寸不符: 记录 {size_bytes}，实际 {total}")
        return problems
    if not target.is_file():
        return [f"文件缺失: {rel_path}"]
    st = target.stat().st_size
    if st != size_bytes:
        return [f"尺寸不符: {rel_path}"]
    if sha256_file(target) != sha256:
        return [f"摘要不符: {rel_path}"]
    return []


def resolve_under_root(artifacts_root: Path, relative_path: str) -> Path | None:
    """把相对路径约束在产物根目录内；拒绝绝对路径与 .. 逃逸。"""
    if not relative_path or relative_path.startswith(("/", "\\")):
        return None
    root = artifacts_root.resolve()
    try:
        target = (root / relative_path).resolve()
    except (OSError, ValueError):
        return None
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


def zip_directory_to(dir_path: Path, zip_path: Path) -> None:
    """把目录打包为 ZIP（用于多文件产物的下载）。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_name(zip_path.name + f".tmp-{os.getpid()}")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(dir_path.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(dir_path).as_posix())
    os.replace(tmp, zip_path)
