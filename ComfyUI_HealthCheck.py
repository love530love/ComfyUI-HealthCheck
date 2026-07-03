# ComfyUI_HealthCheck.py
# A lightweight health check plugin for ComfyUI
# Author: love530love
# Version: 1.0.9

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
    """Captures ComfyUI startup logs to detect IMPORT FAILED.

    双通道捕获：
    1. logging.Handler.emit 拦截：捕获 logging.info/warning 等调用，
       这是 v0.27.0+ ComfyUI 实际使用的输出方式（StreamHandler → sys.stderr）
    2. stdout/stderr Tee：兜底，捕获 print() 和子进程输出

    两路共享同一个 _process_line 状态机，保证新/旧版 ComfyUI 都能触发报告。
    """

    def __init__(self):
        self.captured = io.StringIO()
        self.import_failed_lines = []
        self.import_success_lines = []
        self.import_times_complete = False  # 标记是否完成导入统计
        self._lock = threading.Lock()
        self._line_buffer = ""
        # 记录我们安装/替换的对象，便于 stop() 时还原
        self._installed_handlers = []   # [(handler, original_emit)]
        self._stdout_proxy = None
        self._stderr_proxy = None
        self._orig_stdout = None
        self._orig_stderr = None

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
            # 旧版兼容：触发延迟报告（延迟更长以覆盖新版额外输出）
            trigger_delayed_report(10.0)
        elif "To see the GUI go to:" in line:
            # 新版 ComfyUI：服务器启动完成，触发延迟报告
            # 延迟 15 秒确保覆盖后续异步插件初始化（DEPRECATION WARNING、
            # web 资源加载、ComfyUI-Manager 缓存更新等）
            # trigger_delayed_report 会自动取消之前的 timer，
            # 所以即使 Import times 先触发，最终也会以这个更晚的触发为准
            trigger_delayed_report(15.0)
        elif "[ComfyUI-Manager] All startup tasks have been completed." in line:
            # ComfyUI-Manager 启动完成，几乎立即输出
            trigger_delayed_report(0.5)

    def _feed(self, text):
        """把任意字符串喂给状态机，按行切分触发 _process_line。"""
        with self._lock:
            self._append_capture(text)
            self._line_buffer += text
            if "\n" in self._line_buffer:
                lines = self._line_buffer.split("\n")
                self._line_buffer = lines[-1]
                for line in lines[:-1]:
                    self._process_line(line)

    @staticmethod
    def _write_original(stream, data):
        try:
            stream.write(data)
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "utf-8"
            safe_data = data.encode(encoding, errors="replace").decode(encoding)
            stream.write(safe_data)

    def _make_emit_proxy(self, handler):
        """为 handler.emit 创建一个代理，先调用原 emit 写终端，
        再把格式化后的消息喂给 _feed。"""
        original_emit = handler.emit
        capture = self

        def emit_proxy(record):
            try:
                original_emit(record)
            except Exception:
                # 原 emit 失败不影响我们自己的逻辑
                pass
            try:
                msg = handler.format(record) if handler.formatter else record.getMessage()
                capture._feed(msg + "\n")
            except Exception:
                pass

        return emit_proxy, original_emit

    def _patch_existing_handlers(self):
        """给所有已存在的 logging handler 替换 emit。"""
        root = logging.getLogger()
        for handler in root.handlers:
            proxy, original = self._make_emit_proxy(handler)
            handler.emit = proxy
            self._installed_handlers.append((handler, original))

    def _install_handler_watcher(self):
        """启动一个后台线程，周期性扫描新加入的 logging handler，
        把它们的 emit 也包一层。ComfyUI 可能在加载过程中动态添加 handler。"""
        capture = self
        stop_event = threading.Event()

        def watcher():
            seen = set(id(h) for h in logging.getLogger().handlers)
            while not stop_event.wait(0.5):
                current = list(logging.getLogger().handlers)
                for h in current:
                    if id(h) in seen:
                        continue
                    seen.add(id(h))
                    if h in [hh for hh, _ in capture._installed_handlers]:
                        continue
                    proxy, original = capture._make_emit_proxy(h)
                    h.emit = proxy
                    capture._installed_handlers.append((h, original))

        thread = threading.Thread(target=watcher, daemon=True)
        thread.start()
        return stop_event, thread

    def start(self):
        # 1. 拦截 logging.Handler.emit（v0.27.0+ 核心通道）
        self._patch_existing_handlers()
        self._watcher_stop, self._watcher_thread = self._install_handler_watcher()

        # 2. 兜底：拦截 stdout/stderr（兼容旧版、子进程输出、print()）
        capture = self
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr

        class TeeIO:
            def __init__(self, orig, stream_type):
                self._orig = orig
                self.stream_type = stream_type

            def write(self, data):
                try:
                    capture._write_original(self._orig, data)
                except Exception:
                    pass
                try:
                    capture._feed(data)
                except Exception:
                    pass

            def flush(self):
                try:
                    self._orig.flush()
                except Exception:
                    pass

            def isatty(self):
                return False

        self._stdout_proxy = TeeIO(self._orig_stdout, "stdout")
        self._stderr_proxy = TeeIO(self._orig_stderr, "stderr")
        sys.stdout = self._stdout_proxy
        sys.stderr = self._stderr_proxy

    def stop(self):
        # 1. 还原 logging handler.emit
        for handler, original_emit in self._installed_handlers:
            try:
                handler.emit = original_emit
            except Exception:
                pass
        self._installed_handlers.clear()

        # 2. 停止 handler watcher
        if getattr(self, "_watcher_stop", None) is not None:
            self._watcher_stop.set()

        # 3. 还原 stdout/stderr
        if sys.stdout is self._stdout_proxy:
            sys.stdout = self._orig_stdout
        if sys.stderr is self._stderr_proxy:
            sys.stderr = self._orig_stderr
        self._stdout_proxy = None
        self._stderr_proxy = None
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
        normalized = line.strip()
        if "IMPORT FAILED" not in normalized.upper():
            continue

        # 格式：IMPORT FAILED: <full_path_to_plugin>
        # 找到 "IMPORT FAILED" 之后的内容
        idx = normalized.upper().find("IMPORT FAILED")
        if idx == -1:
            continue
        rest = normalized[idx + len("IMPORT FAILED"):].strip().lstrip(":)")

        # 在路径中找 custom_nodes/ 或 custom_nodes\
        path_part = rest.replace("\\", "/").lower()
        marker = "custom_nodes/"
        if marker in path_part:
            after = rest.replace("\\", "/")[path_part.find(marker) + len(marker):]
            plugin_name = after.split("/")[0].strip().rstrip(")")
            if plugin_name and plugin_name not in failed:
                failed.append(plugin_name)

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

   🔍 ComfyUI HealthCheck v1.0.9
"""

_report_printed = False  # 防止重复输出
_report_timer = None     # 当前待执行的报告 timer


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


def trigger_delayed_report(delay=5.0):
    """在检测到导入完成后触发报告，支持自定义延迟
    
    每次调用会取消之前的 timer，确保只有最后一个触发点生效。
    例如：Import times (10s) → To see the GUI (15s)，最终只会执行 15s 的那个。
    """
    global _report_timer
    if _report_timer is not None:
        _report_timer.cancel()
    _report_timer = start_daemon_timer(delay, print_report)


# ===== Initialization =====
log_capture.start()


# 备用：如果 60 秒内没有检测到导入完成标记，强制输出
def backup_timer():
    if not _report_printed:
        print("[HealthCheck] Backup timer triggered...")
        print_report()


start_daemon_timer(60.0, backup_timer)
