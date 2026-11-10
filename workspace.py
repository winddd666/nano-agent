"""A task-local source tree, with one path policy and observable revisions."""

import difflib
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


SKIP = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache"}
MAX_FILE = 1024 * 1024


@dataclass(frozen=True)
class LargeFile:
    size: int
    digest: str


class Workspace:
    def __init__(self, root):
        if Path(root).is_symlink():
            raise ValueError("工作区根目录不能是符号链接")
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("工作区必须是已存在的目录")
        for base, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = [name for name in dirs if name not in SKIP and not (Path(base) / name).is_symlink()]
            for name in files:
                path = Path(base) / name
                if path.is_file() and not path.is_symlink() and path.stat().st_nlink > 1:
                    raise ValueError(f"工作区包含硬链接文件：{path.relative_to(self.root)}")
        self.baseline = self._snapshot()
        self.current = dict(self.baseline)
        self.revision = 0

    def path(self, name, *, existing=True):
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ValueError("路径必须是非空相对路径")
        relative = Path(name)
        if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
            raise ValueError("路径必须在工作区内，不能使用 . 或 ..")
        path = self.root / relative
        # Reject links in every component, including a dangling final link.
        cursor = self.root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError("工作区工具不跟随符号链接")
        if existing and not path.is_file():
            raise ValueError("文件不存在或不是普通文件")
        if not existing and not path.parent.is_dir():
            raise ValueError("父目录不存在")
        return path

    def _snapshot(self):
        result = {}
        for base, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not (Path(base) / d).is_symlink())
            for name in sorted(files):
                path = Path(base) / name
                if path.is_symlink() or not path.is_file():
                    continue
                rel = path.relative_to(self.root).as_posix()
                try:
                    if path.stat().st_size > MAX_FILE:
                        digest = hashlib.sha256()
                        with path.open("rb") as source:
                            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                digest.update(chunk)
                        result[rel] = LargeFile(path.stat().st_size, digest.hexdigest())
                    else:
                        result[rel] = path.read_bytes()
                except OSError:
                    result[rel] = LargeFile(-1, "unreadable")
        return result

    def refresh(self):
        latest = self._snapshot()
        if latest != self.current:
            self.current = latest
            self.revision += 1
        return self.revision

    def list_files(self, directory="."):
        try:
            path = self.directory(directory)
            items = []
            for child in sorted(path.iterdir()):
                if child.name in SKIP or child.is_symlink():
                    continue
                items.append(child.relative_to(self.root).as_posix() + ("/" if child.is_dir() else ""))
            return "\n".join(items) or "（空目录）"
        except (ValueError, OSError) as exc:
            return "错误：" + str(exc)

    def directory(self, name):
        if name == ".":
            return self.root
        path = self.path(name, existing=False)
        if not path.is_dir():
            raise ValueError("目录不存在")
        return path

    def read_file(self, name, start_line, end_line):
        try:
            path = self.path(name)
            if type(start_line) is not int or type(end_line) is not int or start_line < 1 or end_line < start_line:
                raise ValueError("行号必须是从 1 开始的有效范围")
            raw = path.read_bytes()
            if len(raw) > MAX_FILE:
                raise ValueError("文件超过 1 MiB 上限")
            content = raw.decode("utf-8")
        except (ValueError, OSError, UnicodeError) as exc:
            return "错误：" + str(exc)
        lines = content.splitlines(keepends=True)
        selected = lines[start_line - 1:end_line]
        digest = hashlib.sha256(raw).hexdigest()
        return f"文件: {name}\nsha256: {digest}\n总行数: {len(lines)}\n" + "".join(
            f"{index}: {line}" for index, line in enumerate(selected, start_line)
        )

    def search_text(self, query, max_results):
        if not query or type(max_results) is not int or not 1 <= max_results <= 100:
            return "错误：query 非空，max_results 为 1 到 100"
        hits = []
        for name, content in sorted(self._snapshot().items()):
            if isinstance(content, LargeFile):
                continue
            try:
                lines = content.decode("utf-8").splitlines()
            except UnicodeError:
                continue
            for number, line in enumerate(lines, 1):
                if query in line:
                    hits.append(f"{name}:{number}: {line[:240]}")
                    if len(hits) >= max_results:
                        return "\n".join(hits) + "\n（达到结果上限）"
        return "\n".join(hits) or "（无匹配）"

    def edit_file(self, name, expected_hash, old_text, new_text):
        try:
            path = self.path(name)
            raw = path.read_bytes()
            if len(raw) > MAX_FILE:
                raise ValueError("文件超过 1 MiB 上限")
            if hashlib.sha256(raw).hexdigest() != expected_hash:
                raise ValueError("文件摘要已变化，请重新读取")
            original = raw.decode("utf-8")
            if not old_text or original.count(old_text) != 1:
                raise ValueError("old_text 必须唯一匹配且非空")
            changed = original.replace(old_text, new_text, 1).encode("utf-8")
            temporary = path.with_name("." + path.name + ".nano-tmp")
            if temporary.exists() or temporary.is_symlink():
                raise ValueError("临时文件路径已被占用")
            try:
                with temporary.open("xb") as output:
                    output.write(changed)
                os.chmod(temporary, path.stat().st_mode & 0o777)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            self.refresh()
            return f"已修改 {name}；sha256: {hashlib.sha256(changed).hexdigest()}；workspace_revision: {self.revision}"
        except (ValueError, OSError, UnicodeError) as exc:
            return "错误：" + str(exc)

    def create_file(self, name, content):
        try:
            path = self.path(name, existing=False)
            if len(content.encode("utf-8")) > MAX_FILE:
                raise ValueError("新文件超过 1 MiB 上限")
            with path.open("x", encoding="utf-8") as output:
                output.write(content)
            self.refresh()
            return f"已创建 {name}；workspace_revision: {self.revision}"
        except (ValueError, OSError, UnicodeError) as exc:
            return "错误：" + str(exc)

    def get_diff(self):
        self.refresh()
        result = []
        for name in sorted(self.baseline.keys() | self.current.keys()):
            present_before, present_after = name in self.baseline, name in self.current
            before, after = self.baseline.get(name, b""), self.current.get(name, b"")
            if present_before == present_after and before == after:
                continue
            if not present_before and after == b"":
                result.append(f"新增空文件: {name}\n")
                continue
            if not present_after and before == b"":
                result.append(f"删除空文件: {name}\n")
                continue
            if isinstance(before, LargeFile) or isinstance(after, LargeFile):
                result.append(f"{name}: 二进制或超过 1 MiB，无法显示文本差异\n")
                continue
            try:
                old = before.decode("utf-8").splitlines(keepends=True)
                new = after.decode("utf-8").splitlines(keepends=True)
            except UnicodeError:
                result.append(f"{name}: 非 UTF-8 文件，无法显示文本差异\n")
                continue
            result.extend(difflib.unified_diff(old, new, fromfile="a/" + name, tofile="b/" + name))
        return f"workspace_revision: {self.revision}\n" + ("".join(result) or "（无改动）")
