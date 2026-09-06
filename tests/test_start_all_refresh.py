from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import start_all


class _FakeProcess:
    def __init__(
        self,
        pid: int,
        *,
        cwd: Path,
        command: list[str],
        parents: list["_FakeProcess"] | None = None,
    ) -> None:
        self.pid = pid
        self._cwd = cwd
        self._command = command
        self._parents = parents or []

    def cwd(self) -> str:
        return str(self._cwd)

    def cmdline(self) -> list[str]:
        return self._command

    def parents(self) -> list["_FakeProcess"]:
        return self._parents


def test_service_ownership_requires_project_cwd_and_expected_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(start_all, "ROOT", str(tmp_path))
    backend = _FakeProcess(
        101, cwd=tmp_path, command=["python.exe", "run_backend.py"]
    )
    unrelated = _FakeProcess(
        102, cwd=tmp_path, command=["python.exe", "another_server.py"]
    )
    wrong_directory = _FakeProcess(
        103, cwd=tmp_path / "other", command=["python.exe", "run_backend.py"]
    )

    assert start_all._is_expected_project_service(backend, 8000) is True
    assert start_all._is_expected_project_service(unrelated, 8000) is False
    assert start_all._is_expected_project_service(wrong_directory, 8000) is False


def test_project_launcher_ancestor_is_selected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(start_all, "ROOT", str(tmp_path))
    monkeypatch.setattr(start_all.os, "getpid", lambda: 999)
    launcher = _FakeProcess(
        201, cwd=tmp_path, command=["python.exe", "scripts/start_all.py"]
    )
    wrapper = _FakeProcess(
        202, cwd=tmp_path, command=["python.exe", "run_backend.py"],
        parents=[launcher],
    )

    assert start_all._project_launcher_ancestor(wrapper) is launcher


def test_refresh_refuses_to_stop_an_unverified_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(start_all, "ROOT", str(tmp_path))
    listener = _FakeProcess(
        301, cwd=tmp_path, command=["python.exe", "unrelated.py"]
    )
    monkeypatch.setattr(start_all, "_listening_processes", lambda: {8000: listener})
    stopped: list[int] = []
    monkeypatch.setattr(
        start_all, "_terminate_process_tree", lambda process: stopped.append(process.pid)
    )

    with pytest.raises(RuntimeError, match="refusing to stop"):
        start_all.refresh_existing_services()
    assert stopped == []


def test_refresh_stops_one_shared_old_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(start_all, "ROOT", str(tmp_path))
    launcher = _FakeProcess(
        401, cwd=tmp_path, command=["python.exe", "scripts/start_all.py"]
    )
    backend = _FakeProcess(
        402, cwd=tmp_path, command=["python.exe", "run_backend.py"]
    )
    bridge = _FakeProcess(
        403,
        cwd=tmp_path,
        command=["python.exe", "workers.revit_bridge.main:app"],
    )
    monkeypatch.setattr(
        start_all, "_listening_processes", lambda: {8000: backend, 8005: bridge}
    )
    monkeypatch.setattr(start_all, "_is_expected_project_service", lambda *_: True)
    monkeypatch.setattr(start_all, "_project_launcher_ancestor", lambda _: launcher)
    monkeypatch.setattr(start_all, "port_open", lambda *_args, **_kwargs: False)
    stopped: list[int] = []
    monkeypatch.setattr(
        start_all, "_terminate_process_tree", lambda process: stopped.append(process.pid)
    )

    start_all.refresh_existing_services()
    assert stopped == [launcher.pid]


def test_refresh_preserves_vite_when_only_python_services_are_reloaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(start_all, "ROOT", str(tmp_path))
    launcher = _FakeProcess(
        501, cwd=tmp_path, command=["python.exe", "scripts/start_all.py"]
    )
    frontend = _FakeProcess(
        502, cwd=tmp_path / "frontend", command=["node.exe", "vite"]
    )
    backend = _FakeProcess(
        503, cwd=tmp_path, command=["python.exe", "run_backend.py"],
        parents=[launcher],
    )
    monkeypatch.setattr(
        start_all, "_listening_processes",
        lambda: {3000: frontend, 8000: backend},
    )
    monkeypatch.setattr(start_all, "_is_expected_project_service", lambda *_: True)
    monkeypatch.setattr(start_all, "_project_launcher_ancestor", lambda item: launcher if item is backend else None)
    monkeypatch.setattr(start_all, "port_open", lambda *_args, **_kwargs: False)
    trees: list[int] = []
    roots: list[int] = []
    monkeypatch.setattr(start_all, "_terminate_process_tree", lambda process: trees.append(process.pid))
    monkeypatch.setattr(start_all, "_terminate_process_only", lambda process: roots.append(process.pid))

    assert start_all.refresh_existing_services(preserve_frontend=True) is True
    assert trees == [backend.pid]
    assert roots == [launcher.pid]


def test_code_signature_changes_when_managed_source_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(start_all, "ROOT", str(tmp_path))
    source_dir = tmp_path / "backend"
    source_dir.mkdir()
    source = source_dir / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    first = start_all._code_signature()

    source.write_text("VALUE = 200\n", encoding="utf-8")
    second = start_all._code_signature()

    assert first != second


def test_startup_waits_until_managed_service_ports_are_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = iter([False, True])
    monkeypatch.setattr(start_all, "port_open", lambda *_args, **_kwargs: next(attempts))
    monkeypatch.setattr(start_all.time, "sleep", lambda *_args: None)

    start_all._wait_for_service_ports([
        SimpleNamespace(port=8000, label="backend"),
    ], timeout_seconds=1)


def test_service_stop_terminates_real_listener_and_venv_wrapper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    wrapper = SimpleNamespace(pid=701)
    listener = _FakeProcess(
        702, cwd=tmp_path, command=["python.exe", "run_backend.py"],
    )
    monkeypatch.setattr(start_all, "ROOT", str(tmp_path))
    monkeypatch.setattr(start_all, "_listening_processes", lambda: {8000: listener})
    monkeypatch.setattr(start_all.psutil, "Process", lambda pid: _FakeProcess(
        pid, cwd=tmp_path, command=["python.exe", "run_backend.py"],
    ))
    stopped: list[int] = []
    monkeypatch.setattr(
        start_all, "_terminate_process_tree", lambda process: stopped.append(process.pid),
    )
    service = start_all.Service.__new__(start_all.Service)
    service.label = "backend"
    service.port = 8000
    service.proc = wrapper

    service.stop()

    assert stopped == [listener.pid, wrapper.pid]
    assert service.proc is None


def test_restart_keeps_healthy_services_when_one_member_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    services = [
        SimpleNamespace(
            label="backend", port=8000, restarts=0,
            stop=lambda: events.append("stop-backend"),
            start=lambda: events.append("start-backend"),
        ),
        SimpleNamespace(
            label="bridge", port=8005, restarts=0,
            stop=lambda: events.append("stop-bridge"),
            start=lambda: events.append("start-bridge"),
        ),
    ]
    monkeypatch.setattr(start_all, "_wait_for_ports_closed", lambda *_: None)
    monkeypatch.setattr(
        start_all, "_wait_for_service_ports",
        lambda *_: (_ for _ in ()).throw(RuntimeError("bridge not ready")),
    )

    assert start_all._restart_services(services) is False
    assert events == ["stop-backend", "stop-bridge", "start-backend", "start-bridge"]
    assert [service.restarts for service in services] == [1, 1]


def test_invalid_python_snapshot_does_not_replace_running_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        start_all.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stderr="SyntaxError: unmatched '}'", stdout="",
        ),
    )

    assert start_all._managed_python_sources_are_valid() is False
