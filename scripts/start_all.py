"""一键启动 BuildMate Demo（后端 / MCP×3 / Revit Bridge / 前端 + 自动守护）

用法：python scripts/start_all.py        # 启动全部并守护（挂了自动拉起）
      Ctrl+C                             # 优雅停止全部

服务：
  - 后端 8000（run_backend.py——Windows Selector 事件循环，psycopg 兼容）
  - MCP 8003（ifc_parser_server——IFC 解析）
  - MCP 8004（drawing_perception_server——图纸感知）
  - MCP 8002（web_search_server——外部资料检索）
  - Revit Bridge 8005（Windows/Revit 2020 写入与回读）
  - 前端 3000（Vite dev）
守护：每 5 秒检查端口——退出/无响应自动重启
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil

# Windows terminals commonly use GBK; keep the one-click launcher alive when
# its status messages contain Chinese text or service symbols.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_LOG_DIR = os.path.join(ROOT, "data", "runtime", "logs")
os.makedirs(RUNTIME_LOG_DIR, exist_ok=True)
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable
NPM = "npm.cmd" if sys.platform == "win32" else "npm"
SERVICE_COMMAND_MARKERS = {
    3000: ("vite",),
    8000: ("run_backend.py",),
    8002: ("web_search_server.py",),
    8003: ("ifc_parser_server.py",),
    8004: ("backend.mcp.drawing_perception_server",),
    8005: ("workers.revit_bridge.main:app",),
}

# The local launcher is intentionally a process supervisor rather than a
# development hot-reloader: the backend uses a Windows Selector event loop
# and cannot safely be started with Uvicorn's reload subprocess.  Track only
# source/configuration files (never runtime artifacts) and restart the managed
# Python services when their imported modules change.  This prevents a task
# from being handled by a process that still has an older Pydantic contract in
# memory, such as the pre-beams WallModel schema.
CODE_WATCH_ROOTS = (
    "backend",
    "workers",
    "scripts",
    "run_backend.py",
)
CODE_WATCH_SUFFIXES = {".py", ".toml", ".yaml", ".yml"}


def _code_signature() -> tuple[int, int, int]:
    """Return a cheap change token for files loaded by managed services."""

    file_count = 0
    latest_mtime_ns = 0
    total_size = 0
    for relative_root in CODE_WATCH_ROOTS:
        root = Path(ROOT, relative_root)
        if root.is_file():
            candidates = (root,)
        elif root.is_dir():
            candidates = root.rglob("*")
        else:
            continue
        for path in candidates:
            if not path.is_file() or path.suffix.lower() not in CODE_WATCH_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            file_count += 1
            latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
            total_size += stat.st_size
    return file_count, latest_mtime_ns, total_size


def port_open(port: int, timeout: float = 1.0) -> bool:
    """检查端口是否可连接（兼容 IPv4/IPv6——如 Vite 只监听 ::1）"""
    for host in ("127.0.0.1", "::1", "localhost"):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _normalized_path(value: str) -> str:
    return os.path.normcase(os.path.realpath(value or ""))


def _process_snapshot(process: psutil.Process) -> tuple[str, str]:
    """Return a stable cwd/command snapshot for ownership checks."""

    try:
        cwd = _normalized_path(process.cwd())
        command = " ".join(process.cmdline()).casefold()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
        raise RuntimeError(
            f"cannot inspect process {getattr(process, 'pid', '?')}: {exc}"
        ) from exc
    return cwd, command


def _is_expected_project_service(process: psutil.Process, port: int) -> bool:
    """Only authorize termination of a known BuildMate service process."""

    markers = SERVICE_COMMAND_MARKERS.get(port)
    if not markers:
        return False
    cwd, command = _process_snapshot(process)
    expected_cwd = _normalized_path(
        os.path.join(ROOT, "frontend") if port == 3000 else ROOT
    )
    return cwd == expected_cwd and all(marker.casefold() in command for marker in markers)


def _project_launcher_ancestor(process: psutil.Process) -> psutil.Process | None:
    """Find the old BuildMate watchdog that owns a listening service."""
    # A listener can disappear between net_connections() and this lookup
    # (especially while the watchdog is refreshing services).  Do not turn
    # that normal race into a failed one-click startup.
    candidates = [process]
    try:
        candidates.extend(process.parents())
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        # Keep any process snapshot already obtained; ownership validation
        # below remains fail-closed when the full ancestor chain is unavailable.
        pass
    for candidate in candidates:
        if candidate.pid == os.getpid():
            continue
        try:
            cwd, command = _process_snapshot(candidate)
        except RuntimeError:
            continue
        if cwd == _normalized_path(ROOT) and "scripts/start_all.py" in command:
            return candidate
    return None


def _listening_processes() -> dict[int, psutil.Process]:
    listeners: dict[int, psutil.Process] = {}
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError) as exc:
        raise RuntimeError(f"cannot inspect local service ports: {exc}") from exc
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        port = int(connection.laddr.port)
        if port not in SERVICE_COMMAND_MARKERS or not connection.pid:
            continue
        listeners.setdefault(port, psutil.Process(connection.pid))
    return listeners


def _terminate_process_only(process: psutil.Process) -> None:
    """Stop one verified process without touching its children.

    The Vite process is intentionally kept alive when the Python services are
    refreshed.  The launcher owns several sibling services, so killing its
    whole tree would unnecessarily tear down the frontend and lose the
    browser's hot-reload/session state.
    """
    try:
        process.terminate()
    except psutil.NoSuchProcess:
        return
    _gone, alive = psutil.wait_procs([process], timeout=8)
    for item in alive:
        try:
            item.kill()
        except psutil.NoSuchProcess:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=5)


def _terminate_process_tree(process: psutil.Process) -> None:
    """Stop one verified launcher/service tree and wait until it is gone."""

    try:
        processes = [*process.children(recursive=True), process]
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        processes = [process]
    unique = {item.pid: item for item in processes if item.pid != os.getpid()}
    # ``processes`` is children-first.  Stop descendants before their
    # launcher so a venv shim cannot orphan the real uv-managed Python
    # process that owns the listening socket.
    for item in unique.values():
        try:
            item.terminate()
        except psutil.NoSuchProcess:
            pass
    _gone, alive = psutil.wait_procs(list(unique.values()), timeout=8)
    for item in alive:
        try:
            item.kill()
        except psutil.NoSuchProcess:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=5)


def refresh_existing_services(*, preserve_frontend: bool = False) -> bool:
    """Replace stale BuildMate services instead of silently adopting them.

    A second one-click launch used to see occupied ports and reuse processes
    that had imported older Python modules.  Verify every listener first, then
    stop its old watchdog (or the individual service when launched manually).
    An unrelated process on a BuildMate port is reported and never killed.
    """

    listeners = _listening_processes()
    if not listeners:
        print("  = 未发现旧 BuildMate 服务")
        return False
    conflicts: list[str] = []
    for port, process in sorted(listeners.items()):
        try:
            expected = _is_expected_project_service(process, port)
        except RuntimeError as exc:
            conflicts.append(f":{port} pid={process.pid} ({exc})")
            continue
        if not expected:
            conflicts.append(f":{port} pid={process.pid}")
    if conflicts:
        raise RuntimeError(
            "BuildMate ports are occupied by unverified processes; refusing to stop them: "
            + ", ".join(conflicts)
        )

    frontend_preserved = bool(preserve_frontend and 3000 in listeners)
    if preserve_frontend:
        # Stop only Python services first.  Their old watchdog is then
        # stopped as a single process, leaving Vite alive on :3000.
        for port, process in sorted(listeners.items()):
            if port != 3000:
                _terminate_process_tree(process)
        roots: dict[int, psutil.Process] = {}
        for port, process in listeners.items():
            if port == 3000:
                continue
            root = _project_launcher_ancestor(process) or process
            roots[root.pid] = root
        for process in roots.values():
            _terminate_process_only(process)
    else:
        roots = {}
        for process in listeners.values():
            root = _project_launcher_ancestor(process) or process
            roots[root.pid] = root
        for process in roots.values():
            _terminate_process_tree(process)

    deadline = time.time() + 15.0
    ports_to_refresh = sorted(
        port for port in listeners
        if not (preserve_frontend and port == 3000)
    )
    occupied = ports_to_refresh
    while time.time() < deadline:
        occupied = [port for port in ports_to_refresh if port_open(port, timeout=0.2)]
        if not occupied:
            if frontend_preserved:
                print("  ✓ 后端/MCP/Bridge 已停止；保留 Vite 前端，不刷新浏览器")
            else:
                print("  ✓ 旧 BuildMate 服务已全部停止，将加载当前代码")
            return frontend_preserved
        time.sleep(0.25)
    raise RuntimeError(
        "old BuildMate services did not release ports: "
        + ", ".join(str(port) for port in occupied)
    )


class Service:
    def __init__(self, name: str, label: str, cmd: list, cwd: str, port: int | None):
        self.name, self.label, self.cmd, self.cwd, self.port = name, label, cmd, cwd, port
        self.proc: subprocess.Popen | None = None
        self.logf = open(os.path.join(RUNTIME_LOG_DIR, f"{name}.log"), "a", encoding="utf-8")
        self.restarts = 0
        self.last_start = 0.0

    def start(self):
        if self.port:
            # A Windows child can keep the listener for a short period after
            # taskkill returns.  Give that verified replacement a bounded
            # grace period instead of crashing the supervisor on a transient
            # race.  We still fail closed when an unrelated process keeps the
            # port occupied.
            deadline = time.time() + 20.0
            while port_open(self.port, timeout=0.2):
                if self.proc is not None and self.proc.poll() is None:
                    return
                if time.time() >= deadline:
                    raise RuntimeError(
                        f"{self.label} 端口 {self.port} 在刷新后仍被占用，拒绝复用未知旧进程"
                    )
                time.sleep(0.25)
        try:
            self.proc = subprocess.Popen(
                self.cmd, cwd=self.cwd, stdout=self.logf, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0)
            self.last_start = time.time()
            print(f"  ▶ {self.label} 启动 (pid={self.proc.pid})")
        except Exception as ex:
            print(f"  ✗ {self.label} 启动失败: {ex}")
            self.proc = None

    def alive(self) -> bool:
        # On Windows the venv launcher can hand the actual server to a child
        # process.  In that case ``Popen.poll()`` may report an exited wrapper
        # even though the service is healthy and its port is serving traffic.
        # Check the service endpoint first to avoid spawning duplicate servers
        # every watchdog interval (and the resulting port-conflict storm).
        if self.port and port_open(self.port):
            return True
        if self.proc is None or self.proc.poll() is not None:
            return False
        # 启动宽限 20 秒：只查进程存活（防误杀启动慢的服务——如 Vite）
        if time.time() - self.last_start < 20:
            return True
        return self.port is None or port_open(self.port)

    def stop(self):
        # A Windows venv launcher starts the real interpreter as a child.  In
        # some environments taskkill cannot terminate that child, while the
        # wrapper has already exited; the socket then survives forever and the
        # browser is left talking to stale code.  Resolve the verified listener
        # as well as the Popen wrapper and stop both with psutil.
        listener: psutil.Process | None = None
        if self.port is not None:
            try:
                candidate = _listening_processes().get(self.port)
                if candidate is not None and _is_expected_project_service(candidate, self.port):
                    listener = candidate
            except RuntimeError as exc:
                print(f"  ⚠ {self.label} 无法核验监听进程：{exc}")
        wrapper = None
        if self.proc is not None:
            try:
                wrapper = psutil.Process(self.proc.pid)
            except psutil.NoSuchProcess:
                wrapper = None
        targets = {item.pid: item for item in (listener, wrapper) if item is not None}
        for process in targets.values():
            _terminate_process_tree(process)
        self.proc = None
        print(f"  ■ {self.label} 已停止")


def _wait_for_service_ports(services: list[Service], timeout_seconds: float = 45.0) -> None:
    """Do not expose the frontend until its local dependencies are ready."""

    pending = [service for service in services if service.port is not None]
    deadline = time.time() + timeout_seconds
    while pending and time.time() < deadline:
        pending = [
            service for service in pending
            if service.port is not None and not port_open(service.port, timeout=0.2)
        ]
        if pending:
            time.sleep(0.25)
    if pending:
        raise RuntimeError(
            "services did not become ready: "
            + ", ".join(service.label for service in pending)
        )


def _wait_for_ports_closed(services: list[Service], timeout_seconds: float = 30.0) -> None:
    """Wait for listeners to disappear before starting a replacement.

    On Windows ``taskkill`` returns before a child Uvicorn process has closed
    its socket.  Starting the replacement immediately then raises a port
    conflict and used to terminate the supervisor itself.  Waiting here keeps
    the browser/frontend alive while the managed backend services are replaced.
    """

    pending = [service for service in services if service.port is not None]
    deadline = time.time() + timeout_seconds
    while pending and time.time() < deadline:
        pending = [
            service for service in pending
            if service.port is not None and port_open(service.port, timeout=0.2)
        ]
        if pending:
            time.sleep(0.25)
    if pending:
        raise RuntimeError(
            "services did not release ports: "
            + ", ".join(service.label for service in pending)
        )


def _restart_services(services: list[Service]) -> bool:
    """Restart a group without allowing a transient port race to kill the watchdog."""

    for service in services:
        service.stop()
    _wait_for_ports_closed(services)
    for service in services:
        service.start()
        service.restarts += 1
    try:
        _wait_for_service_ports(services)
    except Exception as exc:
        # Healthy services are already running the same source revision.  Keep
        # them available (especially the QA API) and let the watchdog retry
        # only the unavailable member instead of tearing the whole group down.
        print(f"  ✗ 部分服务刷新未就绪：{exc}；健康服务继续运行")
        return False
    return True


def _managed_python_sources_are_valid() -> bool:
    """Compile managed sources before replacing a healthy backend.

    A half-written module used to take every service down during hot refresh.
    Compileall does not import application modules or execute business code;
    it only prevents a syntactically invalid revision from replacing the last
    working process set.
    """

    command = [
        PY, "-m", "compileall", "-q",
        os.path.join(ROOT, "backend"),
        os.path.join(ROOT, "workers"),
        os.path.join(ROOT, "scripts"),
        os.path.join(ROOT, "run_backend.py"),
    ]
    try:
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  ✗ 后端代码检查失败：{exc}；继续保留当前服务")
        return False
    if result.returncode == 0:
        return True
    detail = (result.stderr or result.stdout or "未知语法错误").strip()
    print(f"  ✗ 新后端代码未通过语法检查，当前服务不刷新：\n{detail}")
    return False


def main():
    do_seed = "--seed" in sys.argv  # 默认跳过知识库灌入（已灌过——演示要快）
    print("=" * 56)
    print(" BuildMate 一键启动（初始化 + 全服务 + 自动守护）")
    print("=" * 56)

    print("⓪ 刷新旧服务进程...")
    frontend_preserved = refresh_existing_services(preserve_frontend=True)

    print("① 初始化数据库...")
    subprocess.run([PY, "scripts/init_db.py"], cwd=ROOT, check=False)

    if do_seed:
        print("② 灌入知识库（RAG）...")
        subprocess.run([PY, "scripts/seed_knowledge.py"], cwd=ROOT, check=False)
        print("②.5 抓取真实建材价格（失败不阻塞）...")
        try:
            subprocess.run([PY, "scripts/fetch_material_prices.py", "--force"],
                           cwd=ROOT, timeout=120)
        except Exception as e:
            print(f"  ⚠ 价格抓取跳过：{e}")
    else:
        print("② 跳过知识库灌入（已灌过——需要重建加 --seed）")

    services = [
        Service("backend", "后端 API   :8000",
                [PY, "run_backend.py"], ROOT, 8000),
        Service("mcp8002", "MCP 搜索   :8002",
                [PY, os.path.join("backend", "mcp", "web_search_server.py")], ROOT, 8002),
        Service("mcp8003", "MCP IFC    :8003",
                [PY, os.path.join("backend", "mcp", "ifc_parser_server.py")], ROOT, 8003),
        Service("mcp8004", "MCP 感知   :8004",
                [PY, "-m", "backend.mcp.drawing_perception_server"], ROOT, 8004),
        Service("revit_bridge", "Revit Bridge:8005",
                [PY, "-m", "uvicorn", "workers.revit_bridge.main:app",
                 "--host", "127.0.0.1", "--port", "8005"], ROOT, 8005),
        Service("frontend", "前端 Vite  :3000",
                [NPM, "run", "dev"], os.path.join(ROOT, "frontend"), 3000),
    ]
    print("③ 启动服务...")
    python_services = [service for service in services if service.name != "frontend"]
    frontend_service = next(service for service in services if service.name == "frontend")
    for service in python_services:
        service.start()
    _wait_for_service_ports(python_services)
    if frontend_preserved:
        print("  ✓ 前端 Vite 复用现有进程 :3000")
    else:
        frontend_service.start()

    try:
        last_status = 0
        code_signature = _code_signature()
        while True:
            time.sleep(5)
            current_signature = _code_signature()
            if current_signature != code_signature:
                if not _managed_python_sources_are_valid():
                    # Do not compile the same rejected snapshot every five
                    # seconds.  A real file change creates a new signature and
                    # triggers another validation attempt.
                    code_signature = current_signature
                    continue
                print("  ↻ 检测到后端代码/合同变化，刷新 Python 服务...")
                # Vite already handles frontend source updates.  Restarting
                # the Python services together keeps API, worker, MCP and
                # Revit Bridge contracts on the same revision.
                try:
                    _restart_services(python_services)
                    code_signature = current_signature
                except Exception as exc:
                    # Port release failures are transient on Windows.  Keep
                    # the old signature so the next watchdog tick retries.
                    print(f"  ✗ 服务刷新未完成：{exc}；保留守护进程并稍后重试")
                    continue
            for s in services:
                if not s.alive():
                    reason = (f"退出(code={s.proc.returncode})" if s.proc and s.proc.poll() is not None
                              else "端口无响应")
                    print(f"  ⚠ {s.label} {reason}——自动重启...")
                    s.stop()
                    try:
                        s.start()
                        s.restarts += 1
                    except Exception as exc:
                        # A short Windows socket/process race must not end the
                        # supervisor.  The next watchdog tick retries the
                        # service after Service.start's bounded port wait.
                        print(f"  ✗ {s.label} 重启未完成：{exc}；稍后自动重试")
                        continue
            now = int(time.time())
            if now - last_status >= 30:
                last_status = now
                states = " ".join(f"{s.name}={'OK' if s.alive() else 'DOWN'}"
                                  for s in services)
                print(f"  [状态] {states}")
    except KeyboardInterrupt:
        print("\n  ⏹ 停止全部服务...")
        for s in services:
            s.stop()
    print("Done.")


if __name__ == "__main__":
    main()
