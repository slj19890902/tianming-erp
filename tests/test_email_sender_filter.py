import pytest
from app.services.email_sender_filter import normalize_senders, sender_matches


def test_normalization_and_complete_mailbox_matching():
    allowed = normalize_senders([' Customer@Example.com ', 'customer@example.com'])
    assert allowed == ['customer@example.com']
    assert sender_matches(['采购 <CUSTOMER@example.com>'], allowed)
    assert not sender_matches(['othercustomer@example.com'], allowed)
    assert not sender_matches(['customer@example.com.evil.test'], allowed)
    assert not sender_matches(['customer@example.com, other@example.com'], allowed)
    assert not sender_matches([], allowed)
    assert not sender_matches(['customer@example.com'], [])


@pytest.mark.parametrize('value', ['*', '@example.com', 'a@example.com\r\nFROM other@example.com', 'Name <a@example.com>', 'a@', ''])
def test_invalid_addresses_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_senders([value])
