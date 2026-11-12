"""The home identity must be the same in every process: scan workers rebuild it."""
import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import ROOT, macos_only
from storage_utility import hostos
from storage_utility.scope import Scope


def test_fd_path_names_the_folder_regardless_of_descriptor_number(tmp_path):
    first = os.open(tmp_path, os.O_RDONLY)
    spacer = os.open("/", os.O_RDONLY)
    second = os.open(tmp_path, os.O_RDONLY)
    try:
        assert first != second
        assert hostos.fd_path(first) == hostos.fd_path(second) == os.path.realpath(tmp_path)
    finally:
        for fd in (first, spacer, second):
            os.close(fd)


def test_fd_path_follows_the_open_folder_not_the_name(tmp_path):
    folder = tmp_path / "before"
    folder.mkdir()
    fd = os.open(folder, os.O_RDONLY)
    try:
        folder.rename(tmp_path / "after")
        assert hostos.fd_path(fd) == os.path.realpath(tmp_path / "after")
    finally:
        os.close(fd)


def test_scope_binding_is_reproducible_in_the_same_process(home):
    first = Scope(home, "p")
    blocker = os.open("/", os.O_RDONLY)  # shift the next descriptor number
    try:
        second = Scope(home, "p", epoch=first.epoch)
    finally:
        os.close(blocker)
    assert first.root_locator == os.path.realpath(home)
    assert first.binding == second.binding


def test_scope_binding_is_reproducible_in_another_process(home):
    """Regression: on macOS the locator used to be '/proc/self/fd/<n>', which differs per process,
    so every scan worker rejected its own result ('worker scope binding mismatch')."""
    parent = Scope(home, "p")
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
            "from storage_utility.scope import Scope; s = Scope(Path(sys.argv[2]), 'p', epoch=sys.argv[3]); "
            "print(json.dumps(s.binding.as_dict()))")
    out = subprocess.run([sys.executable, "-c", code, str(ROOT), str(home), parent.epoch], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == parent.binding.as_dict()


def test_own_open_paths_lists_a_file_this_process_holds(tmp_path):
    target = tmp_path / "held.bin"
    target.write_bytes(b"1")
    with open(target, "rb"):
        assert os.path.realpath(target) in hostos.own_open_paths()


@macos_only
def test_process_start_matches_hermes_units_for_this_process():
    import psutil
    assert hostos.process_start(os.getpid()) == round(psutil.Process().create_time() * 100)
    assert hostos.process_start(0) is None
