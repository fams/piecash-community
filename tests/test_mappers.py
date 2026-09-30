# -*- coding: utf-8 -*-
"""Mappers configure without SQLAlchemy warnings (SA 1.4 and 2.0).

GnuCash uses polymorphic associations (slots.obj_guid, recurrences.obj_guid,
jobs.owner_guid, invoices.owner_guid/billto_guid): several classes write the
same column on purpose. The relationships declare it with ``overlaps=`` so
SQLAlchemy doesn't warn on every import."""
import warnings

import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import configure_mappers

import piecash  # noqa: F401  (registers every mapped class)
from piecash.business.invoice import Job
from piecash.business.person import Customer, Vendor


def test_configure_mappers_without_sa_warnings():
    with warnings.catch_warnings():
        warnings.simplefilter("error", sa_exc.SAWarning)
        configure_mappers()


def test_job_owner_relationships_filter_by_owner_type():
    for rel, owner_type in ((Job._customer, "2"), (Job._vendor, "4")):
        join = str(rel.property.primaryjoin.compile(compile_kwargs={"literal_binds": True}))
        assert "owner_type" in join and owner_type in join
        assert rel.property.viewonly
    assert Customer.jobs.property.primaryjoin is not None and Vendor.jobs.property.primaryjoin is not None
