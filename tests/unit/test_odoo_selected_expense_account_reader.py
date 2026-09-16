from __future__ import annotations

from app.erp.odoo.selected_expense_account_reader import OdooSelectedAccountReader


class _RecordingAdapter:
    def __init__(self, records: list[dict]) -> None:
        self._records = records
        self.calls: list[dict] = []

    def search_read_all(self, *, model, domain, fields, max_records=None, page_size=None):
        self.calls.append({"model": model, "domain": domain, "fields": fields, "max_records": max_records})
        return tuple(self._records)


def test_find_accounts_by_ids_reads_real_field_contract() -> None:
    adapter = _RecordingAdapter([{"id": 9101, "company_ids": [1, 7]}])
    reader = OdooSelectedAccountReader(adapter=adapter)

    records = reader.find_accounts_by_ids((9101,))

    assert records == (type(records[0])(id=9101, company_ids=(1, 7)),)
    assert adapter.calls[0]["model"] == "account.account"
    assert adapter.calls[0]["domain"] == [["id", "in", [9101]]]
    assert adapter.calls[0]["fields"] == ["id", "company_ids"]


def test_find_accounts_by_ids_empty_input_short_circuits() -> None:
    adapter = _RecordingAdapter([])
    reader = OdooSelectedAccountReader(adapter=adapter)

    assert reader.find_accounts_by_ids(()) == ()
    assert adapter.calls == []


def test_find_accounts_by_ids_filters_non_positive_and_boolean_ids() -> None:
    adapter = _RecordingAdapter([{"id": 9101, "company_ids": [1]}])
    reader = OdooSelectedAccountReader(adapter=adapter)

    reader.find_accounts_by_ids((0, -1, True, 9101))

    assert adapter.calls[0]["domain"] == [["id", "in", [9101]]]


def test_malformed_company_ids_shape_yields_empty_tuple() -> None:
    adapter = _RecordingAdapter([{"id": 9101, "company_ids": False}])
    reader = OdooSelectedAccountReader(adapter=adapter)

    records = reader.find_accounts_by_ids((9101,))

    assert records[0].company_ids == ()
