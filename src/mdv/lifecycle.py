from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from mdv.models import MarketSnapshot


@dataclass(frozen=True)
class SourceLifecyclePhase:
    """A configured availability transition for one collected market source."""

    effective_at: datetime
    collect: bool
    status: str | None = None


@dataclass(frozen=True)
class SourceLifecycle:
    """Configured source lifecycle that remains auditable after collection ends."""

    source: str
    phases: tuple[SourceLifecyclePhase, ...]

    def phase_at(self, at: datetime) -> SourceLifecyclePhase | None:
        normalized = at.astimezone(timezone.utc)
        current = None
        for phase in self.phases:
            if normalized >= phase.effective_at:
                current = phase
            else:
                break
        return current


def collection_lifecycle(
    source: str,
    *,
    lifecycles: tuple[SourceLifecycle, ...] = (),
    at: datetime | None = None,
) -> tuple[SourceLifecycle, SourceLifecyclePhase] | None:
    """Return the active configured lifecycle phase for a source, if any."""
    normalized_source = str(source).upper()
    lifecycle = next(
        (item for item in lifecycles if item.source == normalized_source),
        None,
    )
    if lifecycle is None:
        return None
    phase = lifecycle.phase_at(at or datetime.now(timezone.utc))
    return (lifecycle, phase) if phase is not None else None


def source_is_collectable(
    source: str,
    *,
    lifecycles: tuple[SourceLifecycle, ...] = (),
    at: datetime | None = None,
) -> bool:
    """Whether a source may be queried at the supplied UTC instant."""
    active = collection_lifecycle(source, lifecycles=lifecycles, at=at)
    return active is None or active[1].collect


def lifecycle_snapshot(
    snapshot: MarketSnapshot,
    *,
    lifecycles: tuple[SourceLifecycle, ...] = (),
) -> MarketSnapshot:
    """Apply a configured non-terminal availability restriction to a snapshot."""
    try:
        observed_at = datetime.fromisoformat(snapshot.observed_at.replace("Z", "+00:00"))
    except ValueError:
        # Snapshot validation retains responsibility for reporting malformed provider times.
        return snapshot
    active = collection_lifecycle(
        snapshot.source,
        lifecycles=lifecycles,
        at=observed_at,
    )
    if active is None or active[1].status is None or not active[1].collect:
        return snapshot
    return replace(
        snapshot,
        markets=tuple(
            replace(market, status=active[1].status, active=False)
            for market in snapshot.markets
        ),
    )
