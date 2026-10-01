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
