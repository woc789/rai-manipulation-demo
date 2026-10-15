"""A fresh isolated SQLite store for each legacy domain scenario."""

import pytest

from db_support import CURRENT
from produce_order_workstation.database import WorkcellDatabase


@pytest.fixture(autouse=True)
def isolated_business_database():
    store = WorkcellDatabase(":memory:")
    token = CURRENT.set(store)
    yield store
    CURRENT.reset(token)
    store.close()
