# ComfyUI_HealthCheck.py
# A lightweight health check plugin for ComfyUI
# Author: love530love
# Version: 1.0.6

import os
import sys
import threading
import io
import logging
import re
from pathlib import Path
from datetime import datetime

MAX_CAPTURE_CHARS = 200_000


# ===== Dummy Node Definition (Avoid IMPORT FAILED) =====
class HealthCheckDummyNode:
    """Placeholder node to prevent ComfyUI marking this file as failed"""
    CATEGORY = "utils"
    FUNCTION = "execute"
    RETURN_TYPES = ()

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    def execute(self):
        return ()


NODE_CLASS_MAPPINGS = {
    "HealthCheckDummy": HealthCheckDummyNode,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "HealthCheckDummy": "Health Check (Internal)",
}


# ===== Log Capture System =====
class LogCapture:
    """Captures ComfyUI startup logs to detect IMPORT FAILED"""

    def __init__(self):
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        self.original_handler_streams = {}
        self.captured = io.StringIO()
        self.import_failed_lines = []
        self.import_success_lines = []
        self.import_times_complete = False  # 标记是否完成导入统计
        self._lock = threading.Lock()
        self.stdout_proxy = None
        self.stderr_proxy = None

    def _append_capture(self, data):
        self.captured.write(data)
        if self.captured.tell() > MAX_CAPTURE_CHARS:
            value = self.captured.getvalue()[-MAX_CAPTURE_CHARS:]
            self.captured.seek(0)
            self.captured.truncate(0)
            self.captured.write(value)

    def _process_line(self, line):
        if "(IMPORT FAILED)" in line or "IMPORT FAILED:" in line:
            self.import_failed_lines.append(line)
        elif "seconds" in line and "custom_nodes" in line and "IMPORT FAILED" not in line:
            self.import_success_lines.append(line)
        elif "Import times for custom nodes:" in line:
            self.import_times_complete = True
            # 触发延迟报告（再等待几秒确保完全完成）
            trigger_delayed_report()

    @staticmethod
    def _write_original(stream, data):
        try:
            stream.write(data)
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "utf-8"
            safe_data = data.encode(encoding, errors="replace").decode(encoding)
            stream.write(safe_data)

    def start(self):
        capture = self

        class TeeIO:
            def __init__(self, stream_type):
                self.stream_type = stream_type
                self.buffer = ""

            def write(self, data):
                # Write to original stream
                if self.stream_type == 'stdout':
                    capture._write_original(capture.original_stdout, data)
                else:
                    capture._write_original(capture.original_stderr, data)

                # Real-time detection
                with capture._lock:
                    # Write to capture buffer
                    capture._append_capture(data)
                    self.buffer += data
                    if "\n" in self.buffer:
                        lines = self.buffer.split("\n")
                        self.buffer = lines[-1]
                        for line in lines[:-1]:
                            capture._process_line(line)

            def flush(self):
                if self.stream_type == 'stdout':
                    capture.original_stdout.flush()
                else:
                    capture.original_stderr.flush()

            def isatty(self):
                return False

        self.stdout_proxy = TeeIO('stdout')
        self.stderr_proxy = TeeIO('stderr')
        sys.stdout = self.stdout_proxy
        sys.stderr = self.stderr_proxy

        # ComfyUI configures logging before custom nodes are imported, so
        # existing handlers keep pointing at the old streams unless updated.
        for handler in logging.getLogger().handlers:
            stream = getattr(handler, "stream", None)
            if stream is capture.original_stdout:
                capture.original_handler_streams[handler] = stream
                handler.stream = sys.stdout
            elif stream is capture.original_stderr:
                capture.original_handler_streams[handler] = stream
                handler.stream = sys.stderr

    def stop(self):
        for handler, stream in self.original_handler_streams.items():
            handler.stream = stream
        self.original_handler_streams.clear()
        if sys.stdout is self.stdout_proxy:
            sys.stdout = self.original_stdout
        if sys.stderr is self.stderr_proxy:
            sys.stderr = self.original_stderr
        self.stdout_proxy = None
        self.stderr_proxy = None
        return self.captured.getvalue()


# Global capture instance
log_capture = LogCapture()


# ===== Statistics Functions =====
def count_plugins():
    """Count plugins in parent directory (custom_nodes), not current directory"""
    base = Path(__file__).resolve().parent.parent
    total = 0
    folders = 0
    pyfiles = 0

    for item in base.iterdir():
        if item.name.startswith("__"):
            continue
        if item.is_dir():
            total += 1
            folders += 1
        elif item.suffix == ".py":
            total += 1
            pyfiles += 1

    return total, folders, pyfiles


def get_node_count():
    try:
        import nodes
        return len(nodes.NODE_CLASS_MAPPINGS)
    except:
        return -1


def extract_failed_plugins(log_lines):
    """Extract failed plugin names from captured log lines"""
    failed = []
    path_pattern = re.compile(
        r"(?:(?:\(\s*IMPORT FAILED\s*\))|(?:IMPORT FAILED:))\s*:?\s*(.+custom_nodes[\\/][^\\/\s:]+)",
        re.IGNORECASE,
    )

    for line in log_lines:
        normalized = line.strip()
        if "IMPORT FAILED" not in normalized:
            continue

        match = path_pattern.search(normalized)
        path_text = match.group(1) if match else normalized
        path_text = path_text.strip().strip("'\"")
        parts = path_text.replace("\\", "/").split("/")

        for i, part in enumerate(parts):
            if part == "custom_nodes" and i + 1 < len(parts):
                plugin_name = parts[i + 1].strip()
                if plugin_name and plugin_name not in failed:
                    failed.append(plugin_name)
                break
    return failed


def extract_plugin_name_from_path(path_text):
    """Extract the plugin folder/file name after custom_nodes from a path."""
    cleaned = path_text.strip().strip("'\"")
    parts = cleaned.replace("\\", "/").split("/")
    for index, part in enumerate(parts):
        if part == "custom_nodes" and index + 1 < len(parts):
            return parts[index + 1].strip()
    return None


# ===== Report Output =====
BANNER = r"""
 ██████╗   ██████╗   ███╗   ███╗  ███████╗  ██╗   ██╗  ██╗  ██╗  ██╗
██╔════╝  ██╔═══██╗  ████╗ ████║  ██╔════╝  ╚██╗ ██╔╝  ██║  ██║  ██║
██║       ██║   ██║  ██╔████╔██║  █████╗     ╚████╔╝   ██║  ██║  ██║
██║       ██║   ██║  ██║╚██╔╝██║  ██╔══╝      ╚██╔╝    ██║  ██║  ██║
╚██████╗  ╚██████╔╝  ██║ ╚═╝ ██║  ██║          ██║     ╚████╔╝   ██║ 
 ╚═════╝   ╚═════╝   ╚═╝     ╚═╝  ╚═╝          ╚═╝      ╚═══╝    ╚═╝   

██╗  ██╗  ███████╗   █████╗   ██╗    ████████╗  ██╗  ██╗   ██████╗  ██╗  ██╗  ███████╗   ██████╗  ██╗  ██╗
██║  ██║  ██╔════╝  ██╔══██╗  ██║    ╚══██╔══╝  ██║  ██║  ██╔════╝  ██║  ██║  ██╔════╝  ██╔════╝  ██║ ██╔╝
███████║  █████╗    ███████║  ██║       ██║     ███████║  ██║       ███████║  █████╗    ██║       █████╔╝ 
██╔══██║  ██╔══╝    ██╔══██║  ██║       ██║     ██╔══██║  ██║       ██╔══██║  ██╔══╝    ██║       ██╔═██╗ 
██║  ██║  ███████╗  ██║  ██║  ███████╗  ██║     ██║  ██║  ╚██████╗  ██║  ██║  ███████╗  ╚██████╗  ██║  ██╗
╚═╝  ╚═╝  ╚══════╝  ╚═╝  ╚═╝  ╚══════╝  ╚═╝     ╚═╝  ╚═╝   ╚═════╝  ╚═╝  ╚═╝  ╚══════╝   ╚═════╝  ╚═╝  ╚═╝

   🔍 ComfyUI HealthCheck v1.0.6
"""

_report_printed = False  # 防止重复输出


def print_report():
    """Generate and print health report"""
    global _report_printed
    if _report_printed:
        return
    _report_printed = True

    try:
        custom_nodes_dir = Path(__file__).resolve().parent.parent
        total, folders, pyfiles = count_plugins()
        node_count = get_node_count()

        failed_plugins = extract_failed_plugins(log_capture.import_failed_lines)

        failed_count = len(failed_plugins)
        success_count = total - failed_count

        health = (success_count / total * 100) if total else 0

        # Color output
        CYAN = "\033[96m"
        GREEN = "\033[92m"
        RED = "\033[91m"
        YELLOW = "\033[93m"
        WHITE = "\033[97m"
        GRAY = "\033[90m"
        BOLD = "\033[1m"
        RESET = "\033[0m"

        # 添加空行与其他输出分隔
        print(f"\n\n{CYAN}{'=' * 60}{RESET}")
        print(f"{CYAN}{BANNER}{RESET}")
        print(f"{CYAN}{'=' * 60}{RESET}")
        print(f"{BOLD}{'🚀 ComfyUI Plugin Health Report':^56}{RESET}")
        print(f"{CYAN}{'=' * 60}{RESET}")
        print(f"{WHITE}📦 扫描到已有的插件数/Total Plugins: {BOLD}{total}{RESET} {GRAY}(其中 插件文件夹数/folders: {folders}, 单独以 .py 形式存在的插件数/.py: {pyfiles}){RESET}")
        print(f"{GREEN}✅ 已成功加载的插件数/Successful: {BOLD}{success_count}{RESET}")
        print(f"{RED}❌ 加载失败需要排查原因的插件数/Failed: {BOLD}{failed_count}{RESET}")
        print(f"{YELLOW}📊 健康度/Health: {BOLD}{health:.1f}%{RESET}")
        print(f"{WHITE}🧠 已成功扫描到的节点数/Node Classes: {BOLD}{node_count}{RESET}")

        if failed_plugins:
            print(f"\n{RED}🚨 加载失败的插件/Failed Plugins:{RESET}")
            for plugin in failed_plugins[:20]:
                full_path = custom_nodes_dir / plugin
                print(f"{RED}   ✗ {plugin}{RESET}")
                print(f"{GRAY}     └─ {full_path}{RESET}")
            if len(failed_plugins) > 20:
                print(f"{RED}   ... 还有/and {len(failed_plugins) - 20} more{RESET}")
            print(f"\n{YELLOW}💡 排查提示/Troubleshooting Hint:{RESET}")
            print(f"{YELLOW}   请查看上方启动日志中的 Traceback、Cannot import、ModuleNotFoundError、ImportError 等关键词。{RESET}")
            print(f"{YELLOW}   Search the startup log above for Traceback, Cannot import, ModuleNotFoundError, or ImportError.{RESET}")
        else:
            print(f"\n{GREEN}🎉 所有插件加载成功！/All plugins loaded successfully!{RESET}")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{GRAY}检测时间戳/Checked at: {timestamp}{RESET}")
        print(f"{CYAN}{'=' * 60}{RESET}\n")

    except Exception as e:
        print(f"\n[HealthCheck] Report generation failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        log_capture.stop()


def start_daemon_timer(delay, callback):
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()
    return timer


def trigger_delayed_report():
    """在检测到导入完成后触发报告"""
    # 再延迟 5 秒确保所有节点注册完成
    start_daemon_timer(5.0, print_report)


# ===== Initialization =====
log_capture.start()


# 备用：如果 60 秒内没有检测到导入完成标记，强制输出
def backup_timer():
    if not _report_printed:
        print("[HealthCheck] Backup timer triggered...")
        print_report()


start_daemon_timer(60.0, backup_timer)
