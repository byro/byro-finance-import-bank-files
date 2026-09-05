Bank file importers for byro
============================

This is a plugin for `byro`_ that provides **importers for file-based bank
statement formats** through byro's bank transaction importer API. Every
supported format appears as its own choice under *Finances → Import bank
transactions*.

The plugin reads a bank file safely, interprets its format specific structure
and hands neutral bank transactions to byro. Validation, duplicate detection,
bookkeeping entries and the matching of payments to members are done by the
byro core. The plugin never creates bookkeeping objects itself.

Formats
-------

Supported:

- **CAMT.053** (ISO 20022 ``BankToCustomerStatement``), see `CAMT.053
  importer`_ below.

Planned, but not implemented yet:

- MT940
- generic bank CSV

Explicitly **not** part of this plugin:

- direct bank communication of any kind (FinTS/HBCI, EBICS, PSD2/Open Banking)
- storing bank credentials
- automatic bank synchronisation
- payment initiation

Requirements
------------

- Python 3.12 or newer
- A `byro`_ installation (Django 5.2) that provides the bank transaction
  importer API (``byro.bookkeeping.bank_import``). The API was introduced with
  `byro pull request #514`_ and is available on byro's ``main`` branch since
  2026-09-04. As soon as a byro release contains it, that release is the
  minimum version; until then, install byro from ``main``.
- `defusedxml`_ (installed automatically)
- ``gettext`` (``msgfmt``) **only** for development and for building or
  installing the plugin from source: it compiles the translation catalogues at
  build time. Installing a prebuilt wheel does not need it.

Installation
------------

Install the plugin into the Python environment of your byro installation::

    $ pip install byro-finance-import-bank-files

or, for a checkout of this repository (requires ``gettext``)::

    $ pip install -e .

Restart byro afterwards. The plugin is registered through the ``byro.plugin``
entry point and shows up under *Settings → About* as **Bank file importers**.

Usage
-----

1. Download a bank statement file from your online banking. For CAMT.053 it is
   often offered as "CAMT" or "XML" export (German banks: "camt.053", "C53").
2. In byro open *Finances → Import bank transactions*.
3. Select the importer for your file format, for example **CAMT.053 bank
   statement**, choose the file and submit.

byro reports how many transactions were read, how many were newly imported and
how many were already known. Re-uploading the same or an overlapping statement
does not create duplicate bookings. Skipped entries (pending, information,
zero amount) are counted in byro's log.

CAMT.053 importer
-----------------

Supported
~~~~~~~~~

- ``camt.053.001.xx`` in any schema version. The parser looks for the elements
  byro needs and accepts every ``camt.053.001.xx`` namespace (also the Austrian
  ``ISO:camt.053.001.xx`` variant). Tested with versions **02**, **08** and
  **12**.
- Files with several statements (``Stmt``) for the same account.
- Booked entries (status ``BOOK``). Entries with status ``PDNG``, ``INFO`` or
  ``FUTR`` and entries with an amount of zero are skipped and counted in the
  log; an entry with an unknown or proprietary status makes the import fail.
- Credits (``CRDT``) become positive amounts, debits (``DBIT``) negative
  amounts. The amount element itself is never trusted for the sign.
- Counterparty name, IBAN (normalised, no whitespace) and BIC: the debtor for
  credits, the creditor for debits. For reversals (``RvslInd``) the parties
  keep the roles of the original transaction, so a returned direct debit
  shows the original debtor.
- Remittance information: all ``Ustrd`` lines joined with single spaces;
  otherwise the structured creditor reference and additional remittance lines;
  otherwise return information, ``AddtlTxInf`` or ``AddtlNtryInf``.
- SEPA references: end-to-end ID, mandate ID, creditor identifier, bank
  reference (``AcctSvcrRef``) and the bank transaction code (ISO domain code
  such as ``PMNT/RCDT/ESCT`` or, if only that is given, the proprietary code
  such as ``NTRF+166``).
- Batch entries: one ``Ntry`` with several ``TxDtls`` is split into one
  transaction per detail **only if** every detail has an amount in the entry's
  currency and the signed detail amounts add up exactly to the entry amount.
  Otherwise the entry is imported as a single transaction with the reason
  stored in its metadata (``batch_details_not_split``). Nothing is invented or
  estimated.
- Stable external IDs for byro's duplicate detection: the entry's
  ``AcctSvcrRef`` (for split batches: the detail's own ``AcctSvcrRef``,
  ``TxId`` or ``UETR``). Placeholders such as ``NOTPROVIDED``, ``NONREF`` or
  ``N/A`` are never used, and a reference that appears on several transactions
  of one file is not used either. byro then falls back to its fingerprint.
- Dates: ``BookgDt`` and ``ValDt`` as ``Dt`` or ``DtTm`` (the written date is
  used, no timezone conversion). If an entry has no booking date, the value
  date is used and marked in the metadata; an entry with neither fails.
- Encodings declared in the XML header (for example ``ISO-8859-1``) and a UTF-8
  byte order mark.

Not supported
~~~~~~~~~~~~~

- CAMT.052 (account reports) and CAMT.054 (debit/credit notifications)
- pain.* and pacs.* messages
- ZIP archives (extract them and upload the XML files)
- Currencies other than the accounting currency: byro's bookkeeping is EUR
  only, so a statement in another currency fails with a clear error. Amounts
  are never converted or silently treated as EUR.
- Files containing statements for more than one bank account (byro has a
  single bank account, so such files are rejected instead of guessing)

Metadata
~~~~~~~~

Besides the fields byro stores for every imported transaction (counterparty,
external ID, SEPA references, transaction code) the importer adds CAMT specific
metadata to ``Booking.data`` where available: ``camt_version``,
``statement_id``, ``entry_reference``, ``account_servicer_reference``,
``transaction_id``, ``instruction_id``, ``uetr``, ``creditor_reference``,
``purpose_code``, ``proprietary_bank_transaction_code`` (and ``_issuer``),
``additional_entry_info``, ``reversal``, ``return_reason``,
``return_reason_info``, ``booking_date_source``, ``batch_transaction_count``,
``batch_message_id``, ``batch_payment_information_id``,
``batch_details_not_split``, ``ultimate_counterparty_name`` and
``counterparty_account_id``. The complete XML file is kept by byro as the
import source and is not copied into the bookings.

Security and privacy
--------------------

- Bank files are untrusted input. XML based formats (CAMT) are parsed with
  `defusedxml`_; DTDs, entity declarations and external references are
  rejected, and nothing is fetched from the network while parsing.
- Error messages shown in byro never contain file content. The plugin logs
  only technical facts (format version, entry and transaction counts, error
  class), never IBANs, names or remittance text.
- **Never attach real bank statements to bug reports.** If you need to share a
  file, anonymise it completely first (names, IBANs, references, remittance
  text, amounts). The test fixtures in ``tests/fixtures`` are synthetic and
  show which information a bank file contains.

Development setup
-----------------

1. Make sure that you have a working `byro development setup`_ with the bank
   transaction importer API and that ``gettext`` is installed.

2. Clone this repository, e.g. into byro's ``src/local/`` directory.

3. Activate the virtual environment you use for byro development and install
   the plugin in editable mode::

       $ pip install -e ".[dev]"

   Alternatively, run ``./install_local_plugins.sh`` from byro's ``src/``
   directory to install every plugin found in ``src/local/``.

4. Restart your local byro server.

Tests and code style
--------------------

Run the test suite against byro's test settings with an SQLite database::

    $ BYRO_DB_ENGINE=sqlite3 pytest

``tests/test_plugin.py`` checks the plugin registration. The CAMT.053 tests
live in ``tests/importers/camt``: ``test_parser.py`` exercises the parser with
the fixtures in ``tests/fixtures/camt`` without Django, ``test_importer.py``
covers the mapping to byro's ``ImportedBankTransaction`` and the user facing
error messages, and ``test_integration.py`` uploads fixtures through byro's
import page and checks the resulting bookings.

Format and lint the code the same way the byro core does::

    $ isort .
    $ black .
    $ flake8 .

Translations
------------

Create or update the German message catalogue from within the plugin package::

    $ cd byro_finance_import_bank_files
    $ django-admin makemessages -l de

Compile the catalogues from the repository root::

    $ django-admin compilemessages

The compiled ``.mo`` files are not checked in. They are compiled with
``msgfmt`` whenever the package is built or installed from source (see
``setup.py``), so every wheel ships them.

Plugin structure
----------------

The repository is exactly one byro plugin: one Python package, one Django app
and one ``byro.plugin`` entry point. Each supported file format is one
``BankTransactionImporter`` below ``importers/`` and is registered through the
``IMPORTERS`` list in ``signals.py``.

- ``byro_finance_import_bank_files/apps.py`` holds the ``AppConfig`` with the
  ``ByroPluginMeta`` metadata that byro reads. Django only discovers it from
  ``apps.py``.
- ``byro_finance_import_bank_files/signals.py`` registers all importers of the
  plugin via ``byro.bookkeeping.signals.bank_transaction_importers``.
- ``byro_finance_import_bank_files/importers/camt/parser.py`` is the CAMT.053
  parser. It has no Django or byro dependency and turns a file into
  ``CamtTransaction`` objects, raising ``CamtError`` subclasses for files it
  cannot interpret.
- ``byro_finance_import_bank_files/importers/camt/importer.py`` is the byro
  adapter: it implements ``BankTransactionImporter``, maps parser output to
  ``ImportedBankTransaction`` and translates parser errors into user messages.
- ``feature/camt053-importer.md`` is the functional specification of the
  CAMT.053 importer.

See the `byro plugin documentation`_ and its chapter on
`bank transaction importers`_ for the API this plugin implements.

License
-------

byro-finance-import-bank-files is licensed under the GNU Affero General
Public License version 3 only (AGPL-3.0-only). See ``LICENSE`` for details.


.. _byro: https://github.com/byro/byro
.. _byro pull request #514: https://github.com/byro/byro/pull/514
.. _defusedxml: https://pypi.org/project/defusedxml/
.. _byro development setup: https://byro.readthedocs.io/en/latest/developer/setup.html
.. _byro plugin documentation: https://byro.readthedocs.io/en/latest/developer/plugins/
.. _bank transaction importers: https://byro.readthedocs.io/en/latest/developer/plugins/bank-transaction-importers.html
