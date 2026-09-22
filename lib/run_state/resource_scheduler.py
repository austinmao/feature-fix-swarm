"""Pure fair reservation scheduler for the shared SQLite admission authority."""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Iterable

from .resource_observation import (
    ResourceDemand,
    ResourceObservation,
    ResourceObservationRefused,
)


@dataclass(frozen=True)
class SchedulingDecision:
    admitted: bool
    code: str
    limiting_resource: str | None
    observation_age_ns: int | None
    effective_bound: dict[str, int | None]
    next_recheck_ns: int


def demand_from_record(raw: str) -> ResourceDemand:
    try:
        value = json.loads(raw)
        return ResourceDemand(**value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ResourceObservationRefused("RESOURCE_DEMAND_INVALID") from error


class ResourceScheduler:
    """Checks durable reservations against one observed envelope.

    The scheduler does not translate capacity into a count.  That keeps active
    count reporting-only and makes resource classes independently feasible.
    """

    def __init__(
        self, *, cooldown_ns: int = 5_000_000_000, max_age_ns: int = 30_000_000_000
    ) -> None:
        self.cooldown_ns, self.max_age_ns = cooldown_ns, max_age_ns

    @staticmethod
    def _used(rows: Iterable[dict]) -> ResourceDemand:
        totals = dict(
            cpu=0,
            memory_bytes=0,
            disk_bytes=0,
            io_units=0,
            processes=0,
            provider_units=0,
        )
        providers: dict[str, int] = {}
        for row in rows:
            demand = demand_from_record(row["demand_json"])
            for key in totals:
                totals[key] += getattr(demand, key)
            if demand.provider:
                providers[demand.provider] = (
                    providers.get(demand.provider, 0) + demand.provider_units
                )
        # Provider total is handled per provider below; it is not a generic pool.
        return ResourceDemand(**totals)

    def decide(
        self,
        demand: ResourceDemand,
        active_rows: Iterable[dict],
        observation: ResourceObservation | None,
        *,
        ceilings: dict[str, int] | None = None,
        now_ns: int | None = None,
    ) -> SchedulingDecision:
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        next_recheck = now_ns + self.cooldown_ns
        if observation is None:
            return SchedulingDecision(
                False,
                "RESOURCE_OBSERVATION_UNKNOWN",
                "observation",
                None,
                {},
                next_recheck,
            )
        try:
            observation.validate(now_ns=now_ns, max_age_ns=self.max_age_ns)
        except ResourceObservationRefused as error:
            return SchedulingDecision(
                False,
                error.code,
                "observation",
                observation.age_ns(now_ns),
                {},
                next_recheck,
            )
        active_rows = list(active_rows)
        try:
            used = self._used(active_rows)
        except ResourceObservationRefused as error:
            return SchedulingDecision(
                False,
                error.code,
                "reservation",
                observation.age_ns(now_ns),
                {},
                next_recheck,
            )
        limits = {
            "cpu": observation.cpu_available,
            "memory_bytes": observation.memory_available_bytes,
            "disk_bytes": observation.disk_available_bytes,
            "io_units": observation.io_available_units,
            "processes": observation.process_available,
        }
        if ceilings:
            for key, value in ceilings.items():
                if key not in limits or type(value) is not int or value < 0:
                    return SchedulingDecision(
                        False,
                        "RESOURCE_CEILING_INVALID",
                        key,
                        observation.age_ns(now_ns),
                        limits,
                        next_recheck,
                    )
                limits[key] = value if limits[key] is None else min(limits[key], value)
        for field in ("cpu", "memory_bytes", "disk_bytes", "io_units", "processes"):
            required, limit = getattr(demand, field), limits[field]
            if required and limit is None:
                return SchedulingDecision(
                    False,
                    "RESOURCE_WAIT",
                    field,
                    observation.age_ns(now_ns),
                    limits,
                    next_recheck,
                )
            if limit is not None and getattr(used, field) + required > limit:
                return SchedulingDecision(
                    False,
                    "RESOURCE_WAIT",
                    field,
                    observation.age_ns(now_ns),
                    limits,
                    next_recheck,
                )
        if demand.provider:
            available = (
                None
                if observation.provider_available is None
                else observation.provider_available.get(demand.provider)
            )
            if available is None:
                return SchedulingDecision(
                    False,
                    "RESOURCE_WAIT",
                    "provider:" + demand.provider,
                    observation.age_ns(now_ns),
                    limits,
                    next_recheck,
                )
            provider_used = sum(
                demand_from_record(row["demand_json"]).provider_units
                for row in active_rows
                if demand_from_record(row["demand_json"]).provider == demand.provider
            )
            if provider_used + demand.provider_units > available:
                return SchedulingDecision(
                    False,
                    "RESOURCE_WAIT",
                    "provider:" + demand.provider,
                    observation.age_ns(now_ns),
                    limits,
                    next_recheck,
                )
        return SchedulingDecision(
            True, "ADMITTED", None, observation.age_ns(now_ns), limits, next_recheck
        )

    def decide_group(
        self,
        demands: Iterable[ResourceDemand],
        active_rows: Iterable[dict],
        observation: ResourceObservation | None,
        *,
        ceilings=None,
        now_ns=None,
    ):
        """Reserve every required peer cumulatively or reserve none."""
        reservations = list(active_rows)
        decisions = []
        for demand in demands:
            decision = self.decide(
                demand, reservations, observation, ceilings=ceilings, now_ns=now_ns
            )
            decisions.append(decision)
            if not decision.admitted:
                return decisions
            reservations.append(
                {"demand_json": json.dumps(demand.record(), sort_keys=True)}
            )
        return decisions
