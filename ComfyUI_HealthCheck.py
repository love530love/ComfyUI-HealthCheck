# ComfyUI_HealthCheck.py
# A lightweight health check plugin for ComfyUI
# Author: love530love
# Version: 1.1.1

import os
import sys
import time
import socket
import threading
import io
import logging
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


# ===== Startup completion state (shared across capture channels) =====
_server_up = False       # HTTP server listening (all node imports finished)
_manager_done = False    # ComfyUI-Manager "All startup tasks have been completed." seen
_manager_expected = False  # Manager is active => expect its completion marker
_last_activity = time.monotonic()  # last time any log byte was observed


def _set_server_up():
    global _server_up
    _server_up = True


def _set_manager_done():
    global _manager_done
    _manager_done = True


def _set_manager_expected():
    global _manager_expected
    _manager_expected = True


def _touch_activity():
    global _last_activity
    _last_activity = time.monotonic()


def _detect_manager_expected():
    """Manager 驱动着插件加载循环，在 HealthCheck 被导入时其包已在 sys.modules；
    也兼容 --enable-manager 启动参数。"""
    if "--enable-manager" in sys.argv:
        return True
    return any(name == "comfyui_manager" or name.startswith("comfyui_manager.")
               for name in sys.modules)


# ===== Log Capture System =====
class LogCapture:
    """Captures ComfyUI startup logs to detect IMPORT FAILED.

    三通道捕获：
    1. logging.Handler.emit 拦截：捕获 logging.info/warning 等调用
    2. stdout/stderr Tee：捕获 print() 和子进程输出
    3. 日志文件 tail（_completion_watcher）：直接读 ComfyUI-Manager 写的
       user/comfyui.log，即使前两条进程内管线被第三方插件破坏也能拿到
       启动完成标记和 IMPORT FAILED 信息

    三路共享同一个 _process_line 状态机。
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
        _touch_activity()

    def _process_line(self, line):
        if "(IMPORT FAILED)" in line or "IMPORT FAILED:" in line:
            self.import_failed_lines.append(line)
        elif "seconds" in line and "custom_nodes" in line and "IMPORT FAILED" not in line:
            self.import_success_lines.append(line)
        elif "Import times for custom nodes:" in line:
            self.import_times_complete = True
        elif "To see the GUI go to:" in line:
            # 服务器开始监听 = 所有插件已导入完毕
            _set_server_up()
        elif "[ComfyUI-Manager] All startup tasks have been completed." in line:
            # ComfyUI-Manager 启动任务全部完成（包括注册表缓存刷新）
            _set_manager_done()
        elif "[START] ComfyUI-Manager" in line:
            _set_manager_expected()

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


def _collect_failed_from_log():
    """Authoritative full-file scan for IMPORT FAILED lines.

    The incremental capture channels (logging emit proxy, stdout tee, log-tail)
    only observe lines written *after* HealthCheck is imported, so failures of
    plugins loaded earlier would be missed. Scanning the whole ComfyUI log file
    at report time guarantees the failed-plugin list is complete.
    """
    try:
        path = _get_log_file_path(_get_comfyui_port())
        if not path or not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return [ln for ln in content.splitlines() if "IMPORT FAILED" in ln.upper()]
    except Exception:
        return []


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

   🔍 ComfyUI HealthCheck v1.1.1
"""

_report_printed = False  # 防止重复输出
_watcher_stop = threading.Event()


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

        # Merge an authoritative full-file scan so early-loaded plugins (whose
        # failures predate HealthCheck's capture window) are not missed.
        for line in _collect_failed_from_log():
            if line not in log_capture.import_failed_lines:
                log_capture.import_failed_lines.append(line)
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
        _watcher_stop.set()
        log_capture.stop()


# ===== Startup completion watcher (v1.1.1) =====
# 设计目标：插件加载一结束就尽快、正确地打印报告，且绝不能让用户感到"久久没打印"。
# 不靠盲等固定秒数，而是用"多保险信号 + 输出沉降检测"。
#
# 判定"插件加载已结束"的三重独立信号（三保险 / defense in depth）：
#   A) HTTP 端口探测：服务器开始监听 => 所有插件已导入（完全不依赖日志，最稳）
#   B) 日志出现 "Import times for custom nodes:" 这一行
#   C) 日志出现 "[ComfyUI-Manager] All startup tasks have been completed."
# 只要 A 或 B 为真，即可确定每个自定义节点都已导入完毕，所有 IMPORT FAILED
# 行都已落盘。此时进入"沉降观察"：当启动输出已安静 SETTLE_SECONDS（无新日志字节，
# 说明缓冲区已冲刷、不会有更晚的导入错误还在到达）即打印；但无论输出是否持续流动，
# 最多只等 MAX_GRACE_SECONDS。这样既不等盲等的 300s 静默期，也不会在日志持续输出时无限拖。
#   - C 是冗余的"迟到的兜底"：若前面都没触发，它一出现就立即打印。
#   - HARD_CAP_SECONDS 是"启动卡死"探测器（自进程启动起算，不是逐轮盲等）：
#     真·启动卡死时至少也能打印一份诊断。
# 打印位置：紧跟插件列表 / 服务器启动之后，而非被 Manager 静默刷新注册表拖到很后面。
HARD_CAP_SECONDS = 600            # 绝对兜底：启动疑似卡死，强制打印
SETTLE_SECONDS = 3               # 导入结束后输出安静这么久 => 冲刷完毕，可打印
MAX_GRACE_SECONDS = 20           # 但导入结束后最多只等这么久（输出持续流动时）
POLL_INTERVAL = 5.0

_log_tail = {"path": None, "pos": 0}


def _get_comfyui_port():
    try:
        from comfy.cli_args import args
        return args.port
    except Exception:
        pass
    try:
        if "--port" in sys.argv:
            return int(sys.argv[sys.argv.index("--port") + 1])
    except Exception:
        pass
    return 8188


def _get_log_file_path(port):
    """ComfyUI-Manager 的日志文件：默认 user/comfyui.log，指定 --port 时为
    user/comfyui_{port}.log（见 Manager prestartup_script.py 的命名规则）。"""
    try:
        import folder_paths
        user_dir = folder_paths.get_user_directory()
    except Exception:
        return None
    base = os.path.join(user_dir, "comfyui")
    if port != 8188 and os.path.exists(f"{base}_{port}.log"):
        return f"{base}_{port}.log"
    return f"{base}.log"


def _probe_port(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _tail_log_file():
    """增量读取 Manager 写的日志文件，喂给状态机。
    这条通道不经过进程内 logging 管线，即使 handler.emit 链被
    第三方插件破坏也能拿到启动完成标记和 IMPORT FAILED 信息。"""
    path = _log_tail["path"]
    if not path:
        return
    try:
        size = os.path.getsize(path)
        pos = _log_tail["pos"]
        if size < pos:
            pos = 0  # 日志被轮转/截断，从头读
        if size == pos:
            return
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(pos)
            data = f.read()
        _log_tail["pos"] = size
        if data:
            log_capture._feed(data)
    except Exception:
        pass


def _completion_watcher():
    global _manager_expected
    port = _get_comfyui_port()
    path = _get_log_file_path(port)
    _log_tail["path"] = path
    try:
        _log_tail["pos"] = os.path.getsize(path) if path and os.path.exists(path) else 0
    except Exception:
        _log_tail["pos"] = 0

    if _detect_manager_expected():
        _set_manager_expected()

    start = time.monotonic()
    trigger_time = None
    while not _watcher_stop.wait(POLL_INTERVAL):
        if _report_printed:
            return
        _tail_log_file()
        if not _server_up and _probe_port(port):
            _set_server_up()
        now = time.monotonic()

        # 1) Startup-stall detector (absolute last resort: time since process start).
        if now - start > HARD_CAP_SECONDS:
            print_report()
            return

        # 2) Redundant late safety: Manager finished every startup task.
        if _manager_done:
            print_report()
            return

        # 3) Plugin loading finished? (三保险: port probe OR import-times line)
        #    Either signal means every custom node has been imported, so all
        #    IMPORT FAILED lines are already in the log.
        if _server_up or log_capture.import_times_complete:
            if trigger_time is None:
                trigger_time = now
            # Print once the startup output has settled — no new log bytes for
            # SETTLE_SECONDS proves buffers are flushed and no late import error
            # is still arriving...
            settled = (now - _last_activity) >= SETTLE_SECONDS
            # ...but never wait longer than MAX_GRACE_SECONDS after imports done,
            # even if output keeps flowing.
            grace_expired = (now - trigger_time) >= MAX_GRACE_SECONDS
            if settled or grace_expired:
                print_report()
                return


# ===== Initialization =====
log_capture.start()
threading.Thread(target=_completion_watcher, daemon=True).start()
