"""Bulk readers: answer a question about *the whole book* in one or two SQL queries.

The ORM way of asking (``account.get_balance()`` for each account, ``split["a/b"]``
for each split) issues several queries per object. On a book with years of
history that takes many seconds; these helpers do the same aggregation in the
database and return plain Python values.

They are exposed as :meth:`piecash.core.book.Book.account_balances`,
:meth:`piecash.core.book.Book.slot_values` and the row readers
:meth:`piecash.core.book.Book.account_rows`, :meth:`piecash.core.book.Book.transaction_rows`
and :meth:`piecash.core.book.Book.split_rows`.
"""
import datetime
from collections import defaultdict, namedtuple
from decimal import Decimal

from sqlalchemy import func, select, type_coerce
from sqlalchemy.orm import aliased

from ..kvp import KVP_Type, Slot
from ..sa_extra import _DateTime
from .account import Account
from .commodity import Commodity
from .transaction import Split, Transaction

_NATURAL_SIGN_TYPES = {"LIABILITY", "PAYABLE", "CREDIT", "INCOME", "EQUITY"}


def account_balances(book, at_date=None, natural_sign=True):
    """Balance of every account, computed in a single grouped SQL query.

    Same number as ``account.get_balance(recurse=False, natural_sign=natural_sign,
    at_date=at_date)`` for each account: the sum of the quantities of its splits,
    expressed in the account's own commodity (no price conversion).

    Attributes:
        at_date (:class:`datetime.date`, optional): only transactions posted on or
            before this day count. Default: all of them.
        natural_sign (bool, optional): revert the sign for accounts of type
            LIABILITY, PAYABLE, CREDIT, INCOME and EQUITY (default True), like
            :meth:`piecash.core.account.Account.get_balance`.

    Returns:
        dict: ``{account: Decimal}`` with one entry per account of the book (root
        account excluded), ``Decimal(0)`` for those without splits.
    """
    if isinstance(at_date, datetime.datetime):
        at_date = at_date.date()

    query = (
        select(
            Split.account_guid,
            Split._quantity_denom,
            func.sum(Split._quantity_num),
        )
        .join(Transaction, Transaction.guid == Split.transaction_guid)
        .group_by(Split.account_guid, Split._quantity_denom)
    )
    if at_date is not None:
        query = query.where(Transaction._post_date <= at_date)

    totals = defaultdict(Decimal)
    for account_guid, denom, num in book.session.execute(query):
        # denominators differ between splits of the same account (1, 100, 1000...):
        # sum each group as num/denom, exactly like the hybrid property does
        totals[account_guid] += Decimal(int(num)) / Decimal(int(denom))

    balances = {}
    for account in book.session.query(Account).filter(Account.parent != None):  # noqa: E711
        balance = totals.get(account.guid, Decimal(0))
        if natural_sign and account.type in _NATURAL_SIGN_TYPES:
            balance = -balance
        balances[account] = balance
    return balances


_VALUE_COLUMNS = {
    KVP_Type.KVP_TYPE_GINT64: "int64_val",
    KVP_Type.KVP_TYPE_DOUBLE: "double_val",
    KVP_Type.KVP_TYPE_STRING: "string_val",
    KVP_Type.KVP_TYPE_GUID: "guid_val",
    KVP_Type.KVP_TYPE_TIMESPEC: "timespec_val",
    KVP_Type.KVP_TYPE_GDATE: "gdate_val",
    KVP_Type.KVP_TYPE_FRAME: "guid_val",
}


def slot_values(book, path, of=None):
    """Value of one key-value-pair slot for *every* object that has it, in one query.

    ``path`` is the slash-separated path of the slot, as in ``split["user-keys/tags"]``
    (a nested frame ``user-keys`` holding a slot ``tags``). The ORM reads one slot
    per object and per level; this resolves the whole path with chained joins.

    Attributes:
        path (str): slot name or ``frame/…/name`` path.
        of (class, optional): restrict to objects of this mapped class, e.g.
            :class:`piecash.core.transaction.Split` or
            :class:`piecash.core.account.Account`. Default: any object.

    Returns:
        dict: ``{object guid: value}`` where value is converted by slot type (int,
        float, str, :class:`decimal.Decimal`, :class:`datetime.date`/
        :class:`datetime.datetime`; for a frame or guid slot, the guid text).
        Objects without the slot are absent.

    Example:
        ``book.slot_values("user-keys/tags", of=Split)`` -> ``{split_guid: "#a,#b"}``
    """
    parts = [p for p in path.split("/") if p]
    if not parts:
        raise ValueError("path must name a slot, e.g. 'notes' or 'frame/name'")

    table = Slot.__table__
    levels = [table.alias("s%d" % i) for i in range(len(parts))]
    query = select(levels[0].c.obj_guid)
    last = levels[-1]
    query = query.add_columns(
        last.c.slot_type,
        last.c.int64_val,
        last.c.double_val,
        last.c.string_val,
        last.c.guid_val,
        last.c.timespec_val,
        last.c.gdate_val,
        last.c.numeric_val_num,
        last.c.numeric_val_denom,
    ).select_from(levels[0])

    where = []
    for depth, level in enumerate(levels):
        where.append(level.c.name == "/".join(parts[: depth + 1]))
        if depth > 0:
            # a nested slot hangs off the frame (guid_val) of the slot above it
            query = query.join(level, level.c.obj_guid == levels[depth - 1].c.guid_val)
    query = query.where(*where)
    if of is not None:
        query = query.where(levels[0].c.obj_guid.in_(select(of.__table__.c.guid)))

    values = {}
    for row in book.session.execute(query):
        (obj_guid, slot_type, int_val, double_val, string_val, guid_val, time_val, date_val, num, denom) = row
        if slot_type == KVP_Type.KVP_TYPE_NUMERIC:
            value = Decimal(int(num)) / Decimal(int(denom))
        else:
            column = _VALUE_COLUMNS.get(slot_type)
            if column is None:
                continue  # binary / glist: not a plain value
            value = {
                "int64_val": int_val,
                "double_val": double_val,
                "string_val": string_val,
                "guid_val": guid_val,
                "timespec_val": time_val,
                "gdate_val": date_val,
            }[column]
        values[obj_guid] = value
    return values


class _StoredDateTime(_DateTime):
    """The datetime exactly as stored (naive, UTC), with no timezone conversion either way."""

    cache_ok = True

    def process_bind_param(self, value, dialect):
        return value

    def process_result_value(self, value, dialect):
        return value


AccountRow = namedtuple("AccountRow", "guid name parent_guid type commodity_guid mnemonic")
TransactionRow = namedtuple("TransactionRow", "guid post_date description currency_guid num")
SplitRow = namedtuple(
    "SplitRow",
    "guid transaction_guid account_guid post_date description memo "
    "value_num value_denom quantity_num quantity_denom reconcile_state",
)


def _stored_day(value):
    return None if value is None else value.date()


def _day_bounds(query, column, from_date, to_date):
    if from_date is not None:
        query = query.where(column >= datetime.datetime.combine(from_date, datetime.time.min))
    if to_date is not None:
        query = query.where(column < datetime.datetime.combine(to_date, datetime.time.min) + datetime.timedelta(days=1))
    return query


def account_rows(book):
    """Every account of the book as a plain row, in one query (root and template root included).

    Returns:
        list: :class:`AccountRow` ``(guid, name, parent_guid, type, commodity_guid, mnemonic)``;
        ``mnemonic`` is the one of the account's commodity (None if it has none).
    """
    a, c = Account.__table__, Commodity.__table__
    query = select(a.c.guid, a.c.name, a.c.parent_guid, a.c.account_type, a.c.commodity_guid, c.c.mnemonic).select_from(
        a.outerjoin(c, c.c.guid == a.c.commodity_guid)
    )
    return [AccountRow(*row) for row in book.session.execute(query)]


def transaction_rows(book, from_date=None, to_date=None):
    """Every transaction of the book as a plain row, in one query (those without splits included).

    Attributes:
        from_date, to_date (:class:`datetime.date`, optional): keep the transactions posted
            in this closed interval of stored days.

    Returns:
        list: :class:`TransactionRow` ``(guid, post_date, description, currency_guid, num)``.
        ``post_date`` is the :class:`datetime.date` **as stored** (no timezone conversion): the
        posting day for books written with GnuCash's neutral time (10:59 UTC).
    """
    t = Transaction.__table__
    posted = type_coerce(t.c.post_date, _StoredDateTime())
    query = _day_bounds(select(t.c.guid, posted, t.c.description, t.c.currency_guid, t.c.num), posted, from_date, to_date)
    return [TransactionRow(g, _stored_day(d), ds, cur, num) for g, d, ds, cur, num in book.session.execute(query)]


def split_rows(book, account=None, from_date=None, to_date=None):
    """Every split of the book joined to its transaction, as plain rows, in one query.

    Loading the same data through the ORM (``for tr in book.transactions: for sp in tr.splits``)
    builds one object per row and issues a query per transaction.

    Attributes:
        account (:class:`piecash.core.account.Account` or guid, optional): only the splits of this account.
        from_date, to_date (:class:`datetime.date`, optional): keep the splits whose transaction
            is posted in this closed interval of stored days.

    Returns:
        list: :class:`SplitRow` ``(guid, transaction_guid, account_guid, post_date, description, memo,
        value_num, value_denom, quantity_num, quantity_denom, reconcile_state)``. Amounts come as the
        stored integers (``value = value_num / value_denom``) so the caller chooses Decimal, Fraction
        or float; ``post_date`` is the day as stored, like :func:`transaction_rows`.
    """
    s, t = Split.__table__, Transaction.__table__
    posted = type_coerce(t.c.post_date, _StoredDateTime())
    query = select(
        s.c.guid,
        s.c.tx_guid,
        s.c.account_guid,
        posted,
        t.c.description,
        s.c.memo,
        s.c.value_num,
        s.c.value_denom,
        s.c.quantity_num,
        s.c.quantity_denom,
        s.c.reconcile_state,
    ).select_from(s.join(t, t.c.guid == s.c.tx_guid))
    if account is not None:
        query = query.where(s.c.account_guid == getattr(account, "guid", account))
    query = _day_bounds(query, posted, from_date, to_date)
    return [SplitRow(r[0], r[1], r[2], _stored_day(r[3]), *r[4:]) for r in book.session.execute(query)]
