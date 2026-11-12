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

    def _normalize_rel_input(self, name, *, allow_root=False):
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ValueError("路径必须是非空相对路径")
        s = name.strip() if name.strip() == name else name
        if s in ("/tmp", "/tmp/") or s.startswith("/tmp/") or s.startswith("../tmp/"):
            raise ValueError("路径必须在工作区内，不能使用 . 或 ..（临时文件请在 run_command 中读写 /tmp）")
        if s in ("/work", "/work/"):
            s = "."
        elif s.startswith("/work/"):
            s = s[6:]
        while s.startswith("./"):
            s = s[2:]
        if s not in ("", ".") and s.endswith("/"):
            s = s.rstrip("/")
        if allow_root and s == ".":
            return "."
        relative = Path(s)
        if relative.is_absolute() or not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
            raise ValueError("路径必须在工作区内，不能使用 . 或 ..")
        return relative.as_posix()

    def path(self, name, *, existing=True, create_parents=False):
        rel = self._normalize_rel_input(name, allow_root=False)
        relative = Path(rel)
        path = self.root / relative
        # Reject links in every component, including a dangling final link.
        cursor = self.root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError("工作区工具不跟随符号链接")
        if existing:
            if path.is_dir():
                raise ValueError(f"路径 {rel} 是目录而非普通文件，请改用 list_files")
            if not path.is_file():
                raise ValueError("文件不存在或不是普通文件")
        elif not path.parent.is_dir():
            if create_parents:
                path.parent.mkdir(parents=True, exist_ok=True)
            else:
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

    def directory(self, name="."):
        norm = self._normalize_rel_input(name, allow_root=True)
        if norm == ".":
            return self.root
        path = self.path(norm, existing=False)
        if not path.is_dir():
            raise ValueError("目录不存在")
        return path

    def read_file(self, name, start_line=1, end_line=200):
        try:
            path = self.path(name)
            rel = path.relative_to(self.root).as_posix()
            if start_line is None:
                start_line = 1
            if end_line is None:
                end_line = 200
            if type(start_line) is not int or type(end_line) is not int or start_line < 1 or end_line < start_line:
                raise ValueError(
                    f"行号必须是从 1 开始的有效范围（收到 start_line={start_line}, end_line={end_line}，要求 1 <= start_line <= end_line）"
                )
            raw = path.read_bytes()
            if len(raw) > MAX_FILE:
                raise ValueError("文件超过 1 MiB 上限")
            content = raw.decode("utf-8")
        except (ValueError, OSError, UnicodeError) as exc:
            return "错误：" + str(exc)
        lines = content.splitlines(keepends=True)
        digest = hashlib.sha256(raw).hexdigest()
        header = f"文件: {rel}\nsha256: {digest}\n总行数: {len(lines)}\n"
        if lines and start_line > len(lines):
            return header + f"（请求起始行 {start_line} 超过文件总行数 {len(lines)}）\n"
        selected = lines[start_line - 1:end_line]
        return header + "".join(
            f"{index}: {line}" for index, line in enumerate(selected, start_line)
        )

    def search_text(self, query, max_results=30):
        if max_results is None:
            max_results = 30
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
            rel = path.relative_to(self.root).as_posix()
            raw = path.read_bytes()
            if len(raw) > MAX_FILE:
                raise ValueError("文件超过 1 MiB 上限")
            actual_hash = hashlib.sha256(raw).hexdigest()
            original = raw.decode("utf-8")
            orig_lines = original.splitlines()
            if actual_hash != expected_hash:
                raise ValueError(
                    f"文件摘要已变化，请重新读取（当前最新 sha256: {actual_hash}，总行数: {len(orig_lines)}）"
                )
            if not old_text:
                raise ValueError("old_text 必须唯一匹配且非空（收到空字符串）")
            count = original.count(old_text)
            if count == 0:
                probe = next((ln.strip() for ln in old_text.splitlines() if ln.strip()), "")
                near = []
                if len(probe) >= 4:
                    near = [
                        idx for idx, ln in enumerate(orig_lines, 1)
                        if ln.strip() and (
                            probe in ln.strip()
                            or difflib.SequenceMatcher(None, probe, ln.strip()).ratio() >= 0.72
                        )
                    ][:4]
                near_note = f"；第 {', '.join(map(str, near))} 行存在相似文本，请用 read_file 核对精确缩进与字符" if near else ""
                raise ValueError(
                    f"old_text 必须唯一匹配且非空（在 {rel} 中匹配 0 处，文件共 {len(orig_lines)} 行{near_note}）"
                )
            if count > 1:
                starts, pos = [], 0
                while len(starts) < 6:
                    idx = original.find(old_text, pos)
                    if idx == -1:
                        break
                    starts.append(original.count("\n", 0, idx) + 1)
                    pos = idx + max(1, len(old_text))
                raise ValueError(
                    f"old_text 必须唯一匹配且非空（在 {rel} 中匹配到 {count} 处，起始行号：{', '.join(map(str, starts))}；"
                    "请补充前后上下文行使其唯一）"
                )
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
            return f"已修改 {rel}；sha256: {hashlib.sha256(changed).hexdigest()}；workspace_revision: {self.revision}"
        except (ValueError, OSError, UnicodeError) as exc:
            return "错误：" + str(exc)

    def create_file(self, name, content, overwrite=False):
        try:
            if type(overwrite) is not bool:
                raise ValueError("overwrite 必须是布尔值")
            path = self.path(name, existing=False, create_parents=True)
            rel = path.relative_to(self.root).as_posix()
            encoded = content.encode("utf-8")
            if len(encoded) > MAX_FILE:
                raise ValueError("新文件超过 1 MiB 上限")
            if path.is_dir():
                raise ValueError(f"路径 {rel} 已是目录，不能创建同名文件")
            if path.exists():
                if not overwrite:
                    cur_raw = path.read_bytes()
                    cur_hash = hashlib.sha256(cur_raw).hexdigest()
                    cur_lines = len(cur_raw.decode("utf-8", errors="replace").splitlines())
                    raise ValueError(
                        f"文件 {rel} 已存在（当前 sha256: {cur_hash}，总行数: {cur_lines}）；"
                        "如需整文件重写请传 overwrite=true，或使用当前 sha256 调用 edit_file"
                    )
                temporary = path.with_name("." + path.name + ".nano-tmp")
                if temporary.exists() or temporary.is_symlink():
                    raise ValueError("临时文件路径已被占用")
                try:
                    with temporary.open("xb") as output:
                        output.write(encoded)
                    os.chmod(temporary, path.stat().st_mode & 0o777)
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
                self.refresh()
                return f"已覆盖 {rel}；sha256: {hashlib.sha256(encoded).hexdigest()}；workspace_revision: {self.revision}"
            with path.open("x", encoding="utf-8") as output:
                output.write(content)
            self.refresh()
            return f"已创建 {rel}；sha256: {hashlib.sha256(encoded).hexdigest()}；workspace_revision: {self.revision}"
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
