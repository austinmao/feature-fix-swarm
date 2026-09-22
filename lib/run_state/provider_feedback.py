"""Durable bounded exploration when a provider exposes no concurrency quota.

The bound is an admission policy, never a claimed subscription entitlement.
Only completed, identity-settled leases supply feedback. Local resource bounds
still apply independently to every reservation.
"""
from dataclasses import replace
import json


def ensure_schema(connection):
    connection.execute("CREATE TABLE IF NOT EXISTS resource_provider_feedback ("
                       "provider TEXT PRIMARY KEY,boot_id TEXT NOT NULL,policy_version INTEGER NOT NULL,"
                       "exploration_bound INTEGER NOT NULL CHECK(exploration_bound>0),"
                       "successful_completions INTEGER NOT NULL,stable_since_ns INTEGER NOT NULL,"
                       "cooldown_until_ns INTEGER NOT NULL,last_feedback_ns INTEGER NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS resource_provider_results ("
                       "ticket TEXT PRIMARY KEY,provider TEXT NOT NULL,outcome TEXT NOT NULL,"
                       "retry_after_ns INTEGER NOT NULL,observed_ns INTEGER NOT NULL)")


def effective_observation(connection, observation, *, providers, boot_id, now_ns):
    """Keep known observations exact; annotate unknown providers as exploration."""
    if observation is None:
        return None
    from .resource_observation import ResourceObservationRefused
    try:
        observation.validate(now_ns=now_ns, max_age_ns=30_000_000_000)
        fresh = True
    except ResourceObservationRefused:
        fresh = False
    available = dict(observation.provider_available or {})
    exploratory = []
    for provider in sorted({provider for provider in providers if provider is not None}):
        row = connection.execute("SELECT * FROM resource_provider_feedback WHERE provider=?", (provider,)).fetchone()
        if row is None or row["boot_id"] != boot_id or row["last_feedback_ns"] > now_ns:
            connection.execute("INSERT INTO resource_provider_feedback VALUES(?,?,1,1,0,?,0,?) "
                               "ON CONFLICT(provider) DO UPDATE SET boot_id=excluded.boot_id,"
                               "exploration_bound=1,successful_completions=0,stable_since_ns=excluded.stable_since_ns,"
                               "cooldown_until_ns=0,last_feedback_ns=excluded.last_feedback_ns",
                               (provider, boot_id, now_ns, now_ns))
            row = connection.execute("SELECT * FROM resource_provider_feedback WHERE provider=?", (provider,)).fetchone()
        if row["cooldown_until_ns"] > now_ns:
            available[provider] = 0
        elif available.get(provider) is None:
            # Fast completed qualification calls can finish before the stable
            # window closes. Their verified feedback may authorize one later
            # exploration step; requiring another completion here deadlocks a
            # parent/child group whose first feasible envelope is two calls.
            if (fresh and row['successful_completions'] >= 3
                    and now_ns - row['stable_since_ns'] >= 5_000_000_000
                    and 0 <= now_ns - row['last_feedback_ns'] <= 30_000_000_000):
                connection.execute("UPDATE resource_provider_feedback SET exploration_bound=exploration_bound+1,"
                                   "successful_completions=0,stable_since_ns=? WHERE provider=?",
                                   (now_ns, provider))
                row = connection.execute("SELECT * FROM resource_provider_feedback WHERE provider=?", (provider,)).fetchone()
            available[provider] = row["exploration_bound"]
            exploratory.append(provider)
    source = observation.source
    if exploratory:
        source += ";provider-exploration/v1:" + ",".join(exploratory)
    return replace(observation, provider_available=available, source=source)


def record_result(connection, *, ticket, outcome, retry_after_ns, boot_id, now_ns,
                  stable_window_ns=5_000_000_000, successes_to_expand=3):
    if outcome not in {"success", "throttled", "failed", "uncertain"} or type(retry_after_ns) is not int or retry_after_ns < 0:
        raise ValueError("invalid provider feedback")
    lease = connection.execute("SELECT * FROM managed_admissions WHERE ticket=?", (ticket,)).fetchone()
    if lease is None or lease["status"] not in {"released", "reclaimed"} or lease["child_pid"] is None:
        raise ValueError("unsettled provider feedback")
    demand = json.loads(lease["demand_json"])
    provider = demand.get("provider")
    if not provider or not demand.get("provider_units"):
        return
    prior = connection.execute("SELECT * FROM resource_provider_results WHERE ticket=?", (ticket,)).fetchone()
    if prior is not None:
        if prior["outcome"] != outcome or prior["retry_after_ns"] != retry_after_ns:
            raise ValueError("conflicting provider feedback")
        return
    row = connection.execute("SELECT * FROM resource_provider_feedback WHERE provider=?", (provider,)).fetchone()
    if row is None or row["boot_id"] != boot_id or now_ns < row["last_feedback_ns"]:
        raise ValueError("provider clock unknown")
    bound, successes, stable, cooldown = (row["exploration_bound"], row["successful_completions"],
                                         row["stable_since_ns"], row["cooldown_until_ns"])
    if outcome == "throttled":
        bound, successes, stable = max(1, bound // 2), 0, now_ns
        cooldown = max(cooldown, now_ns + max(retry_after_ns, stable_window_ns))
    elif outcome == "success" and now_ns >= cooldown:
        successes += 1
        if successes >= successes_to_expand and now_ns - stable >= stable_window_ns:
            bound, successes, stable = bound + 1, 0, now_ns
    else:
        successes, stable = 0, now_ns
    connection.execute("UPDATE resource_provider_feedback SET exploration_bound=?,successful_completions=?,"
                       "stable_since_ns=?,cooldown_until_ns=?,last_feedback_ns=? WHERE provider=?",
                       (bound, successes, stable, cooldown, now_ns, provider))
    connection.execute("INSERT INTO resource_provider_results VALUES(?,?,?,?,?)",
                       (ticket, provider, outcome, retry_after_ns, now_ns))
