# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

piecash is a pure-Python SQLAlchemy ORM over GnuCash books stored in SQL (sqlite3, Postgres, MySQL). This repo is a community-maintained fork of `sdementen/piecash`. The same code runs on SQLAlchemy 1.4 and 2.0. Only books from GnuCash 3.0 / 3.7 / 4.1 are supported. The table-version matrix is `version_supported` in `piecash/core/session.py`.

## Commands

No virtualenv is committed and there is no global `python`/`pytest`, so create one first (e.g. with `uv`):

```bash
uv venv && uv pip install -e ".[test,ledger]"   # same extras as CI
pytest tests/ -v                                  # full suite
pytest tests/test_transaction.py::TestTransaction_create_transaction -v   # one class/test
pytest tests/ -k "balance" -v
coverage run --source=piecash -m pytest tests/
flake8 --ignore=E126,E121 --max-line-length=99    # lint config from setup.py (CI lint step is disabled)
cd docs && make html                              # docs (needs .[doc])
```

CI (`.github/workflows/github-actions.yml`) runs Python 3.8–3.12 under `LC_ALL=fr_FR.UTF-8`, with Postgres and MySQL services. The locale matters: ledger/currency formatting tests have been locale-sensitive in the past.

`setup.py` accepts `SQLAlchemy>=1.4, <2.1` (since 1.2.2): the same code runs on SA 1.4 and 2.0. Run the suite on both before releasing.

## Test infrastructure

- `tests/test_helper.py` holds all fixtures and is imported explicitly by each test module (`from test_helper import ...`). There is no `conftest.py`.
- Fixtures such as `new_book`, `book_uri` and `book_db_config` are **parametrized over backends**. Locally the backends are in-memory sqlite and file sqlite. Postgres/MySQL are added only when env vars are set: `CI`/`GITHUB_ACTIONS` (fixed CI credentials), `TRAVIS`, `APPVEYOR`, or `PIECASH_DBSERVER_TEST` (+ `PIECASH_DBSERVER_TEST_USERNAME`, `PG_PASSWORD`, `PIECASH_DBSERVER_TEST_PORT`). Setting `CI` locally without those servers running will make tests fail.
- Fixture books live in `tests/books/*.gnucash`. Tests work on copies of them. `gnucash_books/` holds the doc/example books, not the test fixtures.
- `test_ledger.py` compares output to `tests/references/*.ledger`, but it **rewrites the reference file before asserting**, so it always passes and silently updates the references. Check `git diff tests/references` after running it.
- `test_integration.py` needs Postgres/MySQL for the parts that are not sqlite.
- `tests/test_mappers.py` runs `configure_mappers()` with `SAWarning` turned into an error. GnuCash's polymorphic associations (`slots.obj_guid`, `recurrences.obj_guid`, `jobs.owner_guid`, `invoices.owner_guid`/`billto_guid`) make several classes write the same column on purpose, so a new relationship on one of those columns must declare `overlaps=`.
- `pytest.ini` filters `SAWarning` and SQLAlchemy `DeprecationWarning`. Don't add SA-version-specific warning classes there, because they break pytest config on other SA versions.

## Architecture

**Declarative base & read-only models** (`piecash/sa_extra.py`, `piecash/_declbase.py`): `DeclarativeBase` uses a constructor that raises `NotImplementedError`. Only models that define their own `__init__` can be created; everything else is read-only. `DeclarativeBaseGuid` adds the `guid` PK and a polymorphic `slots` relationship. It also mixes in `DictWrapper`, so `obj["kvp/path"]` reads and writes GnuCash KVP slots (`piecash/kvp.py`). `sa_extra.py` also holds GnuCash-specific column types (`_DateTime`, `_Date`, `_DateAsDateTime`, which handle GnuCash's string/UTC date storage per dialect) and helpers that expose slot values as attributes (`mapped_to_slot_property`, `pure_slot_property`, `kvp_attribute`).

**Numeric values**: GnuCash stores amounts as `*_num`/`*_denom` integer pairs. `hybrid_property_gncnumeric` (`piecash/_common.py`) exposes them as `Decimal`.

**Session lifecycle** (`piecash/core/session.py`):
- `create_book` / `open_book` build a URI (`build_uri`), create an engine and check the `versions` table against `version_supported`. They honor the `gnclock` table (lock on open, refuse if locked unless `open_if_lock`) and back up sqlite files when opening read-write.
- `_make_session` creates a `Session(future=True)` on SA 1.4 and falls back to a plain `Session` on SA 2.x.
- `adapt_session` monkeypatches the session. With `readonly=True`, `commit` raises, but the sqlite file is still opened read-write and changes can be flushed. `open_book(strict_readonly=True)` makes SQLite itself open the file with `mode=ro` (`_readonly_sqlite_uri`), so nothing can be flushed and reading does not roll back a hot journal. It also adds lock helpers and `is_saved`/`_all_changes` change tracking, and cross-links `book.session` ↔ `session.book`.
- Global listeners on `Session`: `before_flush → Book.track_dirty`, which records old values per object into `session._all_changes`, and `before_commit → Book.validate_book`.

**Validation model** (`piecash/core/book.py`): on commit, each new/dirty/deleted object yields, through `object_to_validate(change)`, the objects that must be revalidated. For example, a Split yields its Transaction and Lot. The book then calls `.validate()` on each of those. Most GnuCash invariants (balanced transactions, currency rules, immutable fields) are enforced in `validate()`, not in setters, and raise `GncValidationError`/`GncImbalanceError`. `on_book_add()` is the hook for initializing defaults when an object joins a book.

**Bulk readers** (`piecash/core/bulk.py`): `account_balances`, `slot_values`, `account_rows`, `transaction_rows` and `split_rows` answer whole-book questions in one or two SQL queries and return plain values, instead of several ORM queries per object. They are exposed as thin `Book` methods that import `bulk` lazily. Row readers return amounts as the stored `num`/`denom` integers and `post_date` as the stored day. A downstream application compares these values byte for byte, so that format is a compatibility contract: do not change it without a release note. `tests/test_bulk_reads.py` checks each of them against the ORM result.

**SA 2.0 / `future=True` gotcha**: many-to-one assignment no longer cascades a new child into the session. Newly created children such as `Lot` or `TaxtableEntry` must be `book.add()`-ed explicitly (see commit 77a45cc). Use `select()`/`case()` in 2.0 call style.

**Packages**: `piecash/core` (Book, Account, Commodity/Price, Transaction/Split/Lot/ScheduledTransaction, `factories` helpers, and `currency_ISO` data); `piecash/business` (Customer/Vendor/Employee, Invoice/Bill/Job, Taxtable); `piecash/budget.py`; `piecash/ledger.py` (ledger-cli export, dispatched by type); `piecash/scripts` (the `piecash` click CLI entry point `piecash.scripts.export:cli`, plus QIF/price export). `Book` also offers pandas helpers (`splits_df`, `prices_df`). New stateless `TypeDecorator`s need `cache_ok = True`, or SA 2.0 stops caching statements that use them and warns.

## Release

Bump `piecash/metadata.py`, update `CHANGELOG.rst`, then tag `vX.Y.Z`. Work goes through `feature/*` branches fast-forwarded into `master`. See `DEVELOPER.rst`.

Downstream consumers pin this fork by commit SHA, not by tag, so a new release reaches them only when they move the SHA: announce every release. At least one of them runs its test suite with `SAWarning` as an error, so the library must import and query without SQLAlchemy warnings.

## Repository rules

- This fork is public: never commit personal data (real books, account names, amounts, credentials). Use the synthetic books in `tests/books` and `gnucash_books`.
- Check the upstream from time to time for changes worth merging. The active line is `gorf/piecash`; the original `sdementen/piecash` has limited maintenance. Last checked: 2026-10-08.
- Several sessions share this checkout. Keep the main checkout on a clean `master` and develop in a separate `git worktree`.
