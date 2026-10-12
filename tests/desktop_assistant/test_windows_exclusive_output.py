"""Native Windows leaf-boundary regressions plus explicitly simulated ABI tests."""
import ctypes as C
from ctypes import wintypes as W
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from desktop_assistant import storage


@pytest.fixture
def native_api(monkeypatch):
    handle = 0x123456789  # Detect accidental 32-bit HANDLE declarations.
    kernel = SimpleNamespace(CreateFileW=Mock(return_value=handle), CloseHandle=Mock(return_value=1))
    crt = SimpleNamespace(open_osfhandle=Mock(return_value=101))
    monkeypatch.setitem(sys.modules, 'msvcrt', crt)
    monkeypatch.setattr(C, 'WinDLL', Mock(return_value=kernel), raising=False)
    monkeypatch.setattr(C, 'get_last_error', lambda: 183, raising=False)
    monkeypatch.setattr(C, 'WinError', lambda code: OSError(code, 'synthetic error'), raising=False)
    monkeypatch.setattr(os, 'O_BINARY', 0x8000, raising=False)
    monkeypatch.setattr(os, 'O_NOINHERIT', 0x80, raising=False)
    return kernel, crt, handle


def test_simulated_native_call_is_atomic_nofollow_noninheritable(tmp_path, native_api):
    kernel, crt, handle = native_api
    output = tmp_path / 'output'
    assert storage._windows_create_new(str(output), os.O_WRONLY) == 101
    kernel.CreateFileW.assert_called_once_with(str(output), 0x40000000, 3, None, 1, 0x00200080, None)
    assert kernel.CreateFileW.restype is W.HANDLE
    assert kernel.CreateFileW.argtypes[-1] is W.HANDLE
    crt.open_osfhandle.assert_called_once_with(handle, os.O_WRONLY | os.O_BINARY | os.O_NOINHERIT)
    kernel.CloseHandle.assert_not_called()  # CRT now owns exactly this handle.


@pytest.mark.parametrize('kind', ['file', 'directory', 'dangling'])
def test_preexisting_leaf_never_reaches_native_create(tmp_path, native_api, kind):
    kernel, crt, _ = native_api
    output = tmp_path / 'output'
    if kind == 'file': output.write_bytes(b'preserve')
    elif kind == 'directory': output.mkdir()
    else: output.symlink_to(tmp_path / 'absent')
    with pytest.raises(FileExistsError): storage._windows_create_new(str(output), 0)
    kernel.CreateFileW.assert_not_called()
    crt.open_osfhandle.assert_not_called()
    assert not (tmp_path / 'absent').exists()


@pytest.mark.parametrize('code', [80, 183, 5])
def test_simulated_native_failure_has_no_crt_or_cleanup(tmp_path, native_api, monkeypatch, code):
    kernel, crt, _ = native_api
    kernel.CreateFileW.return_value = C.c_void_p(-1).value
    monkeypatch.setattr(C, 'get_last_error', lambda: code)
    with pytest.raises(FileExistsError if code in (80, 183) else OSError):
        storage._windows_create_new(str(tmp_path / 'output'), 0)
    crt.open_osfhandle.assert_not_called()
    kernel.CloseHandle.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('error', [OSError('synthetic fd failure'), KeyboardInterrupt()])
def test_simulated_descriptor_transfer_failure_closes_owned_handle(tmp_path, native_api, error):
    kernel, crt, handle = native_api
    crt.open_osfhandle.side_effect = error
    with pytest.raises(type(error)): storage._windows_create_new(str(tmp_path / 'output'), 0)
    kernel.CloseHandle.assert_called_once_with(handle)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows CreateFileW and CRT actual handle behavior')
@pytest.mark.parametrize('target_exists', [False, True])
def test_windows_native_rejects_link_even_if_precheck_misses_it(tmp_path, monkeypatch, target_exists):
    target = tmp_path / 'other-file'
    if target_exists: target.write_bytes(b'original')
    output = tmp_path / 'output'
    output.symlink_to(target)
    before = output.lstat()
    # Reproduce a link introduced after the advisory check: native flags, not
    # the check, must prevent following either live or dangling symlinks.
    monkeypatch.setattr(storage.os.path, 'lexists', lambda path: False)
    with pytest.raises(FileExistsError): storage._windows_create_new(str(output), 0)
    assert output.is_symlink() and output.lstat().st_ino == before.st_ino
    assert target.exists() == target_exists
    if target_exists: assert target.read_bytes() == b'original'


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows native handle-to-descriptor ownership')
def test_windows_native_new_output_is_binary_noninheritable_and_closed(tmp_path):
    output = tmp_path / 'output'
    with open(output, 'xb', opener=storage._windows_create_new) as stream:
        assert not os.get_inheritable(stream.fileno())
        stream.write(b'\x00\n\r\n\xff')
        stream.flush()
        with pytest.raises(PermissionError): output.rename(tmp_path / 'moved')
    assert output.read_bytes() == b'\x00\n\r\n\xff'
    output.rename(tmp_path / 'moved')  # Closed successfully, no leaked HANDLE.
