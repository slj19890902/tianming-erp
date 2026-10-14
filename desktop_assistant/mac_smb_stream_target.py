"""Explicit stream selection; never a filesystem mount or automatic fallback."""
from contextlib import ExitStack
from pathlib import Path
import os
import shutil

from . import mac_smb_stream_runtime_contract as cfg

PREFIX = 'tianming-smb-stream:'
MODULE = 'desktop_assistant/mac_smb_stream_target.py'
ERROR = 'SMB流备份目标未通过核验；保留现场，不改用其他备份位置'


def require(ok):
    if not ok: raise ValueError(ERROR)


def selected(path):
    # Recognize malformed tokens too: they must not fall through to POSIX IO.
    return isinstance(path, (str, os.PathLike)) and str(path).startswith(PREFIX)


def destination(control):
    require(cfg.sha(control.get('contract_sha256')))
    return Path(PREFIX + control['contract_sha256'])


def metadata_present():
    for path in (cfg.CONTROL, cfg.CONTRACT):
        try: path.lstat()
        except FileNotFoundError: continue
        else: return True  # Partial records and symlinks also require validation.
    return False


def prepare(path):
    """Approve code and root metadata without NAS connection or credential IO."""
    from . import mac_smb_stream_runtime as runtime
    try:
        require(selected(path))
        binding = runtime.load_binding()
        require(str(path) == str(destination(binding.control))
                and Path(__file__).resolve() == binding.tree / MODULE)
        binding.check()
        return binding
    except Exception:
        raise ValueError(ERROR) from None


def approved_destination(context):
    from . import mac_smb_stream_runtime as runtime
    binding = runtime.load_binding()
    path = destination(binding.control)
    require(binding.context == context)
    verified = prepare(path)
    binding.check()
    require(verified.context == context and verified.control == binding.control)
    return path


def rotation_enabled(path):
    """Old archive workers keep their existing route; new capability is explicit."""
    binding=prepare(path)
    manifest=getattr(binding,'manifest',None)
    if manifest is None:return False
    if cfg.ROTATION_CAPABILITY not in manifest.get('runtime_capabilities',{}):return False
    cfg.rotation_capability(manifest);binding.check()
    return True


def configured(path, context):
    """Status only: small approved local records, no secrets or NAS access."""
    from . import mac_smb_stream_receipt as receipt
    bound = receipt.approval()
    return bound['context'] == context and str(path) == str(destination(bound['control']))


def _manager(binding):
    from . import mac_smb_stream_runtime as runtime
    from .manager import Manager
    public = runtime.publication.read_record(runtime.published.PUBLIC)
    require(cfg.digest(public) == binding.control['publisher_sha256'])
    return Manager(cfg.BASE / 'instance', public)


def _check_manager(manager, binding):
    require(manager.root == cfg.BASE / 'instance'
            and manager.root == manager.root.resolve()
            and cfg.digest(manager.public_key) == binding.control['publisher_sha256'])


def preflight(path, *, manager=None):
    """Called under maintenance lock, while the original ERP may still run."""
    from . import mac_smb_stream_receipt as receipt, mac_smb_stream_probe_v2 as probe
    from . import mac_smb_stream_runtime as runtime
    from .mac_service_restore import Lease
    try:
        binding = prepare(path)
        with ExitStack() as stack:
            root = stack.enter_context(receipt.root_lease(binding.context))
            held = [root]
            for name in ('releases', 'shared', 'control', 'backups', 'packages', 'staging'):
                held.append(stack.enter_context(Lease(root.path / name,
                    uid=binding.context['uid'], gid=binding.context['gid'], mode=0o700,
                    directory=True, parent=root, mutable=True)))
            actual = manager if manager is not None else _manager(binding)
            _check_manager(actual, binding)
            required = actual._backup_inputs()['required']
            probe.specification('0' * 32, required)
            require(shutil.disk_usage(root.path / 'staging').free >= required * 3 // 2)
            binding.check()
            proof = runtime.preflight(required)
            probe.validate(proof, binding.value, proof['attempt_id'], required)
            binding.check()
            for lease in held: lease.check()
            return proof
    except Exception:
        raise ValueError(ERROR) from None


def archive_stopped(manager, password, path):
    return _archive(manager, password, path)


def archive_with_resume(manager, password, path, resume):
    """Normal backups resume at the captured point; update/pause stay cold."""
    require(callable(resume))
    return _archive(manager, password, path, resume)


def _archive(manager, password, path, resume=None):
    from . import mac_smb_stream_receipt as receipt
    try:
        binding = prepare(path)
        _check_manager(manager, binding)
        require(not manager._process())
        if resume is None:
            result = receipt.archive_and_publish_stopped(manager, password)
        else:
            result = receipt.archive_and_publish_captured(manager, password, resume)
        binding.check()
        require(result.parent == manager.root / 'backups'
                and manager.state.get('last_backup') == str(result)
                and receipt.verified_at(manager.root, manager.state) is not None)
        return result
    except Exception:
        raise ValueError(ERROR) from None


def capture_bootstrap_authority(manager,password):
    """Approved candidate owner entry before binding upgrade; no new backup."""
    from .nas_backup_rotation_worker import capture_first_authority
    return capture_first_authority(manager,password)


def reauthenticate_bootstrap_first(manager,password,path):
    binding=prepare(path);_check_manager(manager,binding)
    from .nas_backup_rotation_worker import reauthenticate_first
    return reauthenticate_first(manager,password)


def reserve_local_update_slot(manager,path):
    binding=prepare(path);_check_manager(manager,binding)
    from .nas_backup_rotation_worker import reserve_bootstrap_second
    return reserve_bootstrap_second(manager)
