from concurrent.futures import ThreadPoolExecutor

import pytest

from produce_order_workstation.inventory import InventoryLedger


def ledger():
    tick = [0.0]
    book = InventoryLedger(clock=lambda: tick[0], wall_clock=lambda: 1000 + tick[0])
    return book, tick


def test_no_initial_stock_or_truth_seeding():
    book, _ = ledger()
    assert book.snapshot()["items"] == []
    book.reset()
    assert book.snapshot()["epoch"] == 1
    assert book.snapshot()["items"] == []


def test_observations_keep_identity_and_are_not_classification_guarantees():
    book, _ = ledger()
    first = book.observe("apple", "base", [(0.3, 0.1, 0.04)])[0]
    assert book.observe("apple", "base", [(0.301, 0.1, 0.04)])[0] == first
    assert not book.snapshot()["items"][0]["classification_verified"]
    assert book.observe("tomato", "base", [(0.301, 0.1, 0.04)])[0] != first


def test_spatial_association_is_one_to_one_and_frame_scoped():
    book, _ = ledger()
    ids = book.observe("corn", "base", [(0.3, 0, 0.04), (0.32, 0, 0.04)])
    assert len(set(ids)) == 2
    assert book.observe("corn", "camera", [(0.3, 0, 0.04)])[0] not in ids


def test_empty_detection_does_not_clear_stock_and_expiry_is_unknown():
    book, tick = ledger()
    item = book.observe("corn", "base", [(0.3, 0, 0.04)])[0]
    book.observe("corn", "base", [])
    assert len(book.snapshot()["items"]) == 1
    tick[0] = 61
    assert book.snapshot()["items"][0]["state"] == "UNKNOWN"
    with pytest.raises(ValueError):
        book.reserve(item, "order")
    book.reserve(item, "order", refreshed_position=(0.301, 0, 0.04))
    assert book.snapshot()["items"][0]["state"] == "RESERVED"


def test_reservation_owner_and_verified_state_chain():
    book, _ = ledger()
    item = book.observe("corn", "base", [(0.3, 0, 0.04)])[0]
    book.reserve(item, "one")
    with pytest.raises(ValueError):
        book.reserve(item, "two")
    with pytest.raises(ValueError):
        book.transition(item, "two", "CARRYING")
    with pytest.raises(ValueError):
        book.transition(item, "one", "PLACED")
    book.transition(item, "one", "CARRYING")
    book.transition(item, "one", "PLACED")
    book.collect("one")
    assert book.snapshot()["items"][0]["state"] == "COLLECTED"


def test_clearance_exclusions_never_release_another_order_or_carried_item():
    from produce_order_workstation.order import Order, OrderLine

    book, _ = ledger()
    ids = book.observe("corn", "base", [(0.3, -0.3, 0.04), (0.3, 0, 0.04), (0.3, 0.3, 0.04)])
    book.reserve(ids[0], "other")
    book.reserve(ids[1], "current")
    book.transition(ids[1], "current", "CARRYING")
    order = Order("current", "A", (OrderLine("corn", 2),))
    selected, missing = book.reserve_order(order, excluded_item_ids=ids[:2])
    assert not missing
    assert selected == [ids[2]]
    items = {item["item_id"]: item for item in book.snapshot()["items"]}
    assert items[ids[0]]["state"] == "RESERVED"
    assert items[ids[0]]["order_id"] == "other"
    assert items[ids[1]]["state"] == "CARRYING"
    assert items[ids[1]]["order_id"] == "current"


@pytest.mark.parametrize("motion_attempted,expected", [(False, "AVAILABLE"), (True, "UNKNOWN")])
def test_failure_never_blindly_releases_uncertain_moved_item(motion_attempted, expected):
    book, _ = ledger()
    item = book.observe("corn", "base", [(0.3, 0, 0.04)])[0]
    book.reserve(item, "one")
    book.finish_failed("one", motion_attempted)
    assert book.snapshot()["items"][0]["state"] == expected
    if motion_attempted:
        with pytest.raises(ValueError):
            book.reserve(item, "two", refreshed_position=(0.3, 0, 0.04))


def test_only_one_concurrent_reservation_wins():
    book, _ = ledger()
    item = book.observe("corn", "base", [(0.3, 0, 0.04)])[0]
    def reserve(owner):
        try:
            book.reserve(item, owner)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(reserve, ("a", "b", "c", "d"))) == 1


def test_bad_positions_and_large_refresh_jump_rejected():
    book, _ = ledger()
    for points in ([(float("nan"), 0, 0)], [(0, 1)]):
        with pytest.raises(ValueError):
            book.observe("corn", "base", points)
    item = book.observe("corn", "base", [(0.3, 0, 0.04)])[0]
    with pytest.raises(ValueError):
        book.reserve(item, "one", refreshed_position=(0.5, 0, 0.04))
