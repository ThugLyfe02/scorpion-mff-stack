def test_raw_event_is_idempotent(tmp_path, raw_factory):
    from scorpion.store import Store

    store = Store(tmp_path / "s.db")
    raw = raw_factory("QQQ 719C TODAY @ 1.01")
    assert store.append_raw(raw) is True
    assert store.append_raw(raw) is False


def test_halt_persists(tmp_path):
    from scorpion.store import Store

    store = Store(tmp_path / "s.db")
    store.set_halt(True, "operator")
    health = store.health_snapshot()
    assert health["halt"]["halted"] is True
