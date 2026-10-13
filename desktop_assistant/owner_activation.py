"""Owner-domain codec and fixed UID499 audit worker; no private-key access."""
import base64
from contextlib import ExitStack
from datetime import datetime,timezone
import hashlib
import os
from pathlib import Path
import sys


def _contract():
    from desktop_assistant import owner_activation_contract
    return owner_activation_contract


def public_identity(public):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    c = _contract();c.require(type(public) is bytes and 0 < len(public) <= 8192)
    key = serialization.load_pem_public_key(public)
    c.require(isinstance(key,Ed25519PublicKey))
    canonical = key.public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    c.require(canonical == public)
    return key,hashlib.sha256(canonical).hexdigest()


def sign_authorization(request,preparation,declaration,signer,mac_public,*,now):
    """The root-owned caller signs bytes; the key is never exported to this module."""
    c = _contract();body = c.make_body(request,preparation,declaration,now=now)
    c.require(callable(signer) and public_identity(mac_public)[1] == request['mac_publisher_sha256'])
    signature = signer(c.DOMAIN+c.canonical(body))
    c.require(type(signature) is bytes and len(signature) == 64)
    raw = c.canonical(dict(body=body,signature=base64.b64encode(signature).decode('ascii')))
    verify_authorization(raw,mac_public,request)
    return raw


def verify_authorization(raw,mac_public,request):
    c = _contract();body = c.authorization_body(raw,request)
    key,fingerprint = public_identity(mac_public)
    c.require(fingerprint == request['mac_publisher_sha256'])
    key.verify(base64.b64decode(c.decode(raw)['signature'],validate=True),c.DOMAIN+c.canonical(body))
    return body


def verify_mount_authority(preparation,bound,checkpoint):
    """Authenticate retained mounted evidence; startup need not contact the NAS."""
    c = _contract();backup = preparation['backup']
    c.facts.validate_preparation(preparation)
    from desktop_assistant.mounted_nas_proof import validate_checkpoint
    validate_checkpoint(checkpoint)
    c.require(bound['contract']['checkpoint_attempt_id'] is not None
        and bound['control']['package_sha256'] == backup['transport_package_sha256']
        and bound['control']['context_sha256'] == preparation['context_sha256']
        and bound['control']['publisher_sha256'] == preparation['publisher_sha256']
        and bound['control_sha'] == backup['transport_control_sha256']
        and bound['control']['contract_sha256'] == backup['transport_contract_sha256']
        and bound['control']['receipt_sha256'] == backup['receipt_sha256']
        and bound['receipt']['sha256'] == checkpoint['sha256'] == backup['sha256']
        and bound['receipt']['size'] == checkpoint['size'] == backup['size']
        and c.facts.normalize_timestamp(bound['receipt']['verified_at']) == backup['verified_at']
        and c.facts.normalize_timestamp(checkpoint['created_at']) == backup['created_at']
        and checkpoint['release_sha256'] == backup['release_sha256'] == preparation['execution_package_sha256']
        and checkpoint['state_sha256'] == preparation['state_sha256']
        and checkpoint['shared_manifest_sha256'] == preparation['shared_manifest_sha256']
        and checkpoint['local_update_receipt_sha256'] == bound['contract']['local_update_receipt_sha256'])
    return bound


def _entry():
    c = _contract()
    c.require(sys.platform == 'darwin' and sys.flags.isolated and sys.dont_write_bytecode
        and (os.getuid(),os.geteuid(),os.getgid(),os.getegid()) == (499,499,499,499))
    import pwd
    c.require(pwd.getpwuid(499).pw_name == '_tianmingerp')


def run(attempt_id,plan_sha256,challenge,lock_fd,mounted_fd,*,script,executable):
    from desktop_assistant import mac_service_restore as restore,mac_service_manage as manage
    from desktop_assistant import mac_keychain_install as boot,mac_system_service as system
    from desktop_assistant import mac_published_runtime as published,current_mac_target_audit as audit
    from desktop_assistant import mounted_nas_proof as mounted,mac_publisher_trust as trust
    from desktop_assistant.mac_system_upgrade_worker import _held_maintenance
    c = _contract();_entry();c.facts._id(attempt_id);c.facts._hash(plan_sha256);c.facts._id(challenge)
    base = published.BASE;slot_path = base/('owner-current-mac-'+attempt_id)
    with ExitStack() as stack:
        base_lease = stack.enter_context(restore.Lease(base,uid=0,gid=None,mode=0o755,directory=True,mutable=True))
        slot = stack.enter_context(restore.Lease(slot_path,uid=0,gid=None,mode=0o755,directory=True,parent=base_lease))
        item = stack.enter_context(restore.Lease(slot.path/'plan.json',uid=0,gid=None,mode=0o644,parent=slot))
        raw = restore._read_complete(item,c.facts.LIMIT);plan = c.validate_plan(c.decode(raw))
        c.require(raw == c.canonical(plan) and hashlib.sha256(raw).hexdigest() == plan_sha256
            and plan['attempt_id'] == attempt_id)
        prep = plan['preparation'];request = plan['request'];authorization = plan['authorization'].encode()
        context = restore.runtime_identity()
        c.require(context['uid'] == context['gid'] == 499 and boot._actual() == boot._expected(499,499))
        held = []
        for path in (trust.MAC_PUBLIC,trust.RECORD):
            pin = stack.enter_context(restore.Lease(path,uid=0,gid=None,mode=0o644,parent=base_lease))
            held.append((pin,restore._read_complete(pin,c.facts.LIMIT)))
        def authority():
            value = trust.read_authority(expected_context_sha256=prep['context_sha256'],
                expected_legacy_anchor_sha256=prep['publisher_sha256'],
                expected_mac_publisher_sha256=request['mac_publisher_sha256'])
            c.require(value['mac_public'] == held[0][1] and value['raw'] == held[1][1])
            return value
        authority_value = authority()
        for role_identity in (prep['execution_package_sha256'],prep['monitor_package_sha256']):
            role_meta = system.metadata(role_identity);role_manifest = system._program_manifest(role_meta)
            c.require(role_meta['context'] == context and all(role_meta[k] == prep[k] for k in
                ('context_sha256','publisher_sha256','registry_sha256'))
                and type(role_manifest.get('runtime_capabilities',{}).get(c.CAPABILITY)) is int
                and role_manifest['runtime_capabilities'][c.CAPABILITY] == 1 and c.FILES <= role_manifest['files'].keys())
        identity = prep['backup']['transport_package_sha256'];meta = system.metadata(identity)
        manifest = system._program_manifest(meta)
        c.require(meta['context'] == context and all(meta[k] == prep[k] for k in
            ('context_sha256','publisher_sha256','registry_sha256'))
            and manifest.get('runtime_capabilities',{}).get(c.CAPABILITY) == 1
            and type(manifest['runtime_capabilities'][c.CAPABILITY]) is int
            and c.FILES <= manifest['files'].keys()
            and Path(__file__).resolve() == meta['tree']/c.MODULE and Path(script).resolve() == meta['tree']/c.MODULE
            and Path(sys.executable).resolve() == meta['tree']/'runtime/bin/python3.12'
            and Path(executable).resolve() == meta['tree']/'runtime/bin/python3.12')
        body = verify_authorization(authorization,authority_value['mac_public'],request)
        def check():
            base_lease.check();slot.check();item.check()
            c.require(restore._read_complete(item,c.facts.LIMIT) == raw and not os.path.lexists(base/'factory-activation.json'))
            for pin,data in held:
                pin.check();c.require(restore._read_complete(pin,c.facts.LIMIT) == data)
            authority();c.require_install_window(body,now=datetime.now(timezone.utc))
            fresh = system.metadata(identity)
            c.require(fresh == meta and system._program_manifest(fresh) == manifest)
        with manage.instance(context,inherited_lock=lock_fd) as (_,lock,paths_check), _held_maintenance(lock,borrowed=True):
            with audit.verified_current_mac_backup(prep['execution_package_sha256'],prep['monitor_package_sha256'],
                    plan['restore_attempt_id'],context_sha256=prep['context_sha256'],publisher_sha256=prep['publisher_sha256'],
                    registry_sha256=prep['registry_sha256'],restore_receipt_sha256=prep['restore']['restore_receipt_sha256'],
                    inherited_lock=lock_fd,expected_preparation=prep,mounted_fd=mounted_fd,audit_package_sha256=identity) as (actual,audit_check):
                check();paths_check();c.require(actual == prep)
                record = c.make_record(request,plan['network'],authorization,activated_at=datetime.now(timezone.utc))
                reply = dict(type=c.REPLY,attempt_id=attempt_id,plan_sha256=plan_sha256,challenge=challenge,
                    identity=boot._actual(),record=record)
                c.validate_reply(reply,plan,plan_sha256,challenge)
                c.require(audit_check() == prep);check();paths_check()
        check();return reply


def main():
    # Authentication occurs before any application or credential module import.
    if not (sys.platform == 'darwin' and sys.flags.isolated and sys.dont_write_bytecode
            and (os.getuid(),os.geteuid(),os.getgid(),os.getegid()) == (499,499,499,499)):
        raise ValueError('Owner Mac audit requires the isolated dedicated service identity')
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent));os.umask(0o077)
    c = _contract();argv = sys.argv[1:]
    names = {'--attempt','--plan-sha256','--challenge','--lock-fd','--mounted-fd'}
    c.require(len(argv) == 10 and set(argv[::2]) == names)
    args = dict(zip(argv[::2],argv[1::2]))
    for key in ('--lock-fd','--mounted-fd'):
        c.require(args[key].isascii() and args[key].isdecimal() and 3 <= int(args[key]) <= 2**31-1)
    result = run(args['--attempt'],args['--plan-sha256'],args['--challenge'],int(args['--lock-fd']),
        int(args['--mounted-fd']),script=__file__,executable=sys.executable)
    sys.stdout.buffer.write(c.canonical(result)+b'\n');sys.stdout.buffer.flush()


if __name__ == '__main__':main()
