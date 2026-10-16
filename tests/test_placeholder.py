# -*- coding: utf-8 -*-
"""Marking / unmarking an existing account as placeholder, in a book written by GnuCash.

GnuCash keeps the flag twice: the ``accounts.placeholder`` column and the ``placeholder`` KVP slot.
``xaccAccountSetPlaceholder`` (libgnucash/engine/Account.cpp) writes the slot as the string
``"true"`` and removes it when unmarking (``set_kvp_boolean_path`` passes ``nullptr``); the engine
does not look at the splits already in the account.

A string slot is read from ``slot_type`` and ``string_val`` only. piecash fills the unused columns
differently from GnuCash (``double_val`` 0.0 instead of NULL, ``timespec_val`` NULL instead of
1970-01-01), as for every slot it writes, so only the read columns are compared.
"""
import shutil
import sqlite3
from pathlib import Path

import pytest

import piecash
from piecash import GncValidationError

BOOK = Path(__file__).parent / "books" / "complex_sample.gnucash"

# complex_sample's root account has no currency (warning of #251), unrelated to this test
pytestmark = pytest.mark.filterwarnings("ignore:Root account has no commodity")


@pytest.fixture
def book_file(tmp_path):
    path = tmp_path / "book.gnucash"
    shutil.copyfile(BOOK, path)
    return path


def _stored(path, name):
    """(accounts.placeholder, [placeholder slot rows]) read with plain SQL."""
    con = sqlite3.connect(str(path))
    try:
        guid, placeholder = con.execute(
            "SELECT guid, placeholder FROM accounts WHERE name = ?", (name,)
        ).fetchone()
        slots = con.execute(
            "SELECT slot_type, string_val FROM slots WHERE obj_guid = ? AND name = 'placeholder'",
            (guid,),
        ).fetchall()
    finally:
        con.close()
    return placeholder, slots


def _set_placeholder(path, name, value):
    with piecash.open_book(str(path), readonly=False, do_backup=False) as book:
        book.accounts(name=name).placeholder = value
        book.save()
    with piecash.open_book(str(path)) as book:
        return book.accounts(name=name).placeholder


def test_mark_existing_account_with_splits(book_file):
    # "Asset" was marked by GnuCash itself: its slot row is the reference
    _, gnucash_slots = _stored(book_file, "Asset")
    assert gnucash_slots == [(4, "true")]
    assert _stored(book_file, "Checking") == (0, [])

    # the GnuCash engine accepts marking an account that already has splits
    assert _set_placeholder(book_file, "Checking", 1) == 1
    assert _stored(book_file, "Checking") == (1, gnucash_slots)

    assert _set_placeholder(book_file, "Checking", 0) == 0
    assert _stored(book_file, "Checking") == (0, [])


def test_unmark_account_marked_by_gnucash(book_file):
    assert _set_placeholder(book_file, "Asset", 0) == 0
    assert _stored(book_file, "Asset") == (0, [])

    assert _set_placeholder(book_file, "Asset", 1) == 1
    assert _stored(book_file, "Asset") == (1, [(4, "true")])


def test_editing_an_old_transaction_of_a_placeholder_account_is_refused(book_file):
    # stricter than GnuCash, which only refuses to move a split *to* a placeholder account
    # and makes the placeholder account's own register read-only
    _set_placeholder(book_file, "Checking", 1)
    with piecash.open_book(str(book_file), readonly=False, do_backup=False) as book:
        split = book.accounts(name="Checking").splits[0]
        split.transaction.description = "edited"
        with pytest.raises(GncValidationError, match="placeholder"):
            book.save()
