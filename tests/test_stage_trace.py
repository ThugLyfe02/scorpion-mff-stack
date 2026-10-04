import asyncio

from scorpion.pipeline import Pipeline
from scorpion.stage_trace import load_stage_latency_report, storage_snapshot
from scorpion.store import Store


def test_pipeline_persists_stage_latency_without_extra_path(tmp_path, raw_factory):
    path = tmp_path / "trace.db"
    store = Store(path)
    pipeline = Pipeline(store, allowed_author_ids=frozenset({"author"}))
    asyncio.run(pipeline.handle(raw_factory("QQQ 719C TODAY @ 1.01")))

    report = load_stage_latency_report(path)
    assert report.count == 1
    assert report.bottleneck_stage is not None
    assert report.stages["parse_us"].count == 1
    assert report.stages["db_precommit_us"].max_us >= 0

    storage = storage_snapshot(path)
    assert storage.db_bytes > 0
    assert storage.page_count > 0
    assert storage.page_size > 0
    assert storage.journal_mode.lower() == "wal"
