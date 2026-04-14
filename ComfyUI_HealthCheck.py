# ComfyUI_HealthCheck.py
# A lightweight health check plugin for ComfyUI
# Author: love530love
# Version: 1.0.2

import os
import sys
import threading
import io
from pathlib import Path
from datetime import datetime


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
        self.captured = io.StringIO()
        self.import_failed_lines = []
        self.import_success_lines = []
        self.import_times_complete = False  # 标记是否完成导入统计

    def start(self):
        capture = self

        class TeeIO:
            def __init__(self, stream_type):
                self.stream_type = stream_type
                self.buffer = ""

            def write(self, data):
                # Write to original stream
                if self.stream_type == 'stdout':
                    capture.original_stdout.write(data)
                    capture.original_stdout.flush()
                else:
                    capture.original_stderr.write(data)
                    capture.original_stderr.flush()

                # Write to capture buffer
                capture.captured.write(data)

                # Real-time detection
                self.buffer += data
                if "\n" in self.buffer:
                    lines = self.buffer.split("\n")
                    self.buffer = lines[-1]
                    for line in lines[:-1]:
                        if "(IMPORT FAILED)" in line:
                            capture.import_failed_lines.append(line)
                        elif "seconds" in line and "custom_nodes" in line and "IMPORT FAILED" not in line:
                            capture.import_success_lines.append(line)
                        # 检测导入完成标记
                        elif "Import times for custom nodes:" in line:
                            capture.import_times_complete = True
                            # 触发延迟报告（再等待几秒确保完全完成）
                            trigger_delayed_report()

            def flush(self):
                if self.stream_type == 'stdout':
                    capture.original_stdout.flush()
                else:
                    capture.original_stderr.flush()

            def isatty(self):
                return False

        sys.stdout = TeeIO('stdout')
        sys.stderr = TeeIO('stderr')

    def stop(self):
        sys.stdout = self.original_stdout
        sys.stderr = self.original_stderr
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
    for line in log_lines:
        if "(IMPORT FAILED)" in line:
            parts = line.replace("\\", "/").split("/")
            for i, part in enumerate(parts):
                if part == "custom_nodes" and i + 1 < len(parts):
                    plugin_name = parts[i + 1].strip()
                    plugin_name = plugin_name.split()[0] if ' ' in plugin_name else plugin_name
                    if plugin_name and plugin_name not in failed:
                        failed.append(plugin_name)
                    break
    return failed


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

   🔍 ComfyUI HealthCheck v1.0.3
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
        print(f"{WHITE}📦 Total Plugins: {BOLD}{total}{RESET} {GRAY}(folders: {folders}, .py: {pyfiles}){RESET}")
        print(f"{GREEN}✅ Successful: {BOLD}{success_count}{RESET}")
        print(f"{RED}❌ Failed: {BOLD}{failed_count}{RESET}")
        print(f"{YELLOW}📊 Health: {BOLD}{health:.1f}%{RESET}")
        print(f"{WHITE}🧠 Node Classes: {BOLD}{node_count}{RESET}")

        if failed_plugins:
            print(f"\n{RED}🚨 Failed Plugins:{RESET}")
            for plugin in failed_plugins[:20]:
                full_path = custom_nodes_dir / plugin
                print(f"{RED}   ✗ {plugin}{RESET}")
                print(f"{GRAY}     └─ {full_path}{RESET}")
            if len(failed_plugins) > 20:
                print(f"{RED}   ... and {len(failed_plugins) - 20} more{RESET}")
        else:
            print(f"\n{GREEN}🎉 All plugins loaded successfully!{RESET}")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{GRAY}Checked at: {timestamp}{RESET}")
        print(f"{CYAN}{'=' * 60}{RESET}\n")

    except Exception as e:
        print(f"\n[HealthCheck] Report generation failed: {e}")
        import traceback
        traceback.print_exc()


def trigger_delayed_report():
    """在检测到导入完成后触发报告"""
    # 再延迟 5 秒确保所有节点注册完成
    threading.Timer(5.0, print_report).start()


# ===== Initialization =====
log_capture.start()


# 备用：如果 60 秒内没有检测到导入完成标记，强制输出
def backup_timer():
    if not _report_printed:
        print("[HealthCheck] Backup timer triggered...")
        print_report()


threading.Timer(60.0, backup_timer).start()