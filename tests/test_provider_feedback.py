from dataclasses import asdict
import json
import sqlite3

from run_state.provider_feedback import effective_observation, ensure_schema, record_result
from run_state.resource_observation import ResourceDemand, ResourceObservation


def test_unknown_provider_exploration_grows_gradually_and_throttle_cools_down():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    ensure_schema(connection)
    connection.execute("CREATE TABLE managed_admissions(ticket TEXT,status TEXT,child_pid INTEGER,demand_json TEXT)")
    observation = ResourceObservation(100, 8, 1 << 30, 1 << 30, 100, 100, None)
    effective = effective_observation(connection, observation, providers=["codex", "claude"],
                                      boot_id="boot", now_ns=100)
    assert effective.provider_available == {"codex": 1, "claude": 1}
    assert "provider-exploration/v1" in effective.source
    for index, now in enumerate((101, 102, 110)):
        ticket = str(index)
        connection.execute("INSERT INTO managed_admissions VALUES(?,'released',1,?)",
                           (ticket, json.dumps(asdict(ResourceDemand(provider="codex", provider_units=1)))))
        record_result(connection, ticket=ticket, outcome="success", retry_after_ns=0,
                      boot_id="boot", now_ns=now, stable_window_ns=10)
    effective = effective_observation(connection, observation, providers=["codex", "claude"],
                                      boot_id="boot", now_ns=111)
    assert effective.provider_available == {"codex": 2, "claude": 1}
    record_result(connection, ticket="2", outcome="success", retry_after_ns=0,
                  boot_id="boot", now_ns=112, stable_window_ns=10)
    assert connection.execute("SELECT COUNT(*) FROM resource_provider_results").fetchone()[0] == 3
    connection.execute("INSERT INTO managed_admissions VALUES('throttle','released',1,?)",
                       (json.dumps(asdict(ResourceDemand(provider="codex", provider_units=1))),))
    record_result(connection, ticket="throttle", outcome="throttled", retry_after_ns=30,
                  boot_id="boot", now_ns=113, stable_window_ns=10)
    effective = effective_observation(connection, observation, providers=["codex", "claude"],
                                      boot_id="boot", now_ns=120)
    assert effective.provider_available == {"codex": 0, "claude": 1}
    effective = effective_observation(connection, observation, providers=["codex"],
                                      boot_id="boot", now_ns=143)
    assert effective.provider_available["codex"] == 1


def test_known_quota_stays_observed_and_new_boot_returns_conservative_exploration():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    ensure_schema(connection)
    observation = ResourceObservation(100, 8, 1 << 30, 1 << 30, 100, 100, {"codex": 0})
    result = effective_observation(connection, observation, providers=["codex"], boot_id="boot", now_ns=100)
    assert result.provider_available == {"codex": 0}
    connection.execute("UPDATE resource_provider_feedback SET exploration_bound=9")
    unknown = ResourceObservation(101, 8, 1 << 30, 1 << 30, 100, 100, None)
    result = effective_observation(connection, unknown, providers=["codex"], boot_id="next-boot", now_ns=101)
    assert result.provider_available == {"codex": 1}


def test_completed_feedback_can_expand_after_stable_window_without_another_launch():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    ensure_schema(connection)
    connection.execute('CREATE TABLE managed_admissions(ticket TEXT,status TEXT,child_pid INTEGER,demand_json TEXT)')
    observation = ResourceObservation(100, 8, 1 << 30, 1 << 30, 100, 100, None)
    effective_observation(connection, observation, providers=['codex'], boot_id='boot', now_ns=100)
    for index in range(3):
        connection.execute("INSERT INTO managed_admissions VALUES(?,'released',1,?)",
                           (str(index), json.dumps(asdict(ResourceDemand(provider='codex', provider_units=1)))))
        record_result(connection, ticket=str(index), outcome='success', retry_after_ns=0,
                      boot_id='boot', now_ns=101 + index)
    delayed = ResourceObservation(5_000_000_101, 8, 1 << 30, 1 << 30, 100, 100, None)
    result = effective_observation(connection, delayed, providers=['codex'], boot_id='boot', now_ns=5_000_000_101)
    assert result.provider_available['codex'] == 2
    later = ResourceObservation(15_000_000_101, 8, 1 << 30, 1 << 30, 100, 100, None)
    assert effective_observation(connection, later, providers=['codex'], boot_id='boot',
                                 now_ns=15_000_000_101).provider_available['codex'] == 2
