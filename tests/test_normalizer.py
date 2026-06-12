from app.normalizer import (
    extract_error_records,
    is_error_line,
    normalize,
    record_head,
    signature_hash,
)


def test_user_example_port_and_path():
    line = "port 4000 busy! config missing at /users/foo/project/config.yaml"
    sig = normalize(line)
    assert sig == "port <num> busy! config missing at project > config.yaml"


def test_determinism_and_hash_stability():
    line = "2026-01-14T09:12:33.412Z ERROR bind failed on 10.0.0.5:8080"
    assert normalize(line) == normalize(line)
    assert signature_hash(normalize(line)) == signature_hash(normalize(line))


def test_same_error_different_values_same_signature():
    a = "ERROR server failed to start: port 4000 busy!"
    b = "ERROR server failed to start: port 9999 busy!"
    assert normalize(a) == normalize(b)


def test_timestamp_masked_before_numbers():
    sig = normalize("2026-01-14T09:12:33Z ERROR retry 3 failed")
    assert "<ts>" not in sig.split(" ", 1)[0] or sig.startswith("error")
    # leading timestamp is stripped entirely
    assert sig == "error retry <num> failed"


def test_ip_port_uuid_hex_masking():
    sig = normalize(
        "ERROR conn 10.1.2.3:5432 trace 0xdeadbeefcafe "
        "req 123e4567-e89b-12d3-a456-426614174000"
    )
    assert "<ip>:<num>" in sig
    assert "<hex>" in sig
    assert "<uuid>" in sig


def test_windows_path_and_quotes():
    sig = normalize('ERROR cannot open "secret value" at C:\\Users\\foo\\app\\main.py')
    assert "<str>" in sig
    assert "app > main.py" in sig


def test_severity_filter():
    assert is_error_line("2026-01-14 ERROR boom")
    assert is_error_line("WARN low disk")
    assert is_error_line("connection refused by upstream")  # heuristic
    assert not is_error_line("INFO request served in 12ms")


def test_multiline_stack_trace_grouped():
    raw = (
        "2026-01-14T10:02:12Z ERROR psycopg.OperationalError: connection failed\n"
        "    at Pool._acquire (pool.py:212)\n"
        "    at Service.start (svc.py:40)\n"
        "Caused by: socket timeout\n"
        "2026-01-14T10:02:13Z INFO retrying\n"
        "2026-01-14T10:02:14Z ERROR could not connect to database\n"
    )
    records = extract_error_records(raw)
    assert len(records) == 2
    assert "Pool._acquire" in records[0]
    assert record_head(records[0]).endswith("connection failed")


def test_no_errors_in_clean_log():
    raw = "INFO started\nINFO listening on port 8080\nINFO ready\n"
    assert extract_error_records(raw) == []
