"""P0-PROD-12C: read-only account.move.line verification reader.

Proves the reader always scopes to the exact requested move_id, requests only the fixed
field list, normalizes every Odoo many2one value to a safe integer id (or None for a
legitimately empty relation), validates tax_ids and the numeric fields, fails closed on
every malformed/unexpected shape (including a returned line whose own move_id disagrees
with the one requested), and has no path to any Odoo write.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.erp.exceptions import ErpRepositoryResponseError
from app.erp.odoo.account_move_line_verification_reader import (
    ACCOUNT_MOVE_LINE_VERIFICATION_FIELDS,
    OdooAccountMoveLineVerificationReader,
)
from app.erp.odoo.adapter import OdooReadOnlyAdapter

MOVE_ID = 62


class _FakeOdooJson2Client:
    """Structurally has no create/write/unlink/action_post/payment/reconciliation method
    at all -- only search_read, matching the real OdooJson2Client's read-only surface as
    consumed by OdooReadOnlyAdapter. An attempted write would be an AttributeError, not a
    silent no-op."""

    def __init__(self, *, records: list[dict] | None = None) -> None:
        self._records = records if records is not None else []
        self.search_calls: list[dict] = []

    async def search_read(self, *, model, domain, fields, limit=20, offset=0):
        self.search_calls.append({"model": model, "domain": domain, "fields": list(fields)})
        return list(self._records)


def _reader(*, records: list[dict] | None = None) -> tuple[OdooAccountMoveLineVerificationReader, _FakeOdooJson2Client]:
    client = _FakeOdooJson2Client(records=records)
    adapter = OdooReadOnlyAdapter(client=client, retry_backoff_seconds=0)
    return OdooAccountMoveLineVerificationReader(adapter=adapter), client


def _valid_line(**overrides: object) -> dict:
    line = {
        "id": 129,
        "move_id": [MOVE_ID, "HD12026000964602"],
        "product_id": [389, "Stanley The Iceflow Flip Straw 2.0 Pipet"],
        "product_uom_id": [1, "Units"],
        "quantity": "1.0",
        "price_unit": "2166.000000",
        "tax_ids": [34],
        "account_id": [247, "Expense Account"],
    }
    line.update(overrides)
    return line


def test_search_scoped_to_exact_requested_move_id() -> None:
    """3."""
    reader, client = _reader(records=[])
    reader.read_lines_for_move(move_id=MOVE_ID)
    assert client.search_calls == [
        {
            "model": "account.move.line",
            "domain": [["move_id", "=", MOVE_ID]],
            "fields": list(ACCOUNT_MOVE_LINE_VERIFICATION_FIELDS),
        }
    ]


def test_requests_only_the_fixed_minimal_field_list() -> None:
    """4."""
    reader, client = _reader(records=[])
    reader.read_lines_for_move(move_id=MOVE_ID)
    assert client.search_calls[0]["fields"] == [
        "id",
        "move_id",
        "product_id",
        "product_uom_id",
        "quantity",
        "price_unit",
        "tax_ids",
        "account_id",
    ]


def test_move_id_normalized_to_the_requested_integer() -> None:
    """5."""
    reader, _ = _reader(records=[_valid_line()])
    (line,) = reader.read_lines_for_move(move_id=MOVE_ID)
    assert line.move_id == MOVE_ID
    assert type(line.move_id) is int


def test_product_id_normalized_from_many2one_tuple() -> None:
    """6."""
    reader, _ = _reader(records=[_valid_line(product_id=[389, "Product"])])
    (line,) = reader.read_lines_for_move(move_id=MOVE_ID)
    assert line.product_id == 389
    assert type(line.product_id) is int


def test_product_uom_id_normalized_from_many2one_tuple() -> None:
    """7."""
    reader, _ = _reader(records=[_valid_line(product_uom_id=[1, "Units"])])
    (line,) = reader.read_lines_for_move(move_id=MOVE_ID)
    assert line.product_uom_id == 1
    assert type(line.product_uom_id) is int


def test_account_id_normalized_where_applicable() -> None:
    """8."""
    reader, _ = _reader(records=[_valid_line(account_id=[247, "Expense"])])
    (line,) = reader.read_lines_for_move(move_id=MOVE_ID)
    assert line.account_id == 247

    reader_absent, _ = _reader(records=[_valid_line(account_id=False)])
    (line_absent,) = reader_absent.read_lines_for_move(move_id=MOVE_ID)
    assert line_absent.account_id is None


def test_quantity_is_returned_correctly() -> None:
    """9."""
    reader, _ = _reader(records=[_valid_line(quantity="1.000")])
    (line,) = reader.read_lines_for_move(move_id=MOVE_ID)
    assert line.quantity == Decimal("1.000")


def test_price_unit_is_returned_correctly() -> None:
    """10."""
    reader, _ = _reader(records=[_valid_line(price_unit="2166.000000")])
    (line,) = reader.read_lines_for_move(move_id=MOVE_ID)
    assert line.price_unit == Decimal("2166.000000")


def test_tax_ids_validated_and_returned_correctly() -> None:
    """11."""
    reader, _ = _reader(records=[_valid_line(tax_ids=[34])])
    (line,) = reader.read_lines_for_move(move_id=MOVE_ID)
    assert line.tax_ids == (34,)

    reader_empty, _ = _reader(records=[_valid_line(tax_ids=False)])
    (line_empty,) = reader_empty.read_lines_for_move(move_id=MOVE_ID)
    assert line_empty.tax_ids == ()


@pytest.mark.parametrize(
    "malformed_product_id",
    [
        "389",
        {"id": 389},
        [],
        ["not-an-int", "Product"],
    ],
)
def test_malformed_many2one_response_fails_closed(malformed_product_id: object) -> None:
    """12."""
    reader, _ = _reader(records=[_valid_line(product_id=malformed_product_id)])
    with pytest.raises(ErpRepositoryResponseError):
        reader.read_lines_for_move(move_id=MOVE_ID)


def test_bool_true_product_id_fails_closed() -> None:
    """13. `False` is the legitimate Odoo "empty many2one" shape (covered by other tests);
    `True` is never a legitimate Odoo value and must not be silently treated as absent."""
    reader, _ = _reader(records=[_valid_line(product_id=True)])
    with pytest.raises(ErpRepositoryResponseError):
        reader.read_lines_for_move(move_id=MOVE_ID)


def test_bool_true_move_id_fails_closed() -> None:
    """13b."""
    reader, _ = _reader(records=[_valid_line(move_id=True)])
    with pytest.raises(ErpRepositoryResponseError):
        reader.read_lines_for_move(move_id=MOVE_ID)


@pytest.mark.parametrize("invalid_id", [0, -1])
def test_non_positive_requested_move_id_fails_closed(invalid_id: int) -> None:
    """14."""
    reader, _ = _reader(records=[])
    with pytest.raises(ErpRepositoryResponseError):
        reader.read_lines_for_move(move_id=invalid_id)


@pytest.mark.parametrize("invalid_id", [0, -5])
def test_non_positive_many2one_id_fails_closed(invalid_id: int) -> None:
    """14b."""
    reader, _ = _reader(records=[_valid_line(product_id=[invalid_id, "Bad"])])
    with pytest.raises(ErpRepositoryResponseError):
        reader.read_lines_for_move(move_id=MOVE_ID)


def test_line_belonging_to_a_different_move_fails_closed() -> None:
    """15. Odoo's own applied domain is never trusted blindly -- the returned move_id on
    each line must independently agree with the requested move_id."""
    reader, _ = _reader(records=[_valid_line(move_id=[MOVE_ID + 1, "Some Other Bill"])])
    with pytest.raises(ErpRepositoryResponseError):
        reader.read_lines_for_move(move_id=MOVE_ID)


@pytest.mark.parametrize(
    "malformed_tax_ids",
    [
        "34",
        [34, "bad"],
        [34, True],
        [34, 0],
        [34, -1],
    ],
)
def test_malformed_tax_ids_fail_closed(malformed_tax_ids: object) -> None:
    """16."""
    reader, _ = _reader(records=[_valid_line(tax_ids=malformed_tax_ids)])
    with pytest.raises(ErpRepositoryResponseError):
        reader.read_lines_for_move(move_id=MOVE_ID)


def test_reader_performs_no_odoo_writes() -> None:
    """17. The fake client structurally has no create/write/unlink/action_post/payment/
    reconciliation method; calling the reader can therefore never reach one."""
    reader, client = _reader(records=[_valid_line()])
    for attr in ("create", "write", "unlink", "action_post", "button_validate", "send", "cancel"):
        assert not hasattr(client, attr)
    reader.read_lines_for_move(move_id=MOVE_ID)


def test_real_pilot_shape_reads_back_the_expected_line() -> None:
    """End-to-end shape check against the P0-PROD-10G pilot line -- values passed as test
    fixture data only, never hard-coded into the reader itself."""
    reader, _ = _reader(
        records=[
            _valid_line(
                product_id=[389, "Stanley The Iceflow Flip Straw 2.0 Pipet"],
                product_uom_id=[1, "Units"],
                quantity="1.000",
                price_unit="2166.000000",
                tax_ids=[34],
            )
        ]
    )
    (line,) = reader.read_lines_for_move(move_id=MOVE_ID)
    assert line.product_id == 389
    assert line.product_uom_id == 1
    assert line.quantity == Decimal("1.000")
    assert line.price_unit == Decimal("2166.000000")
    assert line.tax_ids == (34,)
