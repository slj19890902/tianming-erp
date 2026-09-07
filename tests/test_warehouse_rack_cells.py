from types import SimpleNamespace

from app.services.warehouse_rack_cells import _map_rack_letter


class _RackIdentityQuery:
    def scalar(self, _statement):
        return None

    def scalars(self, _statement):
        return type("_Rows", (), {
            "all": lambda self: [chr(code) for code in range(ord("A"), ord("Z") + 1)]
        })()


def test_rack_identity_continues_after_single_letter_alphabet() -> None:
    assert _map_rack_letter(
        _RackIdentityQuery(),
        area=SimpleNamespace(id=1, area_name="E2货架区"),
        map_rack_id="new-rack",
    ) == "AA"
