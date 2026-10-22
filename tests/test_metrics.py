import csv

from evewatch.metrics import FIELDS, MetricsLogger, handle_count, memory_mb, summarize


def test_process_probes_return_sane_values():
    rss, peak = memory_mb()
    assert rss > 5 and peak >= rss
    assert handle_count() > 5


def test_logger_writes_header_and_rows_on_interval(tmp_path):
    clock = [100.0]
    path = tmp_path / "m.csv"
    logger = MetricsLogger(path, interval_sec=60, extra=lambda: "q=0", clock=lambda: clock[0])
    assert logger.tick() is True  # first sample immediately
    clock[0] += 30
    assert logger.tick() is False
    clock[0] += 31
    assert logger.tick() is True
    logger.close()
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8")))
    assert list(rows[0].keys()) == FIELDS
    assert len(rows) == 2 and rows[0]["extra"] == "q=0"
    assert float(rows[1]["uptime_s"]) == 61.0
    assert int(rows[0]["threads"]) >= 1


def test_logger_appends_without_a_second_header(tmp_path):
    path = tmp_path / "m.csv"
    for _ in range(2):
        logger = MetricsLogger(path, interval_sec=1)
        logger.tick()
        logger.close()
    assert path.read_text(encoding="utf-8").count("uptime_s") == 1


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for r in rows:
            w.writerow(r)


def test_summarize_flat_run_has_no_growth(tmp_path):
    path = tmp_path / "m.csv"
    write_csv(path, [["t", i * 60, 60.0, 61.0, 0.8, i, 5, 120, 50000, ""] for i in range(60)])
    s = summarize(path)
    assert s["enough_data"] and s["hours"] == round(59 * 60 / 3600, 2)
    assert s["rss_growth_mb_per_hour"] == 0.0 and s["handles_growth"] == 0 and s["threads_max"] == 5


def test_summarize_detects_a_leak(tmp_path):
    path = tmp_path / "m.csv"
    write_csv(path, [["t", i * 60, 60.0 + i * 0.5, 80.0, 1.0, i, 5, 120 + i, 50000 + i * 100, ""] for i in range(60)])
    s = summarize(path)
    assert s["rss_growth_mb_per_hour"] > 20
    assert s["handles_growth"] > 40


def test_first_sample_has_no_absurd_cpu_value(tmp_path):
    clock = [5.0]
    path = tmp_path / "m.csv"
    logger = MetricsLogger(path, interval_sec=60, clock=lambda: clock[0])
    logger.tick()
    logger.close()
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8")))
    assert float(rows[0]["cpu_pct"]) == 0.0
