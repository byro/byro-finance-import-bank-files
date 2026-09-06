"""Unit tests for the MT940 parser. No Django, no database."""

import datetime
import json
import logging
import pathlib
from decimal import Decimal

import pytest

from byro_finance_import_bank_files.importers.mt940 import parser
from byro_finance_import_bank_files.importers.mt940.parser import (
    InvalidEntry,
    InvalidStatement,
    MissingAccountIdentification,
    MissingCurrency,
    Mt940Error,
    Mt940Transaction,
    MultipleAccounts,
    NotMt940,
    UndecodableFile,
    UnparseableField,
    UnsupportedEncoding,
    classify_account,
    classify_bank,
    decode,
    is_placeholder,
    parse_mt940,
)

FIXTURES = pathlib.Path(__file__).parents[2] / "fixtures" / "mt940"

INVALID_FIXTURES = {
    "malformed.mt940": NotMt940,
    "utf16_bom.mt940": UnsupportedEncoding,
    "undecodable.mt940": UndecodableFile,
    "invalid_statement_line.mt940": UnparseableField,
    "invalid_date.mt940": InvalidStatement,
    "invalid_amount.mt940": InvalidStatement,
    "missing_type_code.mt940": InvalidStatement,
    "missing_currency.mt940": MissingCurrency,
    "missing_account.mt940": MissingAccountIdentification,
    "zero_amount.mt940": InvalidEntry,
    "multiple_accounts.mt940": MultipleAccounts,
}

#: Fixtures that must parse without error.
VALID_FIXTURES = sorted(
    path.name for path in FIXTURES.glob("*.mt940") if path.name not in INVALID_FIXTURES
)

SENSITIVE = ("Mustermann", "Musterfrau", "DE89", "DE02", "NLL", "GARBAGE", "STARTUMSE")


def load(name):
    return (FIXTURES / name).read_bytes()


def parse(name):
    return parse_mt940(load(name))


def single(name):
    document = parse(name)
    assert len(document.transactions) == 1
    return document.transactions[0]


# -- basic transactions ------------------------------------------------------


def test_basic_credit():
    document = parse("basic_credit.mt940")
    assert document.encoding == "utf-8"
    (statement,) = document.statements
    assert statement.reference == "STARTUMSE"
    assert statement.account_id == "DE02120300000000202051"
    assert statement.statement_number == "00001"
    assert statement.sequence_number == "001"
    assert statement.currency == "EUR"
    assert statement.opening_balance == Decimal("1000.00")
    assert statement.closing_balance == Decimal("1025.30")
    assert statement.balance_mismatch is False
    assert statement.information == []

    (tx,) = statement.transactions
    assert isinstance(tx.amount, Decimal)
    assert tx.amount == Decimal("25.30")
    assert tx.currency == "EUR"
    assert tx.booking_date == datetime.date(2026, 1, 15)
    assert tx.value_date == datetime.date(2026, 1, 15)
    assert type(tx.booking_date) is datetime.date
    assert tx.memo == "Mitglied NLL123 Jahresbeitrag 2026"
    assert tx.counterparty_name == "Max Mustermann"
    assert tx.counterparty_iban == "DE89370400440532013000"
    assert tx.counterparty_bic == "COBADEFFXXX"
    assert tx.external_id == "2026011500001"
    assert tx.bank_reference == "2026011500001"
    assert tx.end_to_end_id == "NLL123-2026"
    assert tx.mandate_id is None
    assert tx.creditor_id is None
    assert tx.transaction_code == "NTRF"
    assert tx.data == {
        "structured_details": True,
        "statement_reference": "STARTUMSE",
        "statement_number": "00001",
        "statement_sequence_number": "001",
        "debit_credit_mark": "C",
        "business_transaction_code": "166",
        "posting_text": "GUTSCHR. UEBERWEISUNG",
        "prima_nota": "9310",
    }


def test_crlf_line_endings_are_accepted():
    data = load("basic_credit.mt940")
    assert b"\r\n" in data
    assert len(parse_mt940(data).transactions) == 1


def test_basic_debit_is_negative_with_counterparty():
    tx = single("basic_debit.mt940")
    assert tx.amount == Decimal("-80.00")
    assert tx.counterparty_name == "Stadtwerke Musterstadt GmbH"
    assert tx.counterparty_iban == "DE75512108001245126199"
    assert tx.counterparty_bic == "SOLADEST600"
    assert tx.memo == "Rechnung 4711 Strom Januar"
    assert tx.end_to_end_id == "RE-4711"
    assert tx.data["debit_credit_mark"] == "D"
    assert tx.data["business_transaction_code"] == "116"
    assert "reversal" not in tx.data


def test_credit_and_debit_in_one_statement():
    document = parse("credit_and_debit.mt940")
    assert [tx.amount for tx in document.transactions] == [
        Decimal("25.30"),
        Decimal("-80.00"),
    ]
    assert document.balance_mismatches == 0


@pytest.mark.parametrize(
    "name, amount, mark, reversal",
    [
        ("basic_credit.mt940", "25.30", "C", False),
        ("basic_debit.mt940", "-80.00", "D", False),
        ("reversal_credit.mt940", "-25.30", "RC", True),
        ("reversal_debit.mt940", "12.99", "RD", True),
    ],
)
def test_debit_credit_marks_decide_the_sign(name, amount, mark, reversal):
    tx = single(name)
    assert tx.amount == Decimal(amount)
    assert tx.data["debit_credit_mark"] == mark
    assert tx.data.get("reversal", False) is reversal


def test_reversal_of_debit_keeps_sepa_references():
    tx = single("reversal_debit.mt940")
    assert tx.counterparty_name == "Streaming Dienst AG"
    assert tx.end_to_end_id == "E2E-ABO-2026-01"
    assert tx.mandate_id == "MANDAT-987654"
    assert tx.transaction_code == "N009"
    assert tx.data["business_transaction_code"] == "159"


def test_transaction_type_and_gvc_are_kept_apart():
    tx = single("basic_credit.mt940")
    assert tx.transaction_code == "NTRF"
    assert tx.data["business_transaction_code"] == "166"
    assert "+" not in tx.transaction_code


def test_amounts_are_taken_as_written():
    document = parse("multiple_transactions.mt940")
    for tx in document.transactions:
        assert isinstance(tx.amount, Decimal)
    assert [tx.amount for tx in document.transactions] == [
        Decimal("25.30"),
        Decimal("25.30"),
        Decimal("-4.90"),
        Decimal("100.00"),
    ]


# -- dates -------------------------------------------------------------------


def test_missing_entry_date_falls_back_to_value_date():
    tx = single("missing_entry_date.mt940")
    assert tx.booking_date == datetime.date(2026, 1, 15)
    assert tx.value_date == datetime.date(2026, 1, 15)
    assert tx.data["booking_date_source"] == "value_date"


def test_entry_date_year_boundary_in_both_directions():
    first, second = parse("year_boundary.mt940").transactions
    # Value date in January, entry date (MMDD) in the previous December.
    assert first.value_date == datetime.date(2026, 1, 2)
    assert first.booking_date == datetime.date(2025, 12, 30)
    # Value date in December, entry date in the following January.
    assert second.value_date == datetime.date(2025, 12, 30)
    assert second.booking_date == datetime.date(2026, 1, 2)
    for tx in (first, second):
        assert "booking_date_source" not in tx.data


# -- :86: --------------------------------------------------------------------


def test_unstructured_86_becomes_the_memo_without_guessing():
    tx = single("unstructured_86.mt940")
    assert tx.memo == (
        "Mitglied NLL123 Jahresbeitrag 2026 Max Mustermann IBAN DE89370400440532013000"
    )
    # Nothing is extracted from free text.
    assert tx.counterparty_name is None
    assert tx.counterparty_iban is None
    assert tx.counterparty_bic is None
    assert tx.end_to_end_id is None
    assert tx.data["structured_details"] is False


def test_structured_86_fields():
    document = parse("structured_86.mt940")
    credit, direct_debit, return_debit = document.transactions

    # Words split across ?2x sub-fields and ?32/?33 are joined seamlessly.
    assert credit.memo == "Mitglied NLL123 Jahresbeitrag 2026"
    assert credit.counterparty_name == "Max Mustermann"
    assert credit.end_to_end_id == "NLL123-2026"
    # KREF+ is the customer reference of the :86:, the :61: one was NONREF.
    assert credit.data["customer_reference"] == "KUNDENREF-1"
    assert "account_owner_reference" not in credit.data
    assert credit.data["text_key_extension"] == "997"

    assert direct_debit.amount == Decimal("12.99")
    assert direct_debit.counterparty_name == "Peter Beispiel"
    assert direct_debit.counterparty_iban == "DE44500105175407324931"
    assert direct_debit.counterparty_bic == "BYLADEM1001"
    assert direct_debit.end_to_end_id == "E2E-ABO-2026-01"
    assert direct_debit.mandate_id == "MANDAT-987654"
    assert direct_debit.creditor_id == "DE98ZZZ09999999999"
    assert direct_debit.transaction_code == "N005"
    assert direct_debit.data["business_transaction_code"] == "105"
    assert direct_debit.data["purpose_code"] == "OTHR"
    assert direct_debit.data["ultimate_debtor_name"] == "Erika Musterfrau"

    assert return_debit.amount == Decimal("-12.99")
    assert return_debit.memo == "Mitgliedsbeitrag 2025"
    assert return_debit.mandate_id == "MANDAT-NLL124"
    assert return_debit.data["posting_text"] == "RUECKLASTSCHRIFT"
    assert return_debit.data["text_key_extension"] == "912"
    assert return_debit.data["compensation_amount"] == "3,00"
    assert return_debit.data["original_amount"] == "9,99"


def test_posting_text_is_the_memo_fallback():
    document = parse("multiple_transactions.mt940")
    fee = document.transactions[2]
    assert fee.amount == Decimal("-4.90")
    assert fee.memo == "Kontofuehrungsgebuehr Januar"
    assert fee.counterparty_name is None
    assert fee.transaction_code == "NMSC"
    assert fee.data["posting_text"] == "ENTGELT"


def test_legacy_account_number_and_bank_code_are_metadata():
    tx = single("blz_account_86.mt940")
    assert tx.counterparty_name == "Max Mustermann"
    assert tx.counterparty_iban is None
    assert tx.counterparty_bic is None
    assert tx.data["counterparty_account_id"] == "0532013000"
    assert tx.data["counterparty_bank_code"] == "37040044"
    assert tx.memo == "Mitgliedsbeitrag 2026 Max Mustermann"


def test_iban_keyword_in_purpose_is_used():
    tx = single("iban_in_purpose.mt940")
    assert tx.counterparty_iban == "DE89370400440532013000"
    assert tx.counterparty_bic is None
    assert tx.memo == "Mitglied NLL123"


def test_slash_separated_details_stay_in_the_memo():
    tx = single("dutch_style.mt940")
    assert tx.memo == (
        "/TRTP/SEPA OVERBOEKING/IBAN/NL02ABNA0123456789/BIC/ABNANL2A "
        "/NAME/ERIKA MUSTERFRAU/REMI/Lidmaatschap 2026/EREF/NOTPROVIDED"
    )
    assert tx.counterparty_name is None
    assert tx.counterparty_iban is None
    assert tx.external_id == "05123456789"
    assert tx.data["supplementary_details"] == "/TRCD/00100/"
    assert tx.data["statement_reference"] == "940A260115"
    assert tx.data["statement_number"] == "15"


def test_statement_level_86_is_not_attached_to_a_transaction():
    document = parse("statement_information_86.mt940")
    (statement,) = document.statements
    (tx,) = statement.transactions
    assert tx.memo == "Mitglied NLL123"
    assert statement.information == [
        "Kontoauszug Hinweis vorab",
        "Weitere Informationen zum Kontoauszug",
    ]


# -- references and external IDs --------------------------------------------


def test_bank_references_become_external_ids():
    transactions = parse("bank_reference.mt940").transactions
    assert [tx.external_id for tx in transactions] == ["2026011500001", "2026011500002"]
    assert [tx.bank_reference for tx in transactions] == [
        "2026011500001",
        "2026011500002",
    ]


def test_placeholders_are_never_used_as_references():
    tx = single("nonref.mt940")
    assert tx.external_id is None
    assert tx.bank_reference is None
    assert tx.end_to_end_id is None
    assert "account_owner_reference" not in tx.data
    assert tx.memo == "Spende"


def test_repeated_bank_reference_is_not_used_as_external_id():
    transactions = parse("duplicate_bank_reference.mt940").transactions
    assert len(transactions) == 2
    assert [tx.external_id for tx in transactions] == [None, None]
    assert [tx.bank_reference for tx in transactions] == ["BANKREF", "BANKREF"]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("NONREF", True),
        ("NOTPROVIDED", True),
        ("NOT PROVIDED", True),
        ("N/A", True),
        ("NONE", True),
        ("-", True),
        ("", True),
        (None, True),
        ("2026011500001", False),
        ("NLL123-2026", False),
        ("NONREF1", False),
    ],
)
def test_is_placeholder(value, expected):
    assert is_placeholder(value) is expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("DE89 3704 0044 0532 0130 00", ("DE89370400440532013000", None)),
        ("de89370400440532013000", ("DE89370400440532013000", None)),
        ("0532013000", (None, "0532013000")),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_classify_account(value, expected):
    assert classify_account(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("COBADEFFXXX", ("COBADEFFXXX", None)),
        ("COBADEFF", ("COBADEFF", None)),
        ("37040044", (None, "37040044")),
        (None, (None, None)),
    ],
)
def test_classify_bank(value, expected):
    assert classify_bank(value) == expected


# -- statements and accounts ---------------------------------------------------


def test_multiple_statements_keep_their_context():
    document = parse("multiple_statements.mt940")
    first, second = document.statements
    assert (first.statement_number, second.statement_number) == ("00001", "00002")
    assert (first.opening_balance, first.closing_balance) == (
        Decimal("1000.00"),
        Decimal("1025.30"),
    )
    assert (second.opening_balance, second.closing_balance) == (
        Decimal("1025.30"),
        Decimal("1055.30"),
    )
    assert first.account_id == second.account_id == "DE02120300000000202051"
    assert [tx.data["statement_number"] for tx in document.transactions] == [
        "00001",
        "00002",
    ]
    assert [tx.amount for tx in document.transactions] == [
        Decimal("25.30"),
        Decimal("30.00"),
    ]
    assert document.balance_mismatches == 0


def test_multiple_accounts_are_rejected():
    with pytest.raises(MultipleAccounts) as excinfo:
        parse("multiple_accounts.mt940")
    assert excinfo.value.count == 2


def test_statement_without_transactions():
    document = parse("no_transactions.mt940")
    assert document.transactions == []
    assert document.entry_count == 0
    (statement,) = document.statements
    assert statement.opening_balance == statement.closing_balance == Decimal("1025.30")


def test_balance_mismatch_is_reported_not_fixed(caplog):
    with caplog.at_level(logging.WARNING, logger="byro_finance_import_bank_files"):
        document = parse("balance_mismatch.mt940")
    (statement,) = document.statements
    assert statement.balance_mismatch is True
    assert document.balance_mismatches == 1
    assert len(document.transactions) == 1
    assert "MT940 statement 1: opening balance" in caplog.text
    for sensitive in SENSITIVE + ("1000", "1026"):
        assert sensitive not in caplog.text


def test_foreign_currency_is_passed_through_unchanged():
    tx = single("non_eur.mt940")
    assert tx.currency == "USD"
    assert tx.amount == Decimal("100.00")


# -- encodings -----------------------------------------------------------------


def test_utf8_umlauts():
    document = parse("utf8_umlauts.mt940")
    assert document.encoding == "utf-8"
    (tx,) = document.transactions
    assert tx.memo == "Spende für das Vereinsfest äöüß 20 €"
    assert tx.counterparty_name == "Jörg Müller-Lüdenscheidt"


def test_windows_1252_fallback():
    data = load("legacy_encoding.mt940")
    with pytest.raises(UnicodeDecodeError):
        data.decode("utf-8")
    document = parse_mt940(data)
    assert document.encoding == "cp1252"
    (tx,) = document.transactions
    assert tx.memo == "Spende für das Vereinsfest äöüß 20 €"
    assert tx.counterparty_name == "Jörg Müller-Lüdenscheidt"


def test_utf8_bom_is_tolerated():
    data = load("utf8_bom.mt940")
    assert data.startswith(b"\xef\xbb\xbf")
    document = parse_mt940(data)
    assert document.statements[0].reference == "STARTUMSE"
    assert len(document.transactions) == 1


def test_utf16_is_rejected_explicitly():
    with pytest.raises(UnsupportedEncoding):
        parse("utf16_bom.mt940")


def test_undecodable_bytes_are_rejected():
    with pytest.raises(UndecodableFile):
        parse("undecodable.mt940")


def test_decode_reports_the_encoding():
    assert decode(b":20:X") == (":20:X", "utf-8")
    assert decode("ä".encode("utf-8")) == ("ä", "utf-8")
    assert decode("ä".encode("cp1252")) == ("ä", "cp1252")


def test_file_object_input():
    with (FIXTURES / "basic_credit.mt940").open("rb") as f:
        assert len(parse_mt940(f).transactions) == 1


# -- invalid input -----------------------------------------------------------------


@pytest.mark.parametrize("name, error", sorted(INVALID_FIXTURES.items()))
def test_invalid_fixtures_raise(name, error):
    with pytest.raises(error):
        parse(name)


def test_unparseable_field_names_tag_and_statement():
    with pytest.raises(UnparseableField) as excinfo:
        parse("invalid_statement_line.mt940")
    assert excinfo.value.statement == 1
    assert excinfo.value.tag == "61"


@pytest.mark.parametrize(
    "name, reason",
    [
        ("invalid_date.mt940", "invalid date"),
        ("invalid_amount.mt940", "invalid amount"),
        ("missing_type_code.mt940", "statement lines could not be separated"),
    ],
)
def test_invalid_statement_reasons(name, reason):
    with pytest.raises(InvalidStatement) as excinfo:
        parse(name)
    assert excinfo.value.statement == 1
    assert excinfo.value.reason == reason


def test_zero_amount_is_an_error_not_a_skip():
    with pytest.raises(InvalidEntry) as excinfo:
        parse("zero_amount.mt940")
    assert excinfo.value.statement == 1
    assert excinfo.value.position == 1
    assert excinfo.value.reason == "zero amount"


def test_missing_account_and_currency():
    with pytest.raises(MissingAccountIdentification) as excinfo:
        parse("missing_account.mt940")
    assert excinfo.value.statement == 1
    with pytest.raises(MissingCurrency) as excinfo:
        parse("missing_currency.mt940")
    assert excinfo.value.statement == 1


def test_errors_are_attributed_to_the_failing_statement():
    data = load("multiple_statements.mt940").replace(
        b":61:2601160116C30,00", b":61:2601160116C30,0,0"
    )
    with pytest.raises(InvalidStatement) as excinfo:
        parse_mt940(data)
    assert excinfo.value.statement == 2
    assert excinfo.value.reason == "invalid amount"


def test_unexpected_exceptions_propagate_unchanged(monkeypatch):
    class Bug(Exception):
        pass

    def broken(*args, **kwargs):
        raise Bug("programming error")

    monkeypatch.setattr(parser.mt940, "parse_statements", broken)
    with pytest.raises(Bug):
        parse("basic_credit.mt940")


def test_runtime_error_without_tag_propagates(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("not a tag mismatch")

    monkeypatch.setattr(parser.mt940, "parse_statements", broken)
    with pytest.raises(RuntimeError) as excinfo:
        parse("basic_credit.mt940")
    assert not isinstance(excinfo.value, Mt940Error)


@pytest.mark.parametrize("name", sorted(INVALID_FIXTURES))
def test_error_messages_contain_no_bank_data(name):
    with pytest.raises(Mt940Error) as excinfo:
        parse(name)
    message = str(excinfo.value)
    for sensitive in SENSITIVE:
        assert sensitive not in message
    # Library exceptions quote the raw field value, so they are not chained.
    assert excinfo.value.__cause__ is None


def test_library_tag_loggers_are_silent(caplog):
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(UnparseableField):
            parse("invalid_statement_line.mt940")
        parse("basic_credit.mt940")
    assert "GARBAGE" not in caplog.text
    for sensitive in SENSITIVE:
        assert sensitive not in caplog.text


# -- invariants over all valid fixtures ---------------------------------------


@pytest.mark.parametrize("name", VALID_FIXTURES)
def test_valid_fixture_invariants(name):
    document = parse(name)
    assert document.encoding in ("utf-8", "cp1252")
    assert document.statements
    for statement in document.statements:
        assert statement.account_id
        assert statement.currency
        for tx in statement.transactions:
            assert isinstance(tx, Mt940Transaction)
            assert isinstance(tx.amount, Decimal)
            assert tx.amount.is_finite()
            assert tx.amount != 0
            assert tx.currency == statement.currency
            assert type(tx.booking_date) is datetime.date
            assert type(tx.value_date) is datetime.date
            assert isinstance(tx.memo, str)
            assert tx.counterparty_iban is None or " " not in tx.counterparty_iban
            assert tx.external_id is None or not is_placeholder(tx.external_id)
            assert tx.transaction_code is None or tx.transaction_code.isupper()
            json.dumps(tx.data)
            assert tx.data["debit_credit_mark"] in parser.MARK_SIGNS
    external_ids = [tx.external_id for tx in document.transactions if tx.external_id]
    assert len(external_ids) == len(set(external_ids))
