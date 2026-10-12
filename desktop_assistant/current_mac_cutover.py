"""Ed25519 codec for v3 claims; never load/generate/export a private key.

Caller-owned key/public material only. There is no issuer, DPAPI, filesystem,
database, credential or process access, or implicit clock. A valid signature
does not establish Mac backup equality, Windows isolation or root approval.
Those are the future stopped-audit/issuer/activation consumers' obligations.
"""
import base64
import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from desktop_assistant import current_mac_cutover_contract as contract


def _public(public):
    if type(public) is not bytes or not 0 < len(public) <= 8192:
        raise ValueError('当前Mac接管须提供原Ed25519公钥')
    key = serialization.load_pem_public_key(public)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError('当前Mac接管须提供原Ed25519公钥')
    raw = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return key, hashlib.sha256(raw).hexdigest()


def sign_authorization(request, source_stop, preparation, key, *, now):
    """Sign explicitly provided, already audited claims using the existing key."""
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError('当前Mac接管须使用调用者提供的原Ed25519身份')
    body = contract.make_body(request, preparation, source_stop, now=now)
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    if _public(public)[1] != body['request']['publisher_sha256']:
        raise ValueError('当前Mac接管签名与原发布公钥不符')
    signature = key.sign(contract.DOMAIN+contract.canonical(body))
    return contract.canonical({'body': body, 'signature': base64.b64encode(signature).decode('ascii')})


def verify_authorization(raw, public, expected_request):
    """Verify exact bound claims without expiring an established runtime record."""
    body = contract.authorization_body(raw, expected_request)
    signature = base64.b64decode(contract.decode(raw)['signature'], validate=True)
    key, fingerprint = _public(public)
    if body['request']['publisher_sha256'] != fingerprint:
        raise ValueError('当前Mac接管凭据与原发布公钥不符')
    key.verify(signature, contract.DOMAIN+contract.canonical(body))
    return body


def require_install_window(verified_body, *, now):
    return contract.require_install_window(verified_body, now=now)
