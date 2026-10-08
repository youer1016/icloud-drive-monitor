#!/usr/bin/env python3
"""Local iCloud Drive event monitor and opt-in metadata-only tree inventory."""
from __future__ import annotations

import json
import ctypes
import errno
import os
import queue
import re
import secrets
import signal
import stat
from contextlib import contextmanager
import subprocess
import threading
import time
import pwd
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
PORT = 8766
HERE = Path(__file__).resolve().parent
LOCAL_USER = os.environ.get("SUDO_USER") or os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
USER_HOME = Path(pwd.getpwnam(LOCAL_USER).pw_dir)
ICLOUD_ROOT = USER_HOME / "Library/Mobile Documents/com~apple~CloudDocs"
EVENTS: deque[dict] = deque(maxlen=250)
EVENT_QUEUE: queue.Queue[dict] = queue.Queue()
LOCK = threading.Lock()
ACTIVE_CLIENTS = 0
MONITOR: subprocess.Popen[str] | None = None
STOP = threading.Event()
SCAN_LOCK = threading.Lock()
SCAN_CANCEL = threading.Event()
SCAN_STATE: dict = {"phase": "idle", "items": 0, "files": 0,
                    "unknown_folders": 0, "cloud_only": 0, "error": None}
SCAN_TREE: dict | None = None
SCAN_INDEX: dict[str, dict] = {}
SCAN_FILES: dict[str, dict] = {}
SCAN_MAX_ITEMS = 500_000
EVICT_HELPER = HERE / "evict_local"
EVICT_LOCK = threading.Lock()
EVICT_STATE: dict = {"phase": "idle", "total": 0, "done": 0,
                     "succeeded": 0, "results": []}
SF_DATALESS = 0x40000000
IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES = 3
IOPOL_SCOPE_THREAD = 1
IOPOL_MATERIALIZE_DATALESS_FILES_OFF = 1


def bird_snapshot() -> dict:
    result = subprocess.run(
        ["ps", "-axo", "pid=,state=,command="],
        capture_output=True, text=True, check=False,
    )
    for line in result.stdout.splitlines():
        if re.search(r"/Support/bird(?:\s|$)", line):
            pid, state, command = line.strip().split(maxsplit=2)
            return {"pid": pid, "state": state, "command": command}
    return {"pid": None, "state": "absent", "command": None}


def classify(operation: str, path: str | None) -> str:
    if re.search(r"WrData|pwrite|write|rename|create|mkdir", operation, re.I):
        return "写入候选"
    if re.search(r"RdData|pread|read", operation, re.I):
        return "数据读取"
    if re.search(r"stat|attr|lookup|access|readdir", operation, re.I):
        return "属性查询"
    return "其他活动"


def extract_path(line: str) -> str | None:
    """Extract only a Finder-visible iCloud Drive path from fs_usage output."""
    root = str(ICLOUD_ROOT)
    start = line.find(root)
    if start < 0:
        return None
    path = line[start:]
    # fs_usage appends duration, flags, and process name after a padded column.
    path = re.sub(r"\s{2,}\d+\.\d+\s+(?:W\s+)?[^\n]+$", "", path).rstrip()
    if not path.startswith(root) or len(path) >= 4096:
        return None
    return path


def source_process(line: str) -> str:
    """Return the process label printed by fs_usage, when available."""
    match = re.search(r"\s(?:W\s+)?([^\s]+)\.\d+\s*$", line)
    return match.group(1) if match else "未知进程"


def parse_line(line: str) -> dict | None:
    operation = line.split()[1] if len(line.split()) > 1 else "unknown"
    path = extract_path(line)
    if not path:
        return None
    event = {
        "time": time.strftime("%H:%M:%S"),
        "process": source_process(line),
        "operation": operation,
        "kind": classify(operation, path),
        "path": path,
        "raw": line.strip(),
    }
    return event


def publish(event: dict) -> None:
    EVENTS.append(event)
    EVENT_QUEUE.put(event)


def monitor_reader(process: subprocess.Popen[str]) -> None:
    assert process.stdout
    for line in process.stdout:
        if STOP.is_set() or process is not MONITOR:
            break
        event = parse_line(line)
        if event:
            publish(event)


def start_monitor() -> None:
    global MONITOR
    with LOCK:
        if MONITOR and MONITOR.poll() is None:
            return
        STOP.clear()
        MONITOR = subprocess.Popen(
            # Finder downloads can be hydrated by fileproviderd as well as bird.
            # Filtering is done on the actual Finder-visible iCloud Drive root.
            ["fs_usage", "-w", "-f", "filesys", "-f", "pathname"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        threading.Thread(target=monitor_reader, args=(MONITOR,), daemon=True).start()


def stop_monitor() -> None:
    global MONITOR
    with LOCK:
        STOP.set()
        if MONITOR and MONITOR.poll() is None:
            MONITOR.terminate()
            try:
                MONITOR.wait(timeout=2)
            except subprocess.TimeoutExpired:
                MONITOR.kill()
        MONITOR = None


def status() -> dict:
    snapshot = bird_snapshot()
    return {
        "bird": snapshot,
        "monitoring": bool(MONITOR and MONITOR.poll() is None),
        "clients": ACTIVE_CLIENTS,
        "events": list(EVENTS),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


class ScanCancelled(Exception):
    pass


@contextmanager
def prevent_materialization():
    """Fail closed if macOS cannot disable dataless item materialization."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.getiopolicy_np.argtypes = [ctypes.c_int, ctypes.c_int]
    libc.getiopolicy_np.restype = ctypes.c_int
    libc.setiopolicy_np.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    libc.setiopolicy_np.restype = ctypes.c_int
    previous = libc.getiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
                                   IOPOL_SCOPE_THREAD)
    if previous < 0:
        raise OSError(ctypes.get_errno(), "无法读取禁止按需下载策略；扫描已取消")
    result = libc.setiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
                                 IOPOL_SCOPE_THREAD,
                                 IOPOL_MATERIALIZE_DATALESS_FILES_OFF)
    if result < 0:
        raise OSError(ctypes.get_errno(), "无法启用禁止按需下载策略；扫描已取消")
    try:
        yield
    finally:
        libc.setiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
                            IOPOL_SCOPE_THREAD, previous)


def scan_metadata(root: Path, cancel: threading.Event,
                  progress: dict, max_items: int = SCAN_MAX_ITEMS) -> tuple[dict, dict, dict]:
    """List locally materialized files with dataless materialization disabled."""
    index: dict[str, dict] = {}
    files: dict[str, dict] = {}

    def visit(folder: Path, relative: str) -> dict:
        if cancel.is_set():
            raise ScanCancelled()
        node = {"name": folder.name if relative else "iCloud Drive",
                "relative": relative, "directory": True, "local_bytes": 0,
                "files": 0, "selectable_files": 0, "children": []}
        index[relative] = node
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if cancel.is_set():
                        raise ScanCancelled()
                    if entry.name.startswith("."):
                        continue
                    progress["items"] += 1
                    if progress["items"] > max_items:
                        raise RuntimeError(f"项目超过 {max_items} 项，扫描已停止")
                    child_relative = f"{relative}/{entry.name}" if relative else entry.name
                    try:
                        item_stat = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        if error.errno == errno.EDEADLK:
                            progress["unknown_folders"] += 1
                        continue
                    if stat.S_ISDIR(item_stat.st_mode):
                        if item_stat.st_flags & SF_DATALESS:
                            child = {"name": entry.name, "relative": child_relative,
                                     "directory": True, "local_bytes": 0, "files": 0,
                                     "selectable_files": 0,
                                     "unscanned": True, "children": []}
                            index[child_relative] = child
                            progress["unknown_folders"] += 1
                        else:
                            child = visit(Path(entry.path), child_relative)
                        node["children"].append(child)
                        node["local_bytes"] += child["local_bytes"]
                        node["files"] += child["files"]
                        node["selectable_files"] += child["selectable_files"]
                    elif stat.S_ISREG(item_stat.st_mode):
                        if item_stat.st_flags & SF_DATALESS:
                            progress["cloud_only"] += 1
                            continue
                        allocated = item_stat.st_blocks * 512
                        node["children"].append({"name": entry.name,
                            "relative": child_relative, "directory": False,
                            "local_bytes": allocated})
                        files[child_relative] = {
                            "local_bytes": allocated, "device": item_stat.st_dev,
                            "inode": item_stat.st_ino, "size": item_stat.st_size,
                            "mtime_ns": item_stat.st_mtime_ns,
                        }
                        node["local_bytes"] += allocated
                        node["files"] += 1
                        node["selectable_files"] += int(allocated > 0)
                        progress["files"] += 1
        except OSError as error:
            if error.errno == errno.EDEADLK:
                node["unscanned"] = True
                progress["unknown_folders"] += 1
            else:
                node["error"] = str(error)
        node["children"].sort(key=lambda item: (not item["directory"], item["name"].casefold()))
        return node

    with prevent_materialization():
        return visit(root, ""), index, files


def run_scan() -> None:
    global SCAN_TREE, SCAN_INDEX, SCAN_FILES
    try:
        tree, index, files = scan_metadata(ICLOUD_ROOT, SCAN_CANCEL, SCAN_STATE)
        with SCAN_LOCK:
            if SCAN_CANCEL.is_set():
                SCAN_STATE["phase"] = "cancelled"
            else:
                SCAN_TREE, SCAN_INDEX, SCAN_FILES = tree, index, files
                SCAN_STATE["phase"] = "complete"
                SCAN_STATE["scan_id"] = secrets.token_hex(12)
                SCAN_STATE["local_bytes"] = tree["local_bytes"]
                SCAN_STATE["scanned_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except ScanCancelled:
        with SCAN_LOCK:
            SCAN_STATE["phase"] = "cancelled"
    except Exception as error:
        with SCAN_LOCK:
            SCAN_STATE["phase"] = "error"
            SCAN_STATE["error"] = str(error)


def begin_scan() -> dict:
    global SCAN_TREE, SCAN_INDEX, SCAN_FILES
    with EVICT_LOCK, SCAN_LOCK:
        if EVICT_STATE["phase"] == "running":
            return {"phase": "error", "error": "移除本地下载正在进行，请等待完成"}
        if SCAN_STATE["phase"] == "running":
            return dict(SCAN_STATE)
        SCAN_CANCEL.clear()
        SCAN_TREE, SCAN_INDEX, SCAN_FILES = None, {}, {}
        SCAN_STATE.clear()
        SCAN_STATE.update(phase="running", items=0, files=0,
                          unknown_folders=0, cloud_only=0, error=None)
        threading.Thread(target=run_scan, daemon=True).start()
        return dict(SCAN_STATE)


def forget_local_file(relative: str, identity: dict) -> None:
    """Reconcile a snapshotted file after it is confirmed dataless."""
    with SCAN_LOCK:
        if relative not in SCAN_FILES:
            return
        del SCAN_FILES[relative]
        parent_relative = relative.rpartition("/")[0]
        parent = SCAN_INDEX[parent_relative]
        parent["children"] = [item for item in parent["children"]
                              if item["relative"] != relative]
        while True:
            node = SCAN_INDEX[parent_relative]
            node["files"] -= 1
            node["selectable_files"] -= int(identity["local_bytes"] > 0)
            node["local_bytes"] -= identity["local_bytes"]
            if not parent_relative:
                break
            parent_relative = parent_relative.rpartition("/")[0]
        SCAN_STATE["files"] -= 1
        SCAN_STATE["local_bytes"] -= identity["local_bytes"]


def evict_one(relative: str) -> dict:
    """Evict one snapshotted local file; never remove the iCloud item itself."""
    with SCAN_LOCK:
        recorded = SCAN_FILES.get(relative)
        if recorded is None or recorded["local_bytes"] <= 0:
            return {"path": relative, "ok": False, "message": "文件不在可移除快照中"}
        identity = dict(recorded)
    parts = relative.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        return {"path": relative, "ok": False, "message": "路径无效"}
    path = ICLOUD_ROOT.joinpath(*parts)
    try:
        with prevent_materialization():
            current = ICLOUD_ROOT
            for part in parts[:-1]:
                current /= part
                parent_stat = current.lstat()
                if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_flags & SF_DATALESS:
                    raise RuntimeError("父文件夹状态已变化，请重新扫描")
            file_stat = path.lstat()
            if not stat.S_ISREG(file_stat.st_mode):
                raise RuntimeError("文件类型已变化，请重新扫描")
            if file_stat.st_flags & SF_DATALESS:
                forget_local_file(relative, identity)
                return {"path": relative, "ok": True, "status": "already_absent",
                        "bytes": 0, "message": "检查时已无本地副本；未执行移除，请以最新扫描结果为准"}
            if any((file_stat.st_dev != identity["device"],
                    file_stat.st_ino != identity["inode"],
                    file_stat.st_size != identity["size"],
                    file_stat.st_mtime_ns != identity["mtime_ns"])):
                raise RuntimeError("文件在扫描后发生变化，请重新扫描")
        result = subprocess.run(
            ["/usr/bin/sudo", "-u", LOCAL_USER, str(EVICT_HELPER), str(path)],
            capture_output=True, text=True, check=False, timeout=45,
        )
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError:
            response = {"ok": False, "message": result.stderr.strip() or "系统没有返回有效结果"}
        if result.returncode != 0 or not response.get("ok"):
            with prevent_materialization():
                if path.lstat().st_flags & SF_DATALESS:
                    forget_local_file(relative, identity)
                    return {"path": relative, "ok": True, "status": "already_absent",
                            "bytes": 0, "message": "系统状态已更新：当前无本地副本；未执行移除"}
            raise RuntimeError(response.get("message", "系统拒绝移除本地下载"))
        # The system may update the dataless flag shortly after returning.
        with prevent_materialization():
            for _ in range(8):
                if path.lstat().st_flags & SF_DATALESS:
                    forget_local_file(relative, identity)
                    return {"path": relative, "ok": True, "status": "removed",
                            "bytes": identity["local_bytes"],
                            "message": "本地副本已移除，iCloud 文件保留"}
                time.sleep(0.25)
        return {"path": relative, "ok": False,
                "message": "系统接受了请求，但本地状态尚未更新；请稍后重新扫描"}
    except Exception as error:
        return {"path": relative, "ok": False, "message": str(error)}


def run_evictions(paths: list[str]) -> None:
    for relative in paths:
        try:
            outcome = evict_one(relative)
        except Exception as error:
            outcome = {"path": relative, "ok": False,
                       "message": f"处理失败：{error}"}
        with EVICT_LOCK:
            EVICT_STATE["results"].append(outcome)
            EVICT_STATE["done"] += 1
            EVICT_STATE["succeeded"] += int(outcome.get("status") == "removed")
    with EVICT_LOCK:
        EVICT_STATE["phase"] = "complete"


def begin_evictions(scan_id: str, paths: list[str]) -> dict:
    if not EVICT_HELPER.is_file():
        return {"phase": "error", "error": "本地移除工具未编译；请重新运行启动器"}
    if not paths or len(paths) > 200 or len(set(paths)) != len(paths):
        return {"phase": "error", "error": "一次须选择 1–200 个不同文件"}
    with EVICT_LOCK, SCAN_LOCK:
        if EVICT_STATE["phase"] == "running" or SCAN_STATE["phase"] != "complete":
            return {"phase": "error", "error": "扫描或移除操作仍在进行"}
        if scan_id != SCAN_STATE.get("scan_id"):
            return {"phase": "error", "error": "扫描结果已更新，请重新选择"}
        if any(path not in SCAN_FILES or SCAN_FILES[path]["local_bytes"] <= 0
               for path in paths):
            return {"phase": "error", "error": "选择中有未下载或 0 B 项目"}
        EVICT_STATE.clear()
        EVICT_STATE.update(phase="running", total=len(paths), done=0,
                           succeeded=0, results=[])
        threading.Thread(target=run_evictions, args=(paths,), daemon=True).start()
        return {key: value for key, value in EVICT_STATE.items() if key != "results"}


def reveal_in_finder(relative: str) -> dict:
    """Reveal a snapshotted or processed path after an explicit click."""
    with EVICT_LOCK, SCAN_LOCK:
        if SCAN_STATE["phase"] != "complete":
            return {"ok": False, "error": "请先完成本机元数据扫描"}
        folder = SCAN_INDEX.get(relative)
        in_results = any(item["path"] == relative for item in EVICT_STATE["results"])
        if (relative not in SCAN_FILES and
                (folder is None or folder.get("unscanned")) and not in_results):
            return {"ok": False, "error": "路径不在已确认的本机项目中"}
    parts = relative.split("/")
    if not relative or any(part in ("", ".", "..") for part in parts):
        return {"ok": False, "error": "路径无效"}
    path = ICLOUD_ROOT.joinpath(*parts)
    try:
        with prevent_materialization():
            current = ICLOUD_ROOT
            for index, part in enumerate(parts):
                current /= part
                item_stat = current.lstat()
                if item_stat.st_flags & SF_DATALESS and (index < len(parts) - 1 or not in_results):
                    raise RuntimeError("路径当前未在本机，已停止定位")
                if index < len(parts) - 1 and not stat.S_ISDIR(item_stat.st_mode):
                    raise RuntimeError("父路径状态已变化，请重新扫描")
                if index == len(parts) - 1 and not (
                    stat.S_ISDIR(item_stat.st_mode) or stat.S_ISREG(item_stat.st_mode)
                ):
                    raise RuntimeError("项目状态已变化，请重新扫描")
        result = subprocess.run(
            ["/usr/bin/sudo", "-u", LOCAL_USER, "/usr/bin/open", "-R", str(path)],
            capture_output=True, text=True, check=False, timeout=12,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Finder 未能定位此项目")
        return {"ok": True}
    except Exception as error:
        return {"ok": False, "error": str(error)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_json(self, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        global ACTIVE_CLIENTS
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/api/status":
            self.send_json(status())
            return
        if route == "/api/scan/status":
            with SCAN_LOCK:
                self.send_json(dict(SCAN_STATE))
            return
        if route == "/api/evict/status":
            with EVICT_LOCK:
                self.send_json(dict(EVICT_STATE))
            return
        if route == "/api/scan/tree":
            relative = parse_qs(parsed.query).get("path", [""])[0]
            with SCAN_LOCK:
                node = SCAN_INDEX.get(relative) if SCAN_STATE["phase"] == "complete" else None
                if node is None:
                    self.send_json({"error": "扫描尚未完成或文件夹不存在"})
                else:
                    self.send_json({key: value for key, value in node.items() if key != "children"}
                                   | {"children": [
                                       {key: value for key, value in child.items() if key != "children"}
                                       for child in node["children"]
                                   ]})
            return
        if route == "/api/scan/files":
            prefix = parse_qs(parsed.query).get("prefix", [""])[0]
            with SCAN_LOCK:
                if SCAN_STATE["phase"] != "complete" or prefix not in SCAN_INDEX:
                    self.send_json({"error": "扫描尚未完成或文件夹不存在"})
                else:
                    match = [
                        {"path": relative, "bytes": item["local_bytes"]}
                        for relative, item in SCAN_FILES.items()
                        if item["local_bytes"] > 0 and
                        (not prefix or relative.startswith(prefix + "/"))
                    ]
                    self.send_json({"count": len(match),
                                    "files": match if len(match) <= 200 else []})
            return
        if route == "/events":
            ACTIVE_CLIENTS += 1
            start_monitor()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b"event: status\ndata: {\"message\":\"listening\"}\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event = EVENT_QUEUE.get(timeout=12)
                        data = json.dumps(event, ensure_ascii=False).encode("utf-8")
                        self.wfile.write(b"event: event\ndata: " + data + b"\n\n")
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                ACTIVE_CLIENTS = max(0, ACTIVE_CLIENTS - 1)
                if ACTIVE_CLIENTS == 0:
                    stop_monitor()
                    SCAN_CANCEL.set()
            return
        if route in ("/", "/index.html", "/ui.css", "/app.js",
                     "/tree.css", "/tree.js"):
            filename = "index.html" if route == "/" else route.lstrip("/")
            content = (HERE / filename).read_bytes()
            self.send_response(HTTPStatus.OK)
            content_type = {".html": "text/html", ".css": "text/css",
                            ".js": "text/javascript"}[Path(filename).suffix]
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        # A different website must not be able to start a scan or eviction.
        if self.headers.get("Origin") != f"http://{HOST}:{PORT}":
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        route = urlparse(self.path).path
        if route == "/api/scan/start":
            self.send_json(begin_scan())
        elif route == "/api/scan/cancel":
            SCAN_CANCEL.set()
            self.send_json({"phase": "cancelling"})
        elif route == "/api/evict/start":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("请求长度无效")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or not isinstance(data.get("scan_id"), str):
                    raise ValueError("缺少扫描编号")
                paths = data.get("paths")
                if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                    raise ValueError("文件列表无效")
                self.send_json(begin_evictions(data["scan_id"], paths))
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json({"phase": "error", "error": str(error)})
        elif route == "/api/reveal":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("请求长度无效")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or not isinstance(data.get("path"), str):
                    raise ValueError("文件路径无效")
                self.send_json(reveal_in_finder(data["path"]))
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json({"ok": False, "error": str(error)})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("请通过“启动 iCloud Drive Monitor.command”运行，以便取得只读监视权限。")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"iCloud Drive Monitor: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_monitor()
        server.server_close()


if __name__ == "__main__":
    main()
