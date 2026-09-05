"""Unit tests for the CAMT.053 parser. No Django, no database."""

import datetime
import json
import pathlib
from decimal import Decimal

import pytest

from byro_finance_import_bank_files.importers.camt.parser import (
    BATCH_CURRENCY_MISMATCH,
    BATCH_MISSING_AMOUNT,
    BATCH_SUM_MISMATCH,
    CamtError,
    CamtTransaction,
    InvalidEntry,
    MalformedXml,
    MissingBookingDate,
    MultipleAccounts,
    NotCamt053,
    UnsafeXml,
    UnsupportedCamtMessage,
    is_placeholder,
    parse_amount_text,
    parse_camt053,
    select_external_id,
)

FIXTURES = pathlib.Path(__file__).parents[2] / "fixtures" / "camt"

#: Fixtures that must parse without error.
VALID_FIXTURES = sorted(
    path.name
    for path in FIXTURES.glob("*.xml")
    if path.name
    not in {
        "camt052.xml",
        "camt054.xml",
        "not_camt.xml",
        "no_namespace.xml",
        "malformed.xml",
        "empty_file.xml",
        "xxe.xml",
        "dtd.xml",
        "invalid_amount.xml",
        "proprietary_status.xml",
        "missing_dates.xml",
        "multiple_accounts.xml",
    }
)


def load(name):
    return (FIXTURES / name).read_bytes()


def parse(name):
    return parse_camt053(load(name))


def single(name):
    document = parse(name)
    assert len(document.transactions) == 1
    return document.transactions[0]


# -- versions ----------------------------------------------------------------


@pytest.mark.parametrize(
    "name, version",
    [
        ("camt053_001_02_incoming_transfer.xml", "camt.053.001.02"),
        ("camt053_001_08_incoming_transfer.xml", "camt.053.001.08"),
        ("camt053_001_12_incoming_transfer.xml", "camt.053.001.12"),
        ("austrian_namespace.xml", "camt.053.001.02"),
    ],
)
def test_version_detection(name, version):
    document = parse(name)
    assert document.version == version
    assert document.transactions[0].data["camt_version"] == version


def test_incoming_transfer_v02():
    tx = single("camt053_001_02_incoming_transfer.xml")
    assert isinstance(tx.amount, Decimal)
    assert tx.amount == Decimal("25.30")
    assert tx.currency == "EUR"
    assert tx.booking_date == datetime.date(2026, 1, 15)
    assert tx.value_date == datetime.date(2026, 1, 15)
    assert tx.counterparty_name == "Max Mustermann"
    assert tx.counterparty_iban == "DE89370400440532013000"
    assert tx.counterparty_bic == "COBADEFFXXX"
    assert tx.memo == "Mitglied NLL123 Jahresbeitrag 2026"
    assert tx.external_id == "2026011500001-001"
    assert tx.bank_reference == "2026011500001-001"
    assert tx.end_to_end_id == "NLL123-2026"
    assert tx.mandate_id is None
    assert tx.creditor_id is None
    # German banks leave Ntry/BkTxCd empty and put the GVC on the details.
    assert tx.transaction_code == "NTRF+166"
    assert tx.data == {
        "camt_version": "camt.053.001.02",
        "statement_id": "2026-01-15-001",
        "entry_reference": "1",
        "account_servicer_reference": "2026011500001-001",
        "transaction_id": "TX20260115000001",
        "proprietary_bank_transaction_code": "NTRF+166",
        "proprietary_bank_transaction_code_issuer": "ZKA",
        "additional_entry_info": "Ueberweisungsgutschrift",
    }


@pytest.mark.parametrize(
    "name, amount, counterparty, reference",
    [
        (
            "camt053_001_08_incoming_transfer.xml",
            "40.00",
            "Erika Musterfrau",
            "2026011500002-001",
        ),
        (
            "camt053_001_12_incoming_transfer.xml",
            "12.00",
            "Peter Beispiel",
            "2026011500003-001",
        ),
    ],
)
def test_incoming_transfer_newer_versions(name, amount, counterparty, reference):
    """Newer versions use ``Sts/Cd``, ``Pty/Nm``, ``BICFI`` and ``TxDtls/Amt``."""
    tx = single(name)
    assert tx.amount == Decimal(amount)
    assert tx.counterparty_name == counterparty
    assert tx.counterparty_iban == "DE89370400440532013000"
    assert tx.counterparty_bic == "COBADEFFXXX"
    assert tx.external_id == reference
    # The ISO domain code on the entry wins over the proprietary detail code.
    assert tx.transaction_code == "PMNT/RCDT/ESCT"
    assert tx.data["proprietary_bank_transaction_code"] == "NTRF+166"
    assert tx.data["uetr"] == "8a562c67-ca16-48ba-b074-65581be6f001"


def test_utf8_bom_is_tolerated():
    data = load("camt053_001_08_incoming_transfer.xml")
    assert data.startswith(b"\xef\xbb\xbf")
    assert len(parse_camt053(data).transactions) == 1


def test_latin1_encoding_is_honoured():
    tx = single("latin1_encoding.xml")
    assert tx.counterparty_name == "Jörg Müller-Lüdenscheidt"
    assert tx.memo == "Spende für das Vereinsfest äöüß"


def test_file_object_input():
    with (FIXTURES / "camt053_001_02_incoming_transfer.xml").open("rb") as f:
        assert len(parse_camt053(f).transactions) == 1


# -- direction and counterparty ---------------------------------------------


def test_outgoing_transfer_uses_creditor_and_negative_amount():
    tx = single("outgoing_transfer.xml")
    assert tx.amount == Decimal("-80.00")
    assert tx.counterparty_name == "Stadtwerke Musterstadt GmbH"
    assert tx.counterparty_iban == "DE75512108001245126199"
    assert tx.counterparty_bic == "SOLADEST600"
    assert tx.memo == "Rechnung 4711 Strom Januar"
    assert tx.end_to_end_id == "RE-4711"
    assert tx.transaction_code == "PMNT/ICDT/ESCT"
    assert tx.external_id == "2026011500004-001"


def test_direct_debit():
    tx = single("direct_debit.xml")
    assert tx.amount == Decimal("-12.99")
    assert tx.counterparty_name == "Streaming Dienst AG"
    assert tx.counterparty_iban == "DE75512108001245126199"
    assert tx.counterparty_bic == "SOLADEST600"
    assert tx.mandate_id == "MANDAT-987654"
    assert tx.end_to_end_id == "E2E-ABO-2026-01"
    # The creditor identifier is picked by shape; other IDs are ignored.
    assert tx.creditor_id == "DE98ZZZ09999999999"
    assert tx.transaction_code == "PMNT/IDDT/ESDD"
    assert tx.memo == "Abo Januar 2026 Kundennr 42"


def test_missing_counterparty_is_allowed():
    tx = single("missing_counterparty.xml")
    assert tx.amount == Decimal("10.00")
    assert tx.counterparty_name is None
    assert tx.counterparty_iban is None
    assert tx.counterparty_bic is None
    assert tx.memo == "Bareinzahlung"


def test_non_iban_account_is_kept_as_metadata():
    tx = single("other_account_id.xml")
    assert tx.counterparty_name == "Hans Beispiel"
    assert tx.counterparty_iban is None
    assert tx.data["counterparty_account_id"] == "1234567890"


def test_reversal_keeps_direction_and_original_debtor():
    tx = single("reversal.xml")
    # CdtDbtInd already describes the reversal booking: no sign flip.
    assert tx.amount == Decimal("-12.99")
    # Parties keep the roles of the original direct debit, so the other party
    # of this returned debit is the original debtor, not the association.
    assert tx.counterparty_name == "Max Mustermann"
    assert tx.counterparty_iban == "DE89370400440532013000"
    assert tx.counterparty_bic == "COBADEFFXXX"
    assert tx.creditor_id == "DE98ZZZ09999999999"
    assert tx.mandate_id == "MANDAT-NLL123"
    assert tx.memo == "Mitgliedsbeitrag 2026"
    assert tx.transaction_code == "PMNT/IDDT/RRTN"
    assert tx.data["reversal"] is True
    assert tx.data["return_reason"] == "AC01"
    assert tx.data["return_reason_info"] == "IBAN FEHLERHAFT"
    assert tx.data["proprietary_bank_transaction_code"] == "NDDT+109"


# -- remittance information -------------------------------------------------


def test_multiple_unstructured_lines_are_joined_with_single_spaces():
    tx = single("multiple_remittance_lines.xml")
    assert tx.memo == "SVWZ+Mitglied NLL125 Jahresbeitrag 2026 Danke!"


def test_structured_reference():
    tx = single("structured_reference.xml")
    assert tx.memo == "RF18539007547034 Spendenquittung erwuenscht"
    assert tx.data["creditor_reference"] == "RF18539007547034"


def test_entry_without_details_uses_additional_entry_info():
    tx = single("no_tx_details.xml")
    assert tx.amount == Decimal("-4.90")
    assert tx.memo == "Kontofuehrungsgebuehr Januar"
    assert tx.counterparty_name is None
    assert tx.transaction_code == "ACMT/MCOP/CHRG"
    assert tx.external_id == "2026011500006-001"
    assert tx.data["additional_entry_info"] == "Kontofuehrungsgebuehr Januar"


# -- batches -------------------------------------------------------------------


def test_batch_with_complete_details_is_split():
    document = parse("batch_booking.xml")
    transactions = document.transactions
    assert [tx.amount for tx in transactions] == [Decimal("25.00")] * 3
    assert [tx.counterparty_name for tx in transactions] == [
        "Max Mustermann",
        "Erika Musterfrau",
        "Peter Beispiel",
    ]
    assert [tx.end_to_end_id for tx in transactions] == [
        "NLL123-2026",
        "NLL124-2026",
        "NLL125-2026",
    ]
    # Parts never share the entry's reference as external ID.
    assert [tx.external_id for tx in transactions] == [
        "BATCH-001-1",
        "BATCH-001-2",
        "BATCH-001-3",
    ]
    assert [tx.bank_reference for tx in transactions] == [
        "BATCH-001-1",
        "BATCH-001-2",
        "BATCH-001-3",
    ]
    for tx in transactions:
        assert tx.booking_date == datetime.date(2026, 1, 15)
        assert tx.data["account_servicer_reference"] == "BATCH-001"
        assert tx.data["batch_message_id"] == "MSG-SAMMLER-1"
        assert tx.data["batch_payment_information_id"] == "PMT-INFO-1"
        assert tx.data["batch_transaction_count"] == 3
        assert "batch_details_not_split" not in tx.data
    assert transactions[0].memo == "Mitglied NLL123 Jahresbeitrag 2026"
    assert document.batch_fallbacks == 0


def test_batch_split_across_two_entry_details_blocks():
    transactions = parse("batch_booking_v08.xml").transactions
    assert [tx.amount for tx in transactions] == [Decimal("25.00")] * 3
    # Without its own AcctSvcrRef the third part falls back to TxId ...
    assert [tx.external_id for tx in transactions] == [
        "BATCH-001-1",
        "BATCH-001-2",
        "TX-BATCH-3",
    ]
    # ... while bank_reference falls back to the entry reference.
    assert transactions[2].bank_reference == "BATCH-001"
    assert [tx.data["batch_payment_information_id"] for tx in transactions] == [
        "PMT-INFO-1",
        "PMT-INFO-1",
        "PMT-INFO-2",
    ]


def test_batch_with_mixed_directions_is_split_with_signs():
    transactions = parse("batch_mixed_direction.xml").transactions
    assert [tx.amount for tx in transactions] == [
        Decimal("25.00"),
        Decimal("25.00"),
        Decimal("-30.00"),
    ]
    assert transactions[2].counterparty_name == "Peter Beispiel"


@pytest.mark.parametrize(
    "name, amount, reference, reason",
    [
        ("batch_sum_mismatch.xml", "95.00", "BATCH-002", BATCH_SUM_MISMATCH),
        ("batch_currency_mismatch.xml", "75.00", "BATCH-003", BATCH_CURRENCY_MISMATCH),
        ("batch_without_amounts.xml", "75.00", "BATCH-004", BATCH_MISSING_AMOUNT),
    ],
)
def test_inconsistent_batch_is_not_split(name, amount, reference, reason):
    document = parse(name)
    assert document.batch_fallbacks == 1
    tx = document.transactions[0]
    assert len(document.transactions) == 1
    assert tx.amount == Decimal(amount)
    assert tx.external_id == reference
    assert tx.counterparty_name is None
    assert tx.memo == "Sammelgutschrift"
    assert tx.data["batch_details_not_split"] == reason
    assert tx.data["batch_transaction_count"] == 3


def test_batch_summary_without_detail_amounts():
    tx = single("batch_summary_only.xml")
    assert tx.amount == Decimal("-300.00")
    assert tx.memo == "Sammelueberweisung"
    assert tx.data["batch_transaction_count"] == 12
    assert tx.data["batch_payment_information_id"] == "PMT-INFO-5"
    assert "batch_details_not_split" not in tx.data


# -- references and external IDs --------------------------------------------


def test_placeholders_are_never_used_as_references():
    tx = single("notprovided_reference.xml")
    assert tx.external_id is None
    assert tx.end_to_end_id is None
    assert tx.mandate_id is None
    assert tx.bank_reference is None
    assert "account_servicer_reference" not in tx.data
    assert tx.memo == "Mitglied NLL123"


def test_repeated_bank_reference_is_not_used_as_external_id():
    transactions = parse("duplicate_account_servicer_reference.xml").transactions
    assert len(transactions) == 2
    assert [tx.external_id for tx in transactions] == [None, None]
    assert [tx.bank_reference for tx in transactions] == ["Bankreferenz"] * 2
    assert [tx.data["account_servicer_reference"] for tx in transactions] == [
        "Bankreferenz"
    ] * 2


@pytest.mark.parametrize(
    "value, expected",
    [
        ("NOTPROVIDED", True),
        ("NOT PROVIDED", True),
        ("notprovided", True),
        ("N/A", True),
        ("NONE", True),
        ("NULL", True),
        ("NONREF", True),
        ("NOTAVAIL", True),
        ("UNKNOWN", True),
        ("-", True),
        ("/", True),
        ("", True),
        (None, True),
        ("2026011500001-001", False),
        ("NLL123-2026", False),
        ("NA1", False),
        ("NONREF1", False),
    ],
)
def test_is_placeholder(value, expected):
    assert is_placeholder(value) is expected


def test_select_external_id_priorities():
    refs = {"AcctSvcrRef": "TX-REF", "TxId": "TXID", "UETR": "UETR"}
    assert select_external_id("ENTRY-REF", refs, split=False) == "ENTRY-REF"
    assert select_external_id("ENTRY-REF", refs, split=True) == "TX-REF"
    assert select_external_id(None, {"TxId": "TXID", "UETR": "UETR"}, False) == "TXID"
    assert select_external_id(None, {"UETR": "UETR"}, False) == "UETR"
    assert select_external_id("ENTRY-REF", {}, split=True) is None
    assert select_external_id(None, {}, split=False) is None


# -- amounts ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [("25.30", "25.30"), ("25", "25.00"), ("25.300", "25.30"), ("0.00", "0.00")],
)
def test_parse_amount_text(text, expected):
    value = parse_amount_text(text)
    assert isinstance(value, Decimal)
    assert value == Decimal(expected)
    assert value.as_tuple().exponent == -2


@pytest.mark.parametrize(
    "text",
    ["25.305", "-25.00", "+25.00", "1,000.00", "abc", "NaN", "Infinity", "", None],
)
def test_parse_amount_text_rejects(text):
    with pytest.raises(ValueError):
        parse_amount_text(text)


def test_amount_with_more_than_two_decimals_is_an_error():
    with pytest.raises(InvalidEntry) as excinfo:
        parse("invalid_amount.xml")
    assert excinfo.value.position == 1
    assert excinfo.value.reason == "invalid amount"


def test_zero_amount_entry_is_skipped():
    document = parse("zero_amount.xml")
    assert document.entry_count == 2
    assert document.skipped_entries == 1
    assert [tx.amount for tx in document.transactions] == [Decimal("5.00")]


def test_foreign_currency_is_passed_through_unchanged():
    tx = single("foreign_currency.xml")
    assert tx.currency == "USD"
    assert tx.amount == Decimal("100.00")


# -- status --------------------------------------------------------------------


def test_only_booked_entries_become_transactions():
    document = parse("entry_status.xml")
    assert document.entry_count == 4
    assert document.skipped_entries == 3
    assert [tx.amount for tx in document.transactions] == [Decimal("10.00")]


def test_proprietary_status_is_rejected():
    with pytest.raises(InvalidEntry) as excinfo:
        parse("proprietary_status.xml")
    assert excinfo.value.position == 1
    assert excinfo.value.reason == "proprietary status"


# -- dates -------------------------------------------------------------------------


def test_datetime_dates_keep_the_written_date():
    tx = single("datetime_dates.xml")
    assert tx.booking_date == datetime.date(2026, 1, 16)
    assert tx.value_date == datetime.date(2026, 1, 17)


def test_missing_booking_date_falls_back_to_value_date():
    tx = single("missing_booking_date.xml")
    assert tx.booking_date == datetime.date(2026, 1, 15)
    assert tx.value_date == datetime.date(2026, 1, 15)
    assert tx.data["booking_date_source"] == "value_date"


def test_missing_dates_are_an_error():
    with pytest.raises(MissingBookingDate) as excinfo:
        parse("missing_dates.xml")
    assert excinfo.value.position == 1


# -- statements and accounts ---------------------------------------------------


def test_multiple_statements_are_processed():
    document = parse("multiple_statements.xml")
    assert [statement.id for statement in document.statements] == [
        "2026-01-15-001",
        "2026-01-16-001",
    ]
    assert {statement.account_id for statement in document.statements} == {
        "DE02120300000000202051"
    }
    assert [tx.amount for tx in document.transactions] == [
        Decimal("25.00"),
        Decimal("30.00"),
    ]
    assert [tx.data["statement_id"] for tx in document.transactions] == [
        "2026-01-15-001",
        "2026-01-16-001",
    ]


def test_multiple_accounts_are_rejected():
    with pytest.raises(MultipleAccounts) as excinfo:
        parse("multiple_accounts.xml")
    assert excinfo.value.count == 2


def test_empty_statement_yields_no_transactions():
    document = parse("empty_statement.xml")
    assert document.entry_count == 0
    assert document.transactions == []


# -- invalid input -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name, error, attribute",
    [
        ("camt052.xml", UnsupportedCamtMessage, ("message_type", "camt.052.001.02")),
        ("camt054.xml", UnsupportedCamtMessage, ("message_type", "camt.054.001.02")),
        ("not_camt.xml", NotCamt053, ("reason", "root")),
        ("no_namespace.xml", NotCamt053, ("reason", "namespace")),
        ("archive.zip", NotCamt053, ("reason", "zip")),
        ("malformed.xml", MalformedXml, None),
        ("empty_file.xml", MalformedXml, None),
        ("xxe.xml", UnsafeXml, None),
        ("dtd.xml", UnsafeXml, None),
    ],
)
def test_invalid_input(name, error, attribute):
    with pytest.raises(error) as excinfo:
        parse(name)
    if attribute:
        assert getattr(excinfo.value, attribute[0]) == attribute[1]


def test_camt053_without_statements_is_rejected():
    data = load("camt052.xml").replace(b"camt.052.001.02", b"camt.053.001.02")
    with pytest.raises(NotCamt053) as excinfo:
        parse_camt053(data)
    assert excinfo.value.reason == "structure"


@pytest.mark.parametrize(
    "name",
    [
        "camt052.xml",
        "not_camt.xml",
        "malformed.xml",
        "xxe.xml",
        "invalid_amount.xml",
        "proprietary_status.xml",
        "missing_dates.xml",
        "multiple_accounts.xml",
    ],
)
def test_error_messages_contain_no_bank_data(name):
    with pytest.raises(CamtError) as excinfo:
        parse(name)
    message = str(excinfo.value)
    for sensitive in ("Mustermann", "Musterfrau", "DE89", "DE02", "NLL", "passwd"):
        assert sensitive not in message


# -- invariants over all valid fixtures ---------------------------------------


@pytest.mark.parametrize("name", VALID_FIXTURES)
def test_valid_fixture_invariants(name):
    document = parse(name)
    for tx in document.transactions:
        assert isinstance(tx, CamtTransaction)
        assert isinstance(tx.amount, Decimal)
        assert tx.amount != 0
        assert tx.amount.as_tuple().exponent == -2
        assert isinstance(tx.booking_date, datetime.date)
        assert tx.value_date is None or isinstance(tx.value_date, datetime.date)
        assert isinstance(tx.memo, str)
        assert tx.counterparty_iban is None or " " not in tx.counterparty_iban
        assert tx.external_id is None or not is_placeholder(tx.external_id)
        json.dumps(tx.data)
        assert tx.data["camt_version"] == document.version
    external_ids = [tx.external_id for tx in document.transactions if tx.external_id]
    assert len(external_ids) == len(set(external_ids))
