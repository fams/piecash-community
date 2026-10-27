# -*- coding: utf-8 -*-
"""A book created by piecash has the table versions and the column order of a book written by GnuCash.

When GnuCash opens a book whose table version is older than its own, it upgrades the table with
``INSERT INTO <table>_new SELECT * FROM <table>`` (copy by position) and drops the old table even
if the copy failed (``GncSqlBackend::upgrade_table``). A book created with splits version 4 and
the guid as last column lost all its splits that way when opened in GnuCash 4/5.
"""
import datetime
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

import piecash
from piecash.core import Version
from piecash.core.session import version_supported
from test_invoices import (  # noqa: F401  (fixtures)
    book,
    book_with_billterms,
    book_with_updated_job_properties,
    book_with_invoices,
    book_with_invoice_entries,
)

# written by GnuCash 4.0.8 (tables at their current version, splits 5)
REFERENCE = Path(__file__).parent / "books" / "historical_prices.gnucash"


def _reference_tables():
    con = sqlite3.connect(str(REFERENCE))
    try:
        return {
            name: (sql, [r[1] for r in con.execute("PRAGMA table_info({})".format(name))])
            for name, sql in con.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        con.close()


def _columns(session, table):
    return [r[1] for r in session.execute(text("PRAGMA table_info({})".format(table)))]


def test_reference_is_a_gnucash_4_book():
    con = sqlite3.connect(str(REFERENCE))
    versions = dict(con.execute("SELECT table_name, table_version FROM versions"))
    con.close()
    assert versions.pop("Gnucash") >= 4000000
    versions.pop("Gnucash-Resave")
    assert versions == {k: v for k, v in version_supported["4.1"].items() if "Gnucash" not in k}


def test_new_book_has_the_table_versions_of_gnucash_4_1():
    with piecash.create_book(currency="EUR") as b:
        versions = {v.table_name: v.table_version for v in b.session.query(Version)}
    assert versions == version_supported["4.1"]
    assert versions["splits"] == 5 and versions["Gnucash"] == 4000001


def test_new_book_has_the_column_order_of_gnucash(tmp_path):
    path = tmp_path / "new.gnucash"
    piecash.create_book(str(path), currency="EUR").close()
    con = sqlite3.connect(str(path))
    try:
        for name, (_, columns) in _reference_tables().items():
            created = [r[1] for r in con.execute("PRAGMA table_info({})".format(name))]
            if name == "gnclock":
                # GnuCash names them Hostname / PID; SQL column names ignore case
                created, columns = [c.lower() for c in created], [c.lower() for c in columns]
            assert created == columns, name
    finally:
        con.close()


def test_gnucash_table_upgrade_keeps_every_row(book_with_invoice_entries):
    book = book_with_invoice_entries
    nok = book.commodities(mnemonic="NOK")
    ar = piecash.Account(name="A/R", type="RECEIVABLE", parent=book.root_account, commodity=nok)
    ap = piecash.Account(name="A/P", type="PAYABLE", parent=book.root_account, commodity=nok)
    book.add(
        piecash.Price(
            commodity=nok,
            currency=book.commodities(mnemonic="EUR"),
            date=datetime.date.today(),
            value=Decimal("0.1"),
        )
    )
    book.flush()
    book.invoices[0].post(ar, prices=book.prices)
    book.bills[0].post(ap, prices=book.prices)
    book.flush()

    session = book.session
    filled = set()
    for name, (sql, columns) in _reference_tables().items():
        if name in ("gnclock", "versions"):
            continue
        assert _columns(session, name) == columns, name
        new = name + "_new"
        session.execute(text(sql.replace("CREATE TABLE {}(".format(name), "CREATE TABLE {}(".format(new), 1)))
        # what GncSqlBackend::upgrade_table does
        session.execute(text("INSERT INTO {} SELECT * FROM {}".format(new, name)))
        select = "SELECT {} FROM {{}} ORDER BY rowid".format(", ".join(columns))
        rows = session.execute(text(select.format(name))).fetchall()
        assert session.execute(text(select.format(new))).fetchall() == rows, name
        if rows:
            filled.add(name)

    # the copy proves something only for tables that have rows
    assert filled >= {
        "accounts", "billterms", "books", "commodities", "customers", "employees", "entries",
        "invoices", "jobs", "lots", "prices", "slots", "splits", "taxtables", "transactions",
        "vendors",
    }
