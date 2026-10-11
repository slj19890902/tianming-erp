from copy import deepcopy

import pytest

from desktop_assistant import order_inventory_contract as contract


SIGNED = {'reader_capabilities': {contract.CAPABILITY: 1}}
MARKER = {'reader_capability': contract.CAPABILITY, 'version': 1,
          'next_release_must_preserve_reader_contract': True,
          'pre_activation_backup': r'Z:\original\evidence.tmbackup'}


def test_original_marker_and_its_historical_paths_are_preserved_independently():
    state = {contract.STATE_KEY: deepcopy(MARKER)}
    copied = contract.for_backup(state, SIGNED)
    recovered = contract.for_recovery({contract.STATE_KEY: copied}, SIGNED, 'a' * 64)
    assert recovered == MARKER
    copied['version'] = 99
    assert state[contract.STATE_KEY] == recovered == MARKER


@pytest.mark.parametrize('marker', [{}, [], False, {'reader_capability': contract.CAPABILITY, 'version': True},
    {**MARKER, 'version': 2}, {**MARKER, 'next_release_must_preserve_reader_contract': False}])
def test_malformed_record_cannot_disappear_during_backup_or_recovery(marker):
    with pytest.raises(ValueError, match='reader 保护'):
        contract.for_backup({contract.STATE_KEY: marker}, SIGNED)
    with pytest.raises(ValueError, match='reader 保护'):
        contract.for_recovery({contract.STATE_KEY: marker}, SIGNED, 'a' * 64)


@pytest.mark.parametrize('value', [True, False, 0, 2, '1'])
def test_ambiguous_or_unknown_signed_reader_is_rejected(value):
    manifest = {'reader_capabilities': {contract.CAPABILITY: value}}
    with pytest.raises(ValueError, match='reader 保护'):
        contract.for_recovery({}, manifest, 'a' * 64)


@pytest.mark.parametrize('value', [[], False, 0, '', None])
def test_corrupted_falsy_reader_container_is_not_absent(value):
    with pytest.raises(ValueError, match='reader 保护'):
        contract.for_recovery({}, {'reader_capabilities': value}, 'a' * 64)


def test_signed_reader_and_marker_cannot_disagree():
    with pytest.raises(ValueError, match='reader 保护'):
        contract.for_recovery({contract.STATE_KEY: MARKER}, {}, 'a' * 64)
    assert contract.for_recovery({}, {}, 'a' * 64) is None
    marker = contract.for_recovery({}, SIGNED, 'a' * 64)
    assert marker['version'] == 1
    assert marker['recovery_evidence']['original_activation_record_included'] is False
