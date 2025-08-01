import io
import os
import re
import subprocess
import sys
import tempfile
import time
import logging
from collections import Counter, defaultdict
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Dict, List, Tuple
from unittest.mock import Mock, MagicMock, patch

import colorama
import pytest

import ray
from ray._common.test_utils import wait_for_condition
from ray._private import ray_constants
from ray._private.ray_constants import (
    PROCESS_TYPE_DASHBOARD,
    PROCESS_TYPE_DASHBOARD_AGENT,
    PROCESS_TYPE_GCS_SERVER,
    PROCESS_TYPE_LOG_MONITOR,
    PROCESS_TYPE_MONITOR,
    PROCESS_TYPE_PYTHON_CORE_WORKER,
    PROCESS_TYPE_PYTHON_CORE_WORKER_DRIVER,
    PROCESS_TYPE_RAYLET,
    PROCESS_TYPE_RAY_CLIENT_SERVER,
    PROCESS_TYPE_REAPER,
    PROCESS_TYPE_REDIS_SERVER,
    PROCESS_TYPE_RUNTIME_ENV_AGENT,
    PROCESS_TYPE_WORKER,
)
from ray._private.log_monitor import (
    LOG_NAME_UPDATE_INTERVAL_S,
    RAY_LOG_MONITOR_MANY_FILES_THRESHOLD,
    LogFileInfo,
    LogMonitor,
    is_proc_alive,
)
from ray._private.test_utils import (
    get_log_batch,
    get_log_message,
    get_log_data,
    init_log_pubsub,
    run_string_as_driver,
)
from ray.cross_language import java_actor_class
from ray.autoscaler._private.cli_logger import cli_logger
from ray._private.worker import print_worker_logs


def set_logging_config(monkeypatch, max_bytes, backup_count):
    monkeypatch.setenv("RAY_ROTATION_MAX_BYTES", str(max_bytes))
    monkeypatch.setenv("RAY_ROTATION_BACKUP_COUNT", str(backup_count))


def test_reopen_changed_inode(tmp_path):
    """Make sure that when we reopen a file because the inode has changed, we
    open to the right location."""

    path1 = tmp_path / "file"
    path2 = tmp_path / "changed_file"

    with open(path1, "w") as f:
        for i in range(1000):
            print(f"{i}", file=f)

    with open(path2, "w") as f:
        for i in range(2000):
            print(f"{i}", file=f)

    file_info = LogFileInfo(
        filename=path1,
        size_when_last_opened=0,
        file_position=0,
        file_handle=None,
        is_err_file=False,
        job_id=None,
        worker_pid=None,
    )

    file_info.reopen_if_necessary()
    for _ in range(1000):
        file_info.file_handle.readline()

    orig_file_pos = file_info.file_handle.tell()
    file_info.file_position = orig_file_pos

    # NOTE: On windows, an open file can't be deleted.
    file_info.file_handle.close()
    os.remove(path1)
    os.rename(path2, path1)

    file_info.reopen_if_necessary()

    assert file_info.file_position == orig_file_pos
    assert file_info.file_handle.tell() == orig_file_pos


@pytest.mark.skipif(sys.platform == "win32", reason="Fails on windows")
def test_deleted_file_does_not_throw_error(tmp_path):
    filename = tmp_path / "file"

    Path(filename).touch()

    file_info = LogFileInfo(
        filename=filename,
        size_when_last_opened=0,
        file_position=0,
        file_handle=None,
        is_err_file=False,
        job_id=None,
        worker_pid=None,
    )

    file_info.reopen_if_necessary()

    os.remove(filename)

    file_info.reopen_if_necessary()


def test_log_rotation_config(ray_start_cluster, monkeypatch):
    cluster = ray_start_cluster
    max_bytes = 100
    backup_count = 3

    # Create a cluster.
    set_logging_config(monkeypatch, max_bytes, backup_count)
    head_node = cluster.add_node(num_cpus=0)
    # Set a different env var for a worker node.
    set_logging_config(monkeypatch, 0, 0)
    worker_node = cluster.add_node(num_cpus=0)
    cluster.wait_for_nodes()

    config = head_node.logging_config
    assert config["log_rotation_max_bytes"] == max_bytes
    assert config["log_rotation_backup_count"] == backup_count
    config = worker_node.logging_config
    assert config["log_rotation_max_bytes"] == 0
    assert config["log_rotation_backup_count"] == 0


def test_log_files_exist(shutdown_only):
    """Verify all log files exist as specified in
    https://docs.ray.io/en/master/ray-observability/user-guides/configure-logging.html#logging-directory-structure # noqa
    """
    ray.init(num_cpus=1)
    session_dir = ray._private.worker.global_worker.node.address_info["session_dir"]
    session_path = Path(session_dir)
    log_dir_path = session_path / "logs"

    # Run a no-op task to ensure all logs are created.
    # Use a runtime_env to ensure that the agents are alive.
    @ray.remote(runtime_env={"env_vars": {"FOO": "BAR"}})
    def f() -> Tuple[str, int]:
        return ray.get_runtime_context().get_worker_id(), os.getpid()

    driver_id, driver_pid = (ray.get_runtime_context().get_worker_id(), os.getpid())
    worker_id, worker_pid = ray.get(f.remote())

    python_core_driver_filename = (
        PROCESS_TYPE_PYTHON_CORE_WORKER_DRIVER + f"-{driver_id}_{driver_pid}"
    )
    python_core_worker_filename = (
        PROCESS_TYPE_PYTHON_CORE_WORKER + f"-{worker_id}_{worker_pid}"
    )
    job_id = ray.get_runtime_context().get_job_id()
    worker_filename = PROCESS_TYPE_WORKER + f"-{worker_id}-{job_id}-{worker_pid}"

    component_to_extensions = [
        (PROCESS_TYPE_DASHBOARD, [".log", ".out", ".err"]),
        (PROCESS_TYPE_DASHBOARD_AGENT, [".log"]),
        (PROCESS_TYPE_GCS_SERVER, [".out", ".err"]),
        (PROCESS_TYPE_LOG_MONITOR, [".log", ".err"]),
        (PROCESS_TYPE_MONITOR, [".log", ".out", ".err"]),
        (PROCESS_TYPE_RAYLET, [".out", ".err"]),
        (PROCESS_TYPE_RUNTIME_ENV_AGENT, [".log", ".out", ".err"]),
        (python_core_driver_filename, [".log"]),
        (python_core_worker_filename, [".log"]),
        (worker_filename, [".out", ".err"]),
    ]

    paths = list(log_dir_path.iterdir())

    def _assert_component_logs_exist(
        paths: List[str], component_name: str, extensions: List[str]
    ):
        extensions_to_find = set(extensions)
        for path in paths:
            if path.stem != component_name:
                continue

            if path.suffix in extensions_to_find:
                extensions_to_find.remove(path.suffix)

        assert len(extensions_to_find) == 0, (
            f"Missing extensions {(extensions_to_find)} for component '{component_name}'. "
            f"All paths: {paths}."
        )

    for (component_name, extensions) in component_to_extensions:
        _assert_component_logs_exist(paths, component_name, extensions)


# Rotation is disable in the unit test.
def test_log_rotation_disable_rotation_params(shutdown_only, monkeypatch):
    max_bytes = 0
    backup_count = 1
    set_logging_config(monkeypatch, max_bytes, backup_count)
    ray.init(num_cpus=1)
    session_dir = ray._private.worker.global_worker.node.address_info["session_dir"]
    session_path = Path(session_dir)
    log_dir_path = session_path / "logs"

    # NOTE: There's no PROCESS_TYPE_WORKER because "worker" is a
    # substring of "python-core-worker".
    log_rotating_components = [
        PROCESS_TYPE_DASHBOARD,
        PROCESS_TYPE_DASHBOARD_AGENT,
        PROCESS_TYPE_LOG_MONITOR,
        PROCESS_TYPE_MONITOR,
        PROCESS_TYPE_PYTHON_CORE_WORKER_DRIVER,
        PROCESS_TYPE_PYTHON_CORE_WORKER,
        PROCESS_TYPE_RAYLET,
        PROCESS_TYPE_GCS_SERVER,
    ]

    # Run the basic workload.
    @ray.remote
    def f():
        for i in range(10):
            print(f"test {i}")

    # Create a runtime env to make sure dashboard agent is alive.
    ray.get(f.options(runtime_env={"env_vars": {"A": "a", "B": "b"}}).remote())

    # Filter out only paths that end in .log, .log.1, (which is produced by python
    # rotating log handler) and .out.1 and so on (which is produced by C++ spdlog
    # rotation handler) . etc. These paths are handled by the logger; the others (.err)
    # are not.
    paths = []
    for path in log_dir_path.iterdir():
        # Match all rotated files, which suffixes with `log.x` or `log.x.out`.
        if re.search(r".*\.log(\.\d+)?", str(path)):
            paths.append(path)
        elif re.search(r".*\.out(\.\d+)?", str(path)):
            paths.append(path)

    def component_exist(component, paths):
        """Return whether there's at least one log file path is for the given
        [component]."""
        for path in paths:
            filename = path.stem
            if component in filename:
                return True
        return False

    for component in log_rotating_components:
        assert component_exist(component, paths), paths

    # Check if the backup count is respected.
    file_cnts = defaultdict(int)
    for path in paths:
        filename = path.name
        parts = filename.split(".")
        if len(parts) == 3:
            filename_without_suffix = parts[0]
            file_type = parts[1]  # eg. err, log, out
            file_cnts[f"{filename_without_suffix}.{file_type}"] += 1
    for filename, file_cnt in file_cnts.items():
        assert file_cnt == backup_count, (
            f"{filename} has files that are more than "
            f"backup count {backup_count}, file count: {file_cnt}"
        )

    # Test application log, which starts with `worker-`.
    # Should be tested separately with other components since "worker" is a substring of "python-core-worker".
    #
    # Check file count.
    application_stdout_paths = []
    for path in paths:
        if (
            path.stem.startswith("worker-")
            and re.search(r".*\.out(\.\d+)?", str(path))
            and path.stat().st_size > 0
        ):
            application_stdout_paths.append(path)
    assert len(application_stdout_paths) == 1, application_stdout_paths


@pytest.mark.skipif(
    sys.platform == "win32", reason="Log rotation is disable on windows platform."
)
def test_log_rotation(shutdown_only, monkeypatch):
    max_bytes = 1
    backup_count = 3
    set_logging_config(monkeypatch, max_bytes, backup_count)
    ray.init(num_cpus=1)
    session_dir = ray._private.worker.global_worker.node.address_info["session_dir"]
    session_path = Path(session_dir)
    log_dir_path = session_path / "logs"

    # NOTE: There's no PROCESS_TYPE_WORKER because "worker" is a
    # substring of "python-core-worker".
    log_rotating_components = [
        PROCESS_TYPE_DASHBOARD,
        PROCESS_TYPE_DASHBOARD_AGENT,
        PROCESS_TYPE_LOG_MONITOR,
        PROCESS_TYPE_MONITOR,
        PROCESS_TYPE_PYTHON_CORE_WORKER_DRIVER,
        PROCESS_TYPE_PYTHON_CORE_WORKER,
        PROCESS_TYPE_RAYLET,
        PROCESS_TYPE_GCS_SERVER,
    ]

    # Run the basic workload.
    @ray.remote
    def f():
        for i in range(10):
            print(f"test {i}")

    # Create a runtime env to make sure dashboard agent is alive.
    ray.get(f.options(runtime_env={"env_vars": {"A": "a", "B": "b"}}).remote())

    # Filter out only paths that end in .log, .log.1, (which is produced by python
    # rotating log handler) and .out.1 and so on (which is produced by C++ spdlog
    # rotation handler) . etc. These paths are handled by the logger; the others (.err)
    # are not.
    paths = []
    for path in log_dir_path.iterdir():
        # Match all rotated files, which suffixes with `log.x` or `log.x.out`.
        if re.search(r".*\.log(\.\d+)?", str(path)):
            paths.append(path)
        elif re.search(r".*\.out(\.\d+)?", str(path)):
            paths.append(path)

    def component_exist(component, paths):
        """Return whether there's at least one log file path is for the given
        [component]."""
        for path in paths:
            filename = path.stem
            if component in filename:
                return True
        return False

    def component_file_only_one_log_entry(component):
        """Since max_bytes is 1, the log file should
        only have at most one log entry.
        """
        for path in paths:
            if not component_exist(component, [path]):
                continue

            with open(path) as file:
                found = False
                for line in file:
                    if re.match(r"^\[?\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d", line):
                        if found:
                            return False
                        found = True
        return True

    for component in log_rotating_components:
        assert component_exist(component, paths), paths
        assert component_file_only_one_log_entry(component)

    # Check if the backup count is respected.
    file_cnts = defaultdict(int)
    for path in paths:
        filename = path.name
        parts = filename.split(".")
        if len(parts) == 3:
            filename_without_suffix = parts[0]
            file_type = parts[1]  # eg. err, log, out
            file_cnts[f"{filename_without_suffix}.{file_type}"] += 1
    for filename, file_cnt in file_cnts.items():
        assert file_cnt <= backup_count, (
            f"{filename} has files that are more than "
            f"backup count {backup_count}, file count: {file_cnt}"
        )

    # Test application log, which starts with `worker-`.
    # Should be tested separately with other components since "worker" is a substring of "python-core-worker".
    #
    # Check file count.
    application_stdout_paths = []
    for path in paths:
        if (
            path.stem.startswith("worker-")
            and re.search(r".*\.out(\.\d+)?", str(path))
            and path.stat().st_size > 0
        ):
            application_stdout_paths.append(path)
    assert len(application_stdout_paths) == 4, application_stdout_paths

    # Check file content, each file should have one line.
    for cur_path in application_stdout_paths:
        with cur_path.open() as f:
            lines = f.readlines()
            assert len(lines) == 1, lines


def test_periodic_event_stats(shutdown_only):
    ray.init(
        num_cpus=1,
        _system_config={"event_stats_print_interval_ms": 100, "event_stats": True},
    )
    session_dir = ray._private.worker.global_worker.node.address_info["session_dir"]
    session_path = Path(session_dir)
    log_dir_path = session_path / "logs"

    # Run the basic workload.
    @ray.remote
    def f():
        pass

    ray.get(f.remote())

    paths = list(log_dir_path.iterdir())

    def is_event_loop_stats_found(path):
        found = False
        with open(path) as f:
            event_loop_stats_identifier = "Event stats"
            for line in f.readlines():
                if event_loop_stats_identifier in line:
                    found = True
        return found

    for path in paths:
        # Need to remove suffix to avoid reading log rotated files.
        if "python-core-driver" in str(path):
            wait_for_condition(lambda: is_event_loop_stats_found(path))
        if "raylet.out" in str(path):
            wait_for_condition(lambda: is_event_loop_stats_found(path))
        if "gcs_server.out" in str(path):
            wait_for_condition(lambda: is_event_loop_stats_found(path))


def test_worker_id_names(shutdown_only):
    ray.init(
        num_cpus=1,
        _system_config={"event_stats_print_interval_ms": 100, "event_stats": True},
    )
    session_dir = ray._private.worker.global_worker.node.address_info["session_dir"]
    session_path = Path(session_dir)
    log_dir_path = session_path / "logs"

    # Run the basic workload.
    @ray.remote
    def f():
        print("hello")

    ray.get(f.remote())

    paths = list(log_dir_path.iterdir())
    worker_log_files = list()
    ids = []
    for path in paths:
        if "python-core-worker" in str(path):
            pattern = ".*-([a-f0-9]*).*"
        elif "worker" in str(path):
            pattern = ".*worker-([a-f0-9]*)-.*-.*"
        else:
            continue
        worker_id = re.match(pattern, str(path)).group(1)
        worker_log_files.append(str(paths))
        ids.append(worker_id)
    counts = Counter(ids).values()
    for count in counts:
        # For each worker, there should be a "python-core-.*.log", "worker-.*.out",
        # and "worker-.*.err".
        assert count == 3, worker_log_files


def test_log_pid_with_hex_job_id(ray_start_cluster):
    cluster = ray_start_cluster
    cluster.add_node(num_cpus=4)

    def submit_job():
        # Connect a driver to the Ray cluster.
        ray.init(address=cluster.address, ignore_reinit_error=True)
        p = init_log_pubsub()
        # It always prints the monitor messages.
        logs = get_log_message(p, 1)

        @ray.remote
        def f():
            print("remote func")

        ray.get(f.remote())

        def matcher(log_batch):
            return log_batch["task_name"] == "f"

        logs = get_log_batch(p, 1, matcher=matcher)
        # It should logs with pid of hex job id instead of None
        assert logs[0]["pid"] is not None
        ray.shutdown()

    # NOTE(xychu): loop ten times to make job id from 01000000 to 0a000000,
    #              in order to trigger hex pattern
    for _ in range(10):
        submit_job()


def test_ignore_windows_access_violation(ray_start_regular_shared):
    @ray.remote
    def print_msg():
        print("Windows fatal exception: access violation\n")

    @ray.remote
    def print_after(_obj):
        print("done")

    p = init_log_pubsub()
    print_after.remote(print_msg.remote())
    msgs = get_log_message(
        p, num=6, timeout=10, job_id=ray.get_runtime_context().get_job_id()
    )

    assert len(msgs) == 1, msgs
    assert msgs[0][0] == "done"


def test_log_redirect_to_stderr(shutdown_only):
    log_components = {
        PROCESS_TYPE_DASHBOARD: "Starting dashboard metrics server on port",
        PROCESS_TYPE_DASHBOARD_AGENT: "Dashboard agent grpc address",
        PROCESS_TYPE_RUNTIME_ENV_AGENT: "Starting runtime env agent",
        PROCESS_TYPE_GCS_SERVER: "Loading job table data",
        # No log monitor output if all components are writing to stderr.
        PROCESS_TYPE_LOG_MONITOR: "",
        PROCESS_TYPE_MONITOR: "Starting monitor using ray installation",
        PROCESS_TYPE_PYTHON_CORE_WORKER: "worker server started",
        PROCESS_TYPE_PYTHON_CORE_WORKER_DRIVER: "driver server started",
        # TODO(Clark): Add coverage for Ray Client.
        # PROCESS_TYPE_RAY_CLIENT_SERVER: "Starting Ray Client server",
        PROCESS_TYPE_RAY_CLIENT_SERVER: "",
        PROCESS_TYPE_RAYLET: "Starting object store with directory",
        # No reaper process run (kernel fate-sharing).
        PROCESS_TYPE_REAPER: "",
        # Unused.
        PROCESS_TYPE_WORKER: "",
    }

    script = """
import os
from pathlib import Path

import ray

os.environ["RAY_LOG_TO_STDERR"] = "1"
ray.init()

session_dir = ray._private.worker.global_worker.node.address_info["session_dir"]
session_path = Path(session_dir)
log_dir_path = session_path / "logs"

# Run the basic workload.
@ray.remote
def f():
    for i in range(10):
        print(f"test {{i}}")

ray.get(f.remote())

log_component_names = {}

# Confirm that no log files are created for any of the components.
paths = list(path.stem for path in log_dir_path.iterdir())
assert set(log_component_names).isdisjoint(set(paths)), paths
""".format(
        str(list(log_components.keys()))
    )
    stderr = run_string_as_driver(script)

    # Make sure that the expected startup log records for each of the
    # components appears in the stderr stream.
    for component, canonical_record in log_components.items():
        if not canonical_record:
            # Process not run or doesn't generate logs; skip.
            continue
        assert canonical_record in stderr, stderr
        if component == PROCESS_TYPE_REDIS_SERVER:
            # Redis doesn't expose hooks for custom log formats, so we aren't able to
            # inject the Redis server component name into the log records.
            continue
        # NOTE: We do a prefix match instead of including the enclosing right
        # parentheses since some components, like the core driver and worker, add a
        # unique ID suffix.
        assert f"({component}" in stderr, stderr


def test_custom_logging_format(shutdown_only):
    script = """
import ray
ray.init(logging_format='custom logging format - %(message)s')
"""
    stderr = run_string_as_driver(script)
    assert "custom logging format - " in stderr


def test_segfault_stack_trace(ray_start_cluster, capsys):
    @ray.remote
    def f():
        import ctypes

        ctypes.string_at(0)

    with pytest.raises(
        ray.exceptions.WorkerCrashedError, match="The worker died unexpectedly"
    ):
        ray.get(f.remote())

    stderr = capsys.readouterr().err
    assert (
        "*** SIGSEGV received at" in stderr
    ), f"C++ stack trace not found in stderr: {stderr}"
    assert (
        "Fatal Python error: Segmentation fault" in stderr
    ), f"Python stack trace not found in stderr: {stderr}"


@pytest.mark.skipif(
    sys.platform == "win32" or sys.platform == "darwin",
    reason="TODO(simon): Failing on Windows and OSX.",
)
def test_log_java_worker_logs(shutdown_only, capsys):
    tmp_dir = tempfile.mkdtemp()
    print("using tmp_dir", tmp_dir)
    with open(os.path.join(tmp_dir, "MyClass.java"), "w") as f:
        f.write(
            """
public class MyClass {
    public int printToLog(String line) {
        System.err.println(line);
        return 0;
    }
}
        """
        )
    subprocess.check_call(["javac", "MyClass.java"], cwd=tmp_dir)
    subprocess.check_call(["jar", "-cf", "myJar.jar", "MyClass.class"], cwd=tmp_dir)

    ray.init(
        job_config=ray.job_config.JobConfig(code_search_path=[tmp_dir]),
    )

    handle = java_actor_class("MyClass").remote()
    ray.get(handle.printToLog.remote("here's my random line!"))

    def check():
        out, err = capsys.readouterr()
        out += err
        with capsys.disabled():
            print(out)
        return "here's my random line!" in out

    wait_for_condition(check)


"""
Unit testing log monitor.
"""


def create_file(dir, filename, content):
    f = dir / filename
    f.write_text(content)


@pytest.fixture
def live_dead_pids():
    p1 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(6000)"])
    p2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(6000)"])
    p2.kill()
    # avoid zombie processes
    p2.wait()
    yield p1.pid, p2.pid
    p1.kill()
    p1.wait()


def test_log_monitor(tmp_path, live_dead_pids):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    # Create an old dir.
    (log_dir / "old").mkdir()
    worker_id = "6df6d5dd8ca5215658e4a8f9a569a9d98e27094f9cc35a4ca43d272c"
    job_id = "01000000"
    alive_pid, dead_pid = live_dead_pids

    mock_publisher = MagicMock()
    log_monitor = LogMonitor(
        "127.0.0.1", str(log_dir), mock_publisher, is_proc_alive, max_files_open=5
    )

    # files
    worker_out_log_file = f"worker-{worker_id}-{job_id}-{dead_pid}.out"
    worker_err_log_file = f"worker-{worker_id}-{job_id}-{dead_pid}.err"
    monitor = "monitor.log"
    raylet_out = "raylet.out"
    raylet_err = "raylet.err"
    gcs_server_err = "gcs_server.1.err"

    contents = "123"

    create_file(log_dir, raylet_err, contents)
    create_file(log_dir, raylet_out, contents)
    create_file(log_dir, gcs_server_err, contents)
    create_file(log_dir, monitor, contents)
    create_file(log_dir, worker_out_log_file, contents)
    create_file(log_dir, worker_err_log_file, contents)

    """
    Test files are updated.
    """
    log_monitor.update_log_filenames()

    assert len(log_monitor.open_file_infos) == 0
    assert len(log_monitor.closed_file_infos) == 5
    assert log_monitor.can_open_more_files is True
    assert len(log_monitor.log_filenames) == 5

    def file_exists(log_filenames, filename):
        for f in log_filenames:
            if filename in f:
                return True
        return False

    assert file_exists(log_monitor.log_filenames, raylet_err)
    assert not file_exists(log_monitor.log_filenames, raylet_out)
    assert file_exists(log_monitor.log_filenames, gcs_server_err)
    assert file_exists(log_monitor.log_filenames, monitor)
    assert file_exists(log_monitor.log_filenames, worker_out_log_file)
    assert file_exists(log_monitor.log_filenames, worker_err_log_file)

    def get_file_info(file_infos, filename):
        for file_info in file_infos:
            if filename in file_info.filename:
                return file_info
        assert False, "Shouldn't reach."

    raylet_err_info = get_file_info(log_monitor.closed_file_infos, raylet_err)
    gcs_server_err_info = get_file_info(log_monitor.closed_file_infos, gcs_server_err)
    monitor_info = get_file_info(log_monitor.closed_file_infos, monitor)
    worker_out_log_file_info = get_file_info(
        log_monitor.closed_file_infos, worker_out_log_file
    )
    worker_err_log_file_info = get_file_info(
        log_monitor.closed_file_infos, worker_err_log_file
    )

    assert raylet_err_info.is_err_file
    assert gcs_server_err_info.is_err_file
    assert not monitor_info.is_err_file
    assert not worker_out_log_file_info.is_err_file
    assert worker_err_log_file_info.is_err_file

    assert worker_out_log_file_info.job_id is None
    assert worker_err_log_file_info.job_id is None
    assert worker_out_log_file_info.worker_pid == int(dead_pid)
    assert worker_out_log_file_info.worker_pid == int(dead_pid)

    """
    Test files are opened.
    """
    log_monitor.open_closed_files()
    assert len(log_monitor.open_file_infos) == 5
    assert len(log_monitor.closed_file_infos) == 0
    assert not log_monitor.can_open_more_files

    """
    Test files are published.
    """

    assert log_monitor.check_log_files_and_publish_updates()
    assert raylet_err_info.worker_pid == "raylet"
    assert gcs_server_err_info.worker_pid == "gcs_server"
    assert monitor_info.worker_pid == "autoscaler"

    assert mock_publisher.publish_logs.call_count

    for file_info in log_monitor.open_file_infos:
        mock_publisher.publish_logs.assert_any_call(
            {
                "ip": log_monitor.ip,
                "pid": file_info.worker_pid,
                "job": file_info.job_id,
                "is_err": file_info.is_err_file,
                "lines": [contents],
                "actor_name": file_info.actor_name,
                "task_name": file_info.task_name,
            }
        )
    # If there's no new update, it should return False.
    assert not log_monitor.check_log_files_and_publish_updates()

    # Test max lines read == 99 is repsected.
    lines = "1\n" * int(1.5 * ray_constants.LOG_MONITOR_NUM_LINES_TO_READ)
    with open(raylet_err_info.filename, "a") as f:
        # Write 150 more lines.
        f.write(lines)

    assert log_monitor.check_log_files_and_publish_updates()
    mock_publisher.publish_logs.assert_any_call(
        {
            "ip": log_monitor.ip,
            "pid": raylet_err_info.worker_pid,
            "job": raylet_err_info.job_id,
            "is_err": raylet_err_info.is_err_file,
            "lines": ["1" for _ in range(ray_constants.LOG_MONITOR_NUM_LINES_TO_READ)],
            "actor_name": file_info.actor_name,
            "task_name": file_info.task_name,
        }
    )

    """
    Test files are closed.
    """
    # log_monitor.open_closed_files() should close all files
    # if it cannot open new files.
    new_worker_err_file = f"worker-{worker_id}-{job_id}-{alive_pid}.err"
    create_file(log_dir, new_worker_err_file, contents)
    log_monitor.update_log_filenames()

    # System logs are not closed.
    # - raylet, gcs, monitor
    # Dead workers are not tracked anymore. They will be moved to old folder.
    # - dead pid out & err
    # alive worker is going to be newly opened.
    log_monitor.open_closed_files()
    assert len(log_monitor.open_file_infos) == 2
    assert log_monitor.can_open_more_files
    # Two dead workers are not tracked anymore, and they will be in the old folder.
    # monitor.err and gcs_server.1.err have not been updated, so they remain closed.
    assert len(log_monitor.closed_file_infos) == 2
    assert len(list((log_dir / "old").iterdir())) == 2


def test_tpu_logs(tmp_path):
    # Create the log directories. tpu_logs would be a symlink to the
    # /tmp/tpu_logs directory created in Node _init_temp.
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    tpu_log_dir = log_dir / "tpu_logs"
    tpu_log_dir.mkdir()
    # Create TPU device log file in tpu_logs directory.
    tpu_device_log_file = "tpu-device.log"
    first_line = "First line\n"
    create_file(tpu_log_dir, tpu_device_log_file, first_line)

    mock_publisher = MagicMock()
    log_monitor = LogMonitor(
        "127.0.0.1",
        str(log_dir),
        mock_publisher,
        is_proc_alive,
        max_files_open=5,
    )
    # Verify TPU logs are ingested by LogMonitor.
    log_monitor.update_log_filenames()
    log_monitor.open_closed_files()
    assert len(log_monitor.open_file_infos) == 1
    file_info = log_monitor.open_file_infos[0]
    assert Path(file_info.filename) == tpu_log_dir / tpu_device_log_file


def test_log_monitor_actor_task_name_and_job_id(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    worker_id = "6df6d5dd8ca5215658e4a8f9a569a9d98e27094f9cc35a4ca43d272c"
    job_id = "01000000"
    pid = "47660"

    mock_publisher = MagicMock()
    log_monitor = LogMonitor(
        "127.0.0.1", str(log_dir), mock_publisher, lambda _: True, max_files_open=5
    )
    worker_out_log_file = f"worker-{worker_id}-{job_id}-{pid}.out"
    first_line = "First line\n"
    create_file(log_dir, worker_out_log_file, first_line)
    log_monitor.update_log_filenames()
    log_monitor.open_closed_files()
    assert len(log_monitor.open_file_infos) == 1
    file_info = log_monitor.open_file_infos[0]

    # Test task name updated.
    task_name = "task"
    with open(file_info.filename, "a") as f:
        # Write 150 more lines.
        f.write(f"{ray_constants.LOG_PREFIX_TASK_NAME}{task_name}\n")
        f.write("line")
    log_monitor.check_log_files_and_publish_updates()
    assert file_info.task_name == task_name
    assert file_info.actor_name is None
    mock_publisher.publish_logs.assert_any_call(
        {
            "ip": log_monitor.ip,
            "pid": file_info.worker_pid,
            "job": file_info.job_id,
            "is_err": file_info.is_err_file,
            "lines": ["line"],
            "actor_name": None,
            "task_name": task_name,
        }
    )

    # Test the actor name is updated.
    actor_name = "actor"
    with open(file_info.filename, "a") as f:
        # Write 150 more lines.
        f.write(f"{ray_constants.LOG_PREFIX_ACTOR_NAME}{actor_name}\n")
        f.write("line2")
    log_monitor.check_log_files_and_publish_updates()
    assert file_info.task_name is None
    assert file_info.actor_name == actor_name
    mock_publisher.publish_logs.assert_any_call(
        {
            "ip": log_monitor.ip,
            "pid": file_info.worker_pid,
            "job": file_info.job_id,
            "is_err": file_info.is_err_file,
            "lines": ["line2"],
            "actor_name": actor_name,
            "task_name": None,
        }
    )

    # Test the job_id is updated.
    job_id = "01000000"
    with open(file_info.filename, "a") as f:
        # Write 150 more lines.
        f.write(f"{ray_constants.LOG_PREFIX_JOB_ID}{job_id}\n")
        f.write("line2")
    log_monitor.check_log_files_and_publish_updates()
    assert file_info.job_id == job_id
    mock_publisher.publish_logs.assert_any_call(
        {
            "ip": log_monitor.ip,
            "pid": file_info.worker_pid,
            "job": file_info.job_id,
            "is_err": file_info.is_err_file,
            "lines": ["line2"],
            "actor_name": actor_name,
            "task_name": None,
        }
    )


@pytest.fixture
def mock_timer():
    f = time.time
    time.time = MagicMock()
    yield time.time
    time.time = f


def test_log_monitor_update_backpressure(tmp_path, mock_timer):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    mock_publisher = MagicMock()
    log_monitor = LogMonitor(
        "127.0.0.1", str(log_dir), mock_publisher, lambda _: True, max_files_open=5
    )

    current = 0
    mock_timer.return_value = current

    log_monitor.log_filenames = []
    # When threshold < RAY_LOG_MONITOR_MANY_FILES_THRESHOLD, update should happen.
    assert log_monitor.should_update_filenames(current)
    # Add a new file.
    log_monitor.log_filenames = [
        "raylet.out" for _ in range(RAY_LOG_MONITOR_MANY_FILES_THRESHOLD)
    ]
    # If the threshold is met, we should update the file after
    # LOG_NAME_UPDATE_INTERVAL_S.
    assert not log_monitor.should_update_filenames(current)
    mock_timer.return_value = LOG_NAME_UPDATE_INTERVAL_S - 0.1
    assert not log_monitor.should_update_filenames(current)
    mock_timer.return_value = LOG_NAME_UPDATE_INTERVAL_S
    assert not log_monitor.should_update_filenames(current)
    mock_timer.return_value = LOG_NAME_UPDATE_INTERVAL_S + 0.1
    assert log_monitor.should_update_filenames(current)


def test_repr_inheritance(shutdown_only):
    """Tests that a subclass's repr is used in logging."""
    logger = logging.getLogger(__name__)

    class MyClass:
        def __repr__(self) -> str:
            return "ThisIsMyCustomActorName" + ray_constants.TESTING_NEVER_DEDUP_TOKEN

        def do(self):
            logger.warning("text" + ray_constants.TESTING_NEVER_DEDUP_TOKEN)

    class MySubclass(MyClass):
        pass

    my_class_remote = ray.remote(MyClass)
    my_subclass_remote = ray.remote(MySubclass)

    f = io.StringIO()
    with redirect_stderr(f):
        my_class_actor = my_class_remote.remote()
        ray.get(my_class_actor.do.remote())
        # Wait a little to be sure that we have captured the output
        time.sleep(1)
        print("", flush=True)
        print("", flush=True, file=sys.stderr)
        f = f.getvalue()
        assert "ThisIsMyCustomActorName" in f and "MySubclass" not in f

    f2 = io.StringIO()
    with redirect_stderr(f2):
        my_subclass_actor = my_subclass_remote.remote()
        ray.get(my_subclass_actor.do.remote())
        # Wait a little to be sure that we have captured the output
        time.sleep(1)
        print("", flush=True, file=sys.stderr)
        f2 = f2.getvalue()
        assert "ThisIsMyCustomActorName" in f2 and "MySubclass" not in f2


def test_ray_does_not_break_makeRecord():
    """Importing Ray used to cause `logging.makeRecord` to use the default record
    factory, rather than the factory set by `logging.setRecordFactory`.

    This tests validates that this bug is fixed.
    """
    # Make a call with the cli logger to be sure that invoking the
    # cli logger does not mess up logging.makeRecord.
    with redirect_stdout(None):
        cli_logger.info("Cli logger invoked.")

    mockRecordFactory = Mock()
    try:
        logging.setLogRecordFactory(mockRecordFactory)
        # makeRecord needs 7 positional args. What the args are isn't consequential.
        makeRecord_args = [None] * 7
        logging.Logger("").makeRecord(*makeRecord_args)
        # makeRecord called the expected factory.
        mockRecordFactory.assert_called_once()
    finally:
        # Set it back to the default factory.
        logging.setLogRecordFactory(logging.LogRecord)


@pytest.mark.parametrize(
    "logger_name,logger_level",
    (
        ("ray", logging.INFO),
        ("ray.air", logging.INFO),
        ("ray.data", logging.INFO),
        ("ray.rllib", logging.WARNING),
        ("ray.serve", logging.INFO),
        ("ray.train", logging.INFO),
        ("ray.tune", logging.INFO),
    ),
)
@pytest.mark.parametrize(
    "test_level",
    (
        logging.NOTSET,
        logging.DEBUG,
        logging.INFO,
        logging.WARNING,
        logging.ERROR,
        logging.CRITICAL,
    ),
)
def test_log_level_settings(
    propagate_logs, caplog, logger_name, logger_level, test_level
):
    """Test that logs of lower level than the ray subpackage is
    configured for are rejected.
    """
    logger = logging.getLogger(logger_name)

    logger.log(test_level, "Test!")

    if test_level >= logger_level:
        assert caplog.records, "Log message missing where one is expected."
        assert caplog.records[-1].levelno == test_level, "Log message level mismatch."
    else:
        assert len(caplog.records) == 0, "Log message found where none are expected."


def test_log_with_import():
    logger = logging.getLogger(__name__)
    assert not logger.disabled
    ray.log.logger_initialized = False
    ray.log.generate_logging_config()
    assert not logger.disabled


@pytest.mark.skipif(sys.platform != "linux", reason="Only works on linux.")
def test_log_monitor_ip_correct(ray_start_cluster):
    cluster = ray_start_cluster
    # add first node
    cluster.add_node(
        node_ip_address="127.0.0.2",
        resources={"pin_task": 1},
    )
    address = cluster.address
    ray.init(address)
    # add second node
    cluster.add_node(node_ip_address="127.0.0.3")

    @ray.remote(resources={"pin_task": 1})
    def print_msg():
        print("abc")

    p = init_log_pubsub()
    ray.get(print_msg.remote())
    data = get_log_data(
        p, num=6, timeout=10, job_id=ray.get_runtime_context().get_job_id()
    )
    assert data[0]["ip"] == "127.0.0.2"


def get_print_worker_logs_output(data: Dict[str, str]) -> str:
    """
    Helper function that returns the output of `print_worker_logs` as a str.
    """
    out = io.StringIO()
    print_worker_logs(data, out)
    out.seek(0)
    return out.readline()


def test_print_worker_logs_default_color() -> None:
    # Test multiple since pid may affect color
    for pid in (0, 1):
        data = dict(
            ip="10.0.0.1",
            localhost="172.0.0.1",
            pid=str(pid),
            task_name="my_task",
            lines=["is running"],
        )
        output = get_print_worker_logs_output(data)
        assert output == (
            f"{colorama.Fore.CYAN}(my_task pid={pid}, ip=10.0.0.1)"
            + f"{colorama.Style.RESET_ALL} is running\n"
        )

    # Special case
    raylet = dict(
        ip="10.0.0.1",
        localhost="172.0.0.1",
        pid="raylet",
        task_name="my_task",
        lines=["Warning: uh oh"],
    )
    output = get_print_worker_logs_output(raylet)
    assert output == (
        f"{colorama.Fore.YELLOW}(raylet, ip=10.0.0.1){colorama.Style.RESET_ALL} "
        + "Warning: uh oh\n"
    )


@patch.dict(os.environ, {"RAY_COLOR_PREFIX": "0"})
def test_print_worker_logs_no_color() -> None:
    for pid in (0, 1):
        data = dict(
            ip="10.0.0.1",
            localhost="172.0.0.1",
            pid=str(pid),
            task_name="my_task",
            lines=["is running"],
        )
        output = get_print_worker_logs_output(data)
        assert output == f"(my_task pid={pid}, ip=10.0.0.1) is running\n"

    raylet = dict(
        ip="10.0.0.1",
        localhost="172.0.0.1",
        pid="raylet",
        task_name="my_task",
        lines=["Warning: uh oh"],
    )
    output = get_print_worker_logs_output(raylet)
    assert output == "(raylet, ip=10.0.0.1) Warning: uh oh\n"


@patch.dict(os.environ, {"RAY_COLOR_PREFIX": "1"})
def test_print_worker_logs_multi_color() -> None:
    data_pid_0 = dict(
        ip="10.0.0.1",
        localhost="172.0.0.1",
        pid="0",
        task_name="my_task",
        lines=["is running"],
    )
    output = get_print_worker_logs_output(data_pid_0)
    assert output == (
        f"{colorama.Fore.MAGENTA}(my_task pid=0, ip=10.0.0.1)"
        + f"{colorama.Style.RESET_ALL} is running\n"
    )

    data_pid_2 = dict(
        ip="10.0.0.1",
        localhost="172.0.0.1",
        pid="2",
        task_name="my_task",
        lines=["is running"],
    )
    output = get_print_worker_logs_output(data_pid_2)
    assert output == (
        f"{colorama.Fore.GREEN}(my_task pid=2, ip=10.0.0.1){colorama.Style.RESET_ALL} "
        + "is running\n"
    )


class TestSetupLogRecordFactory:
    def test_setup_log_record_factory_directly(self):
        # Reset the log record factory to the default.
        logging.setLogRecordFactory(logging.LogRecord)
        record_old = logging.makeLogRecord({})

        # Set up the log record factory with _setup_log_record_factory().
        ray.log._setup_log_record_factory()
        record_new = logging.makeLogRecord({})

        assert "_ray_timestamp_ns" not in record_old.__dict__
        assert "_ray_timestamp_ns" in record_new.__dict__

    def test_setup_log_record_factory_in_generate_logging_config(self):
        # Reset the log record factory to the default.
        logging.setLogRecordFactory(logging.LogRecord)
        record_old = logging.makeLogRecord({})

        # generate_logging_config() also setup the log record factory.
        ray.log.logger_initialized = False
        ray.log.generate_logging_config()
        record_new = logging.makeLogRecord({})

        assert "_ray_timestamp_ns" not in record_old.__dict__
        assert "_ray_timestamp_ns" in record_new.__dict__


if __name__ == "__main__":
    # Make subprocess happy in bazel.
    os.environ["LC_ALL"] = "en_US.UTF-8"
    os.environ["LANG"] = "en_US.UTF-8"
    sys.exit(pytest.main(["-sv", __file__]))
