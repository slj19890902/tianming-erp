"""Publication receipt shape only; not a source signature or root audit."""
from copy import deepcopy
import pytest

from desktop_assistant import current_mac_publication as g,current_mac_worker_contract as c
from tests.desktop_assistant.test_current_mac_worker_contract import chain


@pytest.fixture
def claim(chain):
    plans,_,replies,hashes,_=chain
    raw=c.canonical(replies['activate']['result']['record'])
    value=dict(type=g.TYPE,attempt_id=plans['activate']['attempt_id'],plan_sha256=hashes['activate'],
        activation_sha256=c.raw_digest(raw),request_sha256=plans['activate']['request_sha256'],ready=True)
    return value,raw


def test_exact_commit_matches_activation_and_request(claim):
    value,raw=claim
    assert g.validate(value,activation_raw=raw,ready=True)==value


@pytest.mark.parametrize('field',sorted(g.FIELDS))
@pytest.mark.parametrize('fault',['missing','wrong'])
def test_bad_publication_claim_rejected(claim,field,fault):
    value,raw=claim;value=deepcopy(value)
    if fault=='missing':value.pop(field)
    else:value[field]=0
    with pytest.raises(ValueError):g.validate(value,activation_raw=raw,ready=True)


@pytest.mark.parametrize('field',['activation_sha256','request_sha256'])
def test_other_activation_or_request_cannot_reuse_completion(claim,field):
    value,raw=claim;value[field]='0'*64
    with pytest.raises(ValueError):g.validate(value,activation_raw=raw,ready=True)


def test_pending_ready_bool_is_explicit_and_not_truthy(claim):
    value,raw=claim;value['ready']=False
    g.validate(value,activation_raw=raw,ready=False)
    with pytest.raises(ValueError):g.validate(value,activation_raw=raw,ready=True)
    with pytest.raises(ValueError):g.validate(value,activation_raw=raw,ready=1)


def test_extra_claim_rejected(claim):
    value,_=claim;value['approved']=True
    with pytest.raises(ValueError):g.validate(value)
