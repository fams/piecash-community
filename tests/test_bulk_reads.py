"""Book.account_balances and Book.slot_values: same answers as the per-object ORM calls."""
from datetime import date, datetime
from decimal import Decimal

import pytest
from piecash import Account, Split, Transaction, Commodity
from test_helper import (
    db_sqlite_uri,
    db_sqlite,
    new_book,
    new_book_USD,
    book_uri,
    book_transactions,
    book_complex,
    book_basic,
)

# dummy line to avoid removing unused symbols
a = (db_sqlite_uri, db_sqlite, new_book, new_book_USD, book_uri, book_transactions, book_complex, book_basic)


@pytest.mark.parametrize("natural_sign", [True, False])
def test_account_balances_equals_get_balance(book_complex, natural_sign):
    balances = book_complex.account_balances(natural_sign=natural_sign)
    assert set(balances) == set(book_complex.accounts)
    for account, balance in balances.items():
        assert balance == account.get_balance(recurse=False, natural_sign=natural_sign), account.fullname


def test_account_balances_at_date(book_complex):
    datas = sorted({sp.transaction.post_date for sp in book_complex.splits})
    for corte in (datas[0], datas[len(datas) // 2], datas[-1], date(1999, 1, 1), date(2100, 1, 1)):
        balances = book_complex.account_balances(at_date=corte)
        for account, balance in balances.items():
            assert balance == account.get_balance(recurse=False, at_date=corte), (account.fullname, corte)
    # um datetime vale como o dia dele
    corte = datas[len(datas) // 2]
    assert book_complex.account_balances(at_date=datetime.combine(corte, datetime.min.time())) == \
        book_complex.account_balances(at_date=corte)


def test_account_balances_account_without_splits_is_zero(book_basic):
    balances = book_basic.account_balances()
    assert balances and all(v == Decimal(0) for v in balances.values())


def test_account_balances_mixed_denominators(book_basic):
    eur = book_basic.currencies[0]
    asset = book_basic.accounts.get(name="asset")
    exp = book_basic.accounts.get(name="exp")
    for valor in (Decimal("10.5"), Decimal("0.333"), Decimal("7")):
        Transaction(currency=eur, description="t", post_date=date(2020, 1, 2),
                    splits=[Split(account=asset, value=-valor), Split(account=exp, value=valor)])
    book_basic.flush()
    balances = book_basic.account_balances()
    assert balances[exp] == Decimal("17.833") == exp.get_balance(recurse=False)
    assert balances[asset] == Decimal("-17.833")


def _tag_splits(book):
    eur = book.currencies[0]
    asset = book.accounts.get(name="asset")
    exp = book.accounts.get(name="exp")
    txs = []
    for i in range(3):
        txs.append(Transaction(currency=eur, description=f"t{i}", post_date=date(2020, 1, 1 + i),
                               splits=[Split(account=asset, value=-i - 1), Split(account=exp, value=i + 1)]))
    book.flush()
    return txs


def test_slot_values_nested_path_equals_orm(book_basic):
    txs = _tag_splits(book_basic)
    txs[0].splits[0]["user-keys/tags"] = "#a,#b"
    txs[1].splits[1]["user-keys/tags"] = "#c"
    txs[1].splits[1]["user-keys/outra"] = "x"
    book_basic.flush()
    valores = book_basic.slot_values("user-keys/tags", of=Split)
    esperado = {}
    for sp in book_basic.splits:                    # o jeito do ORM: uma leitura por split e por nível
        try:
            esperado[sp.guid] = sp["user-keys/tags"].value
        except KeyError:
            pass
    assert valores == esperado == {txs[0].splits[0].guid: "#a,#b", txs[1].splits[1].guid: "#c"}


def test_slot_values_restricts_to_object_class(book_basic):
    txs = _tag_splits(book_basic)
    txs[0].splits[0]["user-keys/tags"] = "#a"
    asset = book_basic.accounts.get(name="asset")
    asset["user-keys/tags"] = "#conta"
    book_basic.flush()
    assert set(book_basic.slot_values("user-keys/tags", of=Split)) == {txs[0].splits[0].guid}
    assert book_basic.slot_values("user-keys/tags", of=Account) == {asset.guid: "#conta"}
    assert set(book_basic.slot_values("user-keys/tags")) == {txs[0].splits[0].guid, asset.guid}


def test_slot_values_types_and_missing(book_basic):
    asset = book_basic.accounts.get(name="asset")
    asset["n"] = 42
    asset["texto"] = "oi"
    asset["dec"] = Decimal("1.25")
    asset["quando"] = date(2021, 3, 4)
    book_basic.flush()
    assert book_basic.slot_values("n") == {asset.guid: 42}
    assert book_basic.slot_values("texto") == {asset.guid: "oi"}
    assert book_basic.slot_values("dec") == {asset.guid: Decimal("1.25")}
    assert book_basic.slot_values("quando") == {asset.guid: date(2021, 3, 4)}
    assert book_basic.slot_values("nao/existe") == {}
    with pytest.raises(ValueError):
        book_basic.slot_values("")


def test_bulk_reads_do_not_warn(book_complex):
    """The queries use the slot/date column types: they must all be cacheable (cache_ok),
    otherwise SQLAlchemy 2.0 warns and loses its statement cache."""
    import warnings
    from sqlalchemy.exc import SAWarning

    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        book_complex.account_balances()
        book_complex.account_balances(at_date=date(2020, 1, 1))
        book_complex.slot_values("user-keys/tags", of=Split)


def test_account_rows_equals_orm(book_complex):
    rows = {r.guid: r for r in book_complex.account_rows()}
    contas = book_complex.session.query(Account).all()
    assert set(rows) == {a.guid for a in contas}
    for a in contas:
        r = rows[a.guid]
        assert (r.name, r.parent_guid, r.type, r.commodity_guid) == (a.name, a.parent_guid, a.type, a.commodity_guid)
        assert r.mnemonic == (a.commodity.mnemonic if a.commodity else None)
    assert book_complex.root_account.guid in rows


def test_transaction_rows_equals_orm(book_complex):
    rows = {r.guid: r for r in book_complex.transaction_rows()}
    assert set(rows) == {t.guid for t in book_complex.transactions}
    for t in book_complex.transactions:
        r = rows[t.guid]
        assert (r.post_date, r.description, r.currency_guid, r.num) == (t.post_date, t.description, t.currency_guid, t.num)


def test_split_rows_equals_orm(book_complex):
    rows = {r.guid: r for r in book_complex.split_rows()}
    assert set(rows) == {s.guid for s in book_complex.splits}
    for s in book_complex.splits:
        r = rows[s.guid]
        assert (r.transaction_guid, r.account_guid, r.memo, r.reconcile_state) == (
            s.transaction_guid, s.account_guid, s.memo, s.reconcile_state)
        assert (r.post_date, r.description) == (s.transaction.post_date, s.transaction.description)
        assert Decimal(r.value_num) / Decimal(r.value_denom) == s.value
        assert Decimal(r.quantity_num) / Decimal(r.quantity_denom) == s.quantity
        assert all(isinstance(n, int) for n in (r.value_num, r.value_denom, r.quantity_num, r.quantity_denom))


def test_split_rows_filters(book_complex):
    todas = book_complex.split_rows()
    conta = next(a for a in book_complex.accounts if a.splits)
    esperado = {s.guid for s in conta.splits}
    assert {r.guid for r in book_complex.split_rows(account=conta)} == esperado
    assert {r.guid for r in book_complex.split_rows(account=conta.guid)} == esperado
    datas = sorted({r.post_date for r in todas})
    de, ate = datas[len(datas) // 3], datas[2 * len(datas) // 3]
    assert {r.guid for r in book_complex.split_rows(from_date=de, to_date=ate)} == {
        r.guid for r in todas if de <= r.post_date <= ate}
    assert {r.guid for r in book_complex.transaction_rows(from_date=de, to_date=ate)} == {
        r.guid for r in book_complex.transaction_rows() if de <= r.post_date <= ate}
    assert book_complex.split_rows(from_date=date(2100, 1, 1)) == []


def test_row_readers_keep_the_stored_day(tmp_path):
    """post_date is the day as stored (UTC), not converted to the local timezone."""
    import shutil, sqlite3, os, piecash
    origem = os.path.join(os.path.dirname(__file__), "..", "gnucash_books", "complex_sample.gnucash")
    livro = str(tmp_path / "c.gnucash")
    shutil.copyfile(origem, livro)
    con = sqlite3.connect(livro)
    guid = con.execute("SELECT guid FROM transactions LIMIT 1").fetchone()[0]
    con.execute("UPDATE transactions SET post_date='2020-03-10 00:30:00' WHERE guid=?", (guid,))
    con.execute("DELETE FROM gnclock")
    con.commit(); con.close()
    with piecash.open_book(livro, readonly=True) as book:
        assert {r.guid: r.post_date for r in book.transaction_rows()}[guid] == date(2020, 3, 10)
        assert {r.post_date for r in book.split_rows() if r.transaction_guid == guid} == {date(2020, 3, 10)}
        assert [r.guid for r in book.transaction_rows(from_date=date(2020, 3, 10), to_date=date(2020, 3, 10))] == [guid]


def test_strict_readonly_opens_the_sqlite_file_readonly(tmp_path):
    """strict_readonly=True cannot write the file at all (nor roll a hot journal back)."""
    import shutil, os, piecash
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    origem = os.path.join(os.path.dirname(__file__), "..", "gnucash_books", "complex_sample.gnucash")
    livro = str(tmp_path / "com espaço & acento é.gnucash")
    shutil.copyfile(origem, livro)
    antes = open(livro, "rb").read()
    with piecash.open_book(livro, readonly=True, strict_readonly=True, open_if_lock=True) as book:
        assert len(book.accounts) > 0
        with pytest.raises(OperationalError):
            book.session.execute(text("UPDATE accounts SET name = name || 'x'"))
    assert open(livro, "rb").read() == antes
    with pytest.raises(ValueError):
        piecash.open_book(livro, readonly=False, strict_readonly=True, open_if_lock=True, do_backup=False)
    # read-write still works
    with piecash.open_book(livro, readonly=False, open_if_lock=True, do_backup=False) as book:
        book.session.execute(text("UPDATE accounts SET name = name"))
        book.save()
