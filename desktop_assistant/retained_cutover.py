"""Independent signature codec for retaining an authenticated restored snapshot.

This module never loads a publishing key, reads operational credentials, stops
processes or installs an activation. The source issuer must first authenticate
all evidence while holding its stopped-instance leases. A valid signature is
only one input to the separately protected target activation procedure.
"""
import base64
from datetime import timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant import retained_snapshot_contract as contract
from desktop_assistant.factory_target_contract import canonical, _now, _stamp, _time
from desktop_assistant.release_request import public_identity


def sign_authorization(request, source, preparation, key, *, now=None):
    """Encode already audited evidence using the original caller-owned identity."""
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError('保留副本切换须使用既有Ed25519发布身份')
    contract.validate_request(request)
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    if public_identity(public)[1] != request['publisher_sha256']:
        raise ValueError('保留副本切换签名不是原发布身份')
    current = _now(now)
    before = min(current + timedelta(hours=8), _time(request['expires_at']))
    # Decode a fresh canonical copy so neither the caller nor a mutable nested
    # mapping can change which evidence is signed between validation and use.
    body = contract.decode(canonical({
        'type': contract.AUTHORIZATION, 'request': request, 'source': source,
        'preparation': preparation, 'issued_at': _stamp(current),
        'install_before': _stamp(before),
    }))
    contract.validate_body(body)
    signature = key.sign(contract.DOMAIN + canonical(body))
    raw = canonical({'body': body,
                     'signature': base64.b64encode(signature).decode('ascii')})
    if len(raw) > contract.LIMIT:
        raise ValueError('保留副本切换凭据过大')
    return raw


def verify_authorization(raw, public, expected_request):
    """Verify exact source and target binding; do not apply an install window.

    The initial installer must additionally call require_install_window. A
    successful protected activation does not expire a running server later.
    """
    body = contract.authorization_body(raw, expected_request)
    signature = base64.b64decode(contract.decode(raw)['signature'], validate=True)
    key, fingerprint = public_identity(public)
    key.verify(signature, contract.DOMAIN + canonical(body))
    if body['request']['publisher_sha256'] != fingerprint:
        raise ValueError('保留副本凭据与原身份或完整目标申请不一致')
    return body


def require_install_window(verified_body, *, now=None):
    """Initial activation age check, separate from persistent signature checks."""
    return contract.require_install_window(verified_body, now=_now(now))
