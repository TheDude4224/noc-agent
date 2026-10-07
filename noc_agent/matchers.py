"""Runbook selectors: which alerts a runbook may claim.

Each entry in a runbook's `matches` list is one alternative, written like a PromQL
selector without the metric:

    "*"                                    any alert
    "GuestStopped"                         that alertname (the original form)
    'GuestStopped{node="OryahCloud-01"}'   that alertname AND these labels
    '{origin="legacy-ct400", severity!="info"}'   any alertname with these labels

Matchers are =, !=, =~ and !~. Regexes are fully anchored, as in Prometheus, so
`job=~"node"` means exactly "node". A label that is missing compares as "". The
label set seen by a matcher is the alert's labels plus alertname, instance, host
and severity. Entries are ORed; matchers inside one pair of braces are ANDed.

Bad syntax raises at load time. A runbook that cannot be parsed must never load
and quietly match nothing (or everything).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NAME = r"[A-Za-z_:][A-Za-z0-9_:]*"
_ENTRY = re.compile(rf"^\s*(?P<name>\*|{_NAME})?\s*(?:\{{(?P<body>.*)\}})?\s*$", re.S)
_MATCHER = re.compile(rf'\s*(?P<label>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<op>=~|!~|!=|=)\s*"(?P<value>(?:[^"\\]|\\.)*)"\s*(?:,|$)')


@dataclass(frozen=True)
class Matcher:
    label: str
    op: str
    value: str

    def __post_init__(self) -> None:
        if self.op in ("=~", "!~"):
            object.__setattr__(self, "_re", re.compile(f"(?:{self.value})\\Z"))

    def ok(self, labels: dict[str, str]) -> bool:
        v = labels.get(self.label, "")
        if self.op == "=":
            return v == self.value
        if self.op == "!=":
            return v != self.value
        hit = self._re.match(v) is not None  # type: ignore[attr-defined]
        return hit if self.op == "=~" else not hit


@dataclass(frozen=True)
class Selector:
    alertname: str | None          # None or "*" = any alertname
    matchers: tuple[Matcher, ...]

    def ok(self, labels: dict[str, str]) -> bool:
        if self.alertname not in (None, "*") and labels.get("alertname", "") != self.alertname:
            return False
        return all(m.ok(labels) for m in self.matchers)


def parse_selector(entry: str) -> Selector:
    m = _ENTRY.match(entry)
    if not m or (m.group("name") is None and m.group("body") is None):
        raise ValueError(f"bad runbook match entry: {entry!r}")
    body = (m.group("body") or "").strip()
    matchers: list[Matcher] = []
    pos = 0
    while pos < len(body):
        mm = _MATCHER.match(body, pos)
        if not mm or mm.end() == pos:
            raise ValueError(f"bad matcher in {entry!r} near {body[pos:pos + 30]!r}")
        value = mm.group("value").replace('\\"', '"')
        try:
            matchers.append(Matcher(mm.group("label"), mm.group("op"), value))
        except re.error as e:
            raise ValueError(f"bad regex in {entry!r}: {e}") from e
        pos = mm.end()
    return Selector(m.group("name"), tuple(matchers))


def label_view(alertname: str, labels: dict[str, str], *, instance: str = "", host: str = "",
               severity: str = "") -> dict[str, str]:
    """What a matcher sees: the alert's labels plus its normalized fields."""
    view = dict(labels)
    view["alertname"] = alertname
    for k, v in (("instance", instance), ("host", host), ("severity", severity)):
        if v and k not in view:
            view[k] = v
    return view
