# -*- coding: utf-8 -*-
"""Split value and quantity are stored at the smallest unit of the currency / commodity, as GnuCash does.

GnuCash's engine converts on every set (``xaccSplitSetValue`` to the transaction currency fraction,
``xaccSplitSetAmount`` to the account SCU, both rounding half up).
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from piecash import Account, Commodity, GncImbalanceError, Split, Transaction
from test_helper import db_sqlite_uri, db_sqlite, book_basic  # noqa: F401  (fixtures)


def _stored(book, description):
    """[(account name, value_num, value_denom, quantity_num, quantity_denom)] as stored, read with plain SQL."""
    book.flush()
    return sorted(
        tuple(r)
        for r in book.session.execute(
            text(
                "SELECT a.name, s.value_num, s.value_denom, s.quantity_num, s.quantity_denom "
                "FROM splits s JOIN transactions t ON t.guid = s.tx_guid "
                "JOIN accounts a ON a.guid = s.account_guid WHERE t.description = :d"
            ),
            {"d": description},
        )
    )


def _transaction(book, description, value, quantity=None, to="exp"):
    eur = book.default_currency
    return Transaction(
        currency=eur,
        description=description,
        post_date=date(2026, 1, 15),
        splits=[
            Split(account=book.accounts(name="asset"), value=-value),
            Split(account=book.accounts(name=to), value=value, quantity=quantity),
        ],
    )


class TestSplitScale(object):
    @pytest.mark.parametrize(
        "value, num",
        [
            (Decimal("119.9"), 11990),  # was stored as 1199/10
            (Decimal("120"), 12000),  # was stored as 120/1
            (Decimal("0.5"), 50),
            (Decimal("12.34"), 1234),
            (Decimal("100.00"), 10000),
        ],
    )
    def test_value_and_quantity_are_stored_at_the_currency_fraction(self, book_basic, value, num):
        _transaction(book_basic, "scale", value)
        book_basic.save()
        assert _stored(book_basic, "scale") == [("asset", -num, 100, -num, 100), ("exp", num, 100, num, 100)]
        split = book_basic.transactions(description="scale").splits(account=book_basic.accounts(name="exp"))
        assert split.value == value and split.quantity == value  # the number itself does not change

    def test_more_decimals_than_the_currency_has_are_rounded_half_up(self, book_basic):
        _transaction(book_basic, "round", Decimal("0.125"))
        book_basic.save()
        assert _stored(book_basic, "round") == [("asset", -13, 100, -13, 100), ("exp", 13, 100, 13, 100)]

    def test_rounding_that_unbalances_the_transaction_is_refused(self, book_basic):
        eur = book_basic.default_currency
        asset, exp, inc = (book_basic.accounts(name=n) for n in ("asset", "exp", "inc"))
        Transaction(
            currency=eur,
            description="thirds",
            post_date=date(2026, 1, 15),
            splits=[  # balanced as given (0.01), but 0.005 rounds to 0.01 twice
                Split(account=asset, value=Decimal("-0.01")),
                Split(account=exp, value=Decimal("0.005")),
                Split(account=inc, value=Decimal("0.005")),
            ],
        )
        with pytest.raises(GncImbalanceError):
            book_basic.save()

    def test_quantity_is_stored_at_the_commodity_fraction(self, book_basic):
        points = Commodity(namespace="MILES", mnemonic="PTS", fullname="Points", fraction=1, book=book_basic)
        Account(name="points", type="ASSET", commodity=points, parent=book_basic.root_account)
        Account(name="points equity", type="EQUITY", commodity=points, parent=book_basic.root_account)
        book_basic.flush()
        eur = book_basic.default_currency
        Transaction(
            currency=eur,
            description="points",
            post_date=date(2026, 1, 15),
            splits=[
                Split(account=book_basic.accounts(name="points"), value=Decimal("0"), quantity=Decimal("500.0")),
                Split(account=book_basic.accounts(name="points equity"), value=Decimal("0"), quantity=Decimal("-500.0")),
            ],
        )
        book_basic.save()
        assert _stored(book_basic, "points") == [("points", 0, 100, 500, 1), ("points equity", 0, 100, -500, 1)]

    def test_changing_the_value_of_an_existing_split_keeps_the_scale(self, book_basic):
        tr = _transaction(book_basic, "edit", Decimal("10"))
        book_basic.save()
        for sp in tr.splits:
            sp.value = sp.quantity = Decimal("25.5") * (1 if sp.value > 0 else -1)
        book_basic.save()
        assert _stored(book_basic, "edit") == [("asset", -2550, 100, -2550, 100), ("exp", 2550, 100, 2550, 100)]

    def test_assigning_with_a_known_scale_rounds_instead_of_truncating(self, book_basic):
        sp = Split(account=book_basic.accounts(name="exp"), value=Decimal("10"))
        sp._value_denom_basis = 100
        sp.value = Decimal("1.999")  # 2.00, not 1.99
        assert (sp._value_num, sp._value_denom) == (200, 100)
        sp.value = Decimal("-1.995")
        assert (sp._value_num, sp._value_denom) == (-200, 100)

    def test_a_split_stored_off_scale_is_left_alone_when_only_its_memo_changes(self, book_basic):
        """Books written before this rule (or by other tools) hold reduced fractions: touching something else
        must not rewrite amounts the caller did not change."""
        tr = _transaction(book_basic, "legacy", Decimal("119.9"))
        book_basic.save()
        book_basic.session.execute(
            text("UPDATE splits SET value_num = value_num / 10, value_denom = 10, "
                 "quantity_num = quantity_num / 10, quantity_denom = 10 WHERE tx_guid = :g"),
            {"g": tr.guid},
        )
        book_basic.save()
        book_basic.session.expire_all()
        tr = book_basic.transactions(description="legacy")
        for sp in tr.splits:
            sp.memo = "only the memo"
        book_basic.save()
        assert _stored(book_basic, "legacy") == [("asset", -1199, 10, -1199, 10), ("exp", 1199, 10, 1199, 10)]
        sp = tr.splits(account=book_basic.accounts(name="exp"))
        sp.value = sp.quantity = Decimal("119.9")  # same number, but now the caller did set it
        other = tr.splits(account=book_basic.accounts(name="asset"))
        other.value = other.quantity = Decimal("-119.9")
        book_basic.save()
        assert _stored(book_basic, "legacy") == [("asset", -11990, 100, -11990, 100), ("exp", 11990, 100, 11990, 100)]


class TestBalanceWhenSplitsAreRemoved(object):
    def test_deleting_a_split_that_unbalances_the_transaction_is_refused(self, book_basic):
        eur = book_basic.default_currency
        asset, exp, inc = (book_basic.accounts(name=n) for n in ("asset", "exp", "inc"))
        tr = Transaction(
            currency=eur,
            description="three legs",
            post_date=date(2026, 1, 15),
            splits=[
                Split(account=asset, value=Decimal("-30")),
                Split(account=exp, value=Decimal("10")),
                Split(account=inc, value=Decimal("20")),
            ],
        )
        book_basic.save()
        book_basic.delete(tr.splits(account=inc))
        with pytest.raises(GncImbalanceError):
            book_basic.save()

    def test_deleting_a_zero_split_keeps_the_transaction_valid(self, book_basic):
        eur = book_basic.default_currency
        asset, exp, inc = (book_basic.accounts(name=n) for n in ("asset", "exp", "inc"))
        tr = Transaction(
            currency=eur,
            description="zero leg",
            post_date=date(2026, 1, 15),
            splits=[
                Split(account=asset, value=Decimal("-30")),
                Split(account=exp, value=Decimal("30")),
                Split(account=inc, value=Decimal("0")),
            ],
        )
        book_basic.save()
        book_basic.delete(tr.splits(account=inc))
        book_basic.save()
        assert len(_stored(book_basic, "zero leg")) == 2


BOOKS_WRITTEN_BY_GNUCASH = ["complex_sample", "simple_sample", "simple_sample.272", "investment", "historical_prices",
                            "ghost_kvp_scheduled_transaction"]
OFF_SCALE = (
    "SELECT COUNT(*) FROM splits s JOIN transactions t ON t.guid = s.tx_guid "
    "JOIN commodities tc ON tc.guid = t.currency_guid JOIN accounts a ON a.guid = s.account_guid "
    "JOIN commodities ac ON ac.guid = a.commodity_guid WHERE s.value_denom <> tc.fraction "
    "OR s.quantity_denom <> (CASE WHEN a.non_std_scu = 1 THEN a.commodity_scu ELSE ac.fraction END)"
)


@pytest.mark.parametrize("name", BOOKS_WRITTEN_BY_GNUCASH)
def test_what_piecash_writes_has_the_scale_of_what_gnucash_wrote(name, tmp_path):
    """The reference is the fixture books, written by GnuCash itself (2.6 to 4.8): every split there is at the
    smallest unit of its currency / commodity. A transaction added by piecash must not be the odd one out."""
    import shutil
    from pathlib import Path

    from piecash import open_book

    copy = tmp_path / "copy.gnucash"
    shutil.copyfile(str(Path(__file__).parent / "books" / (name + ".gnucash")), str(copy))
    with open_book(str(copy), readonly=False, open_if_lock=True, do_backup=False) as book:
        assert book.session.execute(text("SELECT COUNT(*) FROM splits")).scalar() > 0
        assert book.session.execute(text(OFF_SCALE)).scalar() == 0  # what GnuCash wrote
        currency = book.default_currency
        accounts = [a for a in book.accounts if a.commodity == currency and not a.placeholder][:2]
        assert len(accounts) == 2
        Transaction(
            currency=currency,
            description="written by piecash",
            post_date=date(2026, 1, 15),
            splits=[Split(account=accounts[0], value=Decimal("119.9")), Split(account=accounts[1], value=Decimal("-119.9"))],
        )
        book.save()
        assert book.session.execute(text(OFF_SCALE)).scalar() == 0  # and what piecash added
        assert book.session.execute(
            text("SELECT COUNT(*) FROM splits s JOIN transactions t ON t.guid = s.tx_guid WHERE t.description = 'written by piecash'")
        ).scalar() == 2
