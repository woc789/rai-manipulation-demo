from concurrent.futures import ThreadPoolExecutor

import pytest

from db_support import make_inventory, reserve, reserve_order, mark_placed


def ledger():
    tick = [0.0]
    book = make_inventory(clock=lambda: tick[0], wall_clock=lambda: 1000 + tick[0])
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
    assert book.summary()["apple"]["UNKNOWN"] == 1
    assert book.summary()["tomato"]["UNKNOWN"] == 1
    assert book.ambiguous_skus() == {"apple", "tomato"}


def test_cross_sku_overlap_is_not_promised_as_two_products():
    from produce_order_workstation.order import Order, OrderLine

    book, _ = ledger()
    book.observe("apple", "base", [(0.3, 0.1, 0.04)])
    book.observe("tomato", "base", [(0.301, 0.1, 0.04)])
    _, missing = reserve_order(book, Order("ambiguous", "A", (OrderLine("apple", 1),)))
    assert missing == {"apple": 1}
    _, missing = reserve_order(
        book, Order("ambiguous-tomato", "A", (OrderLine("tomato", 1),))
    )
    assert missing == {"tomato": 1}


def test_old_other_sku_observation_does_not_poison_fresh_stock():
    book, tick = ledger()
    book.observe("apple", "base", [(0.3, 0.1, 0.04)])
    tick[0] = 61.0
    fresh = book.observe("tomato", "base", [(0.301, 0.1, 0.04)])[0]
    assert (
        next(row for row in book.snapshot()["items"] if row["item_id"] == fresh)[
            "state"
        ]
        == "AVAILABLE"
    )


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
        reserve(book, item, "order")
    reserve(book, item, "order", refreshed_position=(0.301, 0, 0.04))
    assert book.snapshot()["items"][0]["state"] == "RESERVED"


def test_reservation_owner_and_verified_state_chain():
    book, _ = ledger()
    item = book.observe("corn", "base", [(0.3, 0, 0.04)])[0]
    reserve(book, item, "one")
    with pytest.raises(ValueError):
        reserve(book, item, "two")
    with pytest.raises(ValueError):
        book.transition(item, "two", "CARRYING")
    with pytest.raises(ValueError):
        mark_placed(book, item, "one")
    book.transition(item, "one", "CARRYING")
    mark_placed(book, item, "one")
    book.collect("one")
    assert book.snapshot()["items"][0]["state"] == "COLLECTED"


def test_clearance_exclusions_never_release_another_order_or_carried_item():
    from produce_order_workstation.order import Order, OrderLine

    book, _ = ledger()
    ids = book.observe(
        "corn", "base", [(0.3, -0.3, 0.04), (0.3, 0, 0.04), (0.3, 0.3, 0.04)]
    )
    reserve(book, ids[0], "other")
    reserve(book, ids[1], "current")
    book.transition(ids[1], "current", "CARRYING")
    order = Order("current", "A", (OrderLine("corn", 2),))
    selected, missing = reserve_order(book, order, excluded_item_ids=ids[:2])
    assert not missing
    assert selected == [ids[2]]
    items = {item["item_id"]: item for item in book.snapshot()["items"]}
    assert items[ids[0]]["state"] == "RESERVED"
    assert items[ids[0]]["order_id"] == "other"
    assert items[ids[1]]["state"] == "CARRYING"
    assert items[ids[1]]["order_id"] == "current"


@pytest.mark.parametrize(
    "motion_attempted,expected", [(False, "AVAILABLE"), (True, "UNKNOWN")]
)
def test_failure_never_blindly_releases_uncertain_moved_item(
    motion_attempted, expected
):
    book, _ = ledger()
    item = book.observe("corn", "base", [(0.3, 0, 0.04)])[0]
    reserve(book, item, "one")
    book.finish_failed("one", motion_attempted)
    assert book.snapshot()["items"][0]["state"] == expected
    if motion_attempted:
        with pytest.raises(ValueError):
            reserve(book, item, "two", refreshed_position=(0.3, 0, 0.04))


def test_only_one_concurrent_reservation_wins():
    book, _ = ledger()
    item = book.observe("corn", "base", [(0.3, 0, 0.04)])[0]

    def compete(owner):
        try:
            reserve(book, item, owner)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(compete, ("a", "b", "c", "d"))) == 1


def test_bad_positions_and_large_refresh_jump_rejected():
    book, _ = ledger()
    for points in ([(float("nan"), 0, 0)], [(0, 1)]):
        with pytest.raises(ValueError):
            book.observe("corn", "base", points)
    item = book.observe("corn", "base", [(0.3, 0, 0.04)])[0]
    with pytest.raises(ValueError):
        reserve(book, item, "one", refreshed_position=(0.5, 0, 0.04))
