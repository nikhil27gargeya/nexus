from nexus.models import Evidence


class Board:
    """Every piece of evidence any tool returned, keyed by a stable ID. Citations must point here."""

    def __init__(self) -> None:
        self._items: dict[str, Evidence] = {}
        self.flags: set[str] = set()

    def add(self, evidence: Evidence) -> Evidence:
        current = self._items.get(evidence.id)
        if current:
            evidence = current.model_copy(
                update={"summary": evidence.summary, "data": {**current.data, **evidence.data}}
            )
        self._items[evidence.id] = evidence
        return evidence

    def get(self, evidence_id: str) -> Evidence | None:
        return self._items.get(evidence_id)

    def of_kind(self, kind: str) -> list[Evidence]:
        return [e for e in self._items.values() if e.kind == kind]

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self._items
