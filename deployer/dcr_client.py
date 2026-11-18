"""Minimal client for the DCR Active Repository (dcrgraphs.net) REST API.

P4 executes its quality investigation as a real DCR case: the repository
decides which activities are enabled. Endpoints, per the DCR documentation:

  POST /graphs/{graph}/sims                       create instance (id in x-dcr-simulation-id)
  GET  /graphs/{graph}/sims/{sim}/events?filter=  list events (XML)
  POST /graphs/{graph}/sims/{sim}/events/{event}  execute an event

Authentication is HTTP Basic with the team's dcrgraphs.net account. Credentials
come from the environment (DCR_USERNAME / DCR_PASSWORD); never commit them.

Without those credentials P4 uses LocalDCR: the same graph file
(dcr/p4_quality_investigation.xml) executed with flat DCR semantics (condition,
response, include, exclude, milestone) and each case's marking stored in SQLite.
/health reports which engine is active; local runs are labelled as such.
"""

import base64
import json
import os
import sqlite3
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class DCRUnavailable(Exception):
    """DCR could not be reached or answered unexpectedly; retry later."""


class DCRClient:
    mode = "dcr-repository"

    def __init__(self, graph_id=None, username=None, password=None, base_url=None):
        self.graph_id = graph_id or os.getenv("DCR_GRAPH_ID", "")
        self.username = username or os.getenv("DCR_USERNAME", "")
        self.password = password or os.getenv("DCR_PASSWORD", "")
        self.base_url = (
            base_url or os.getenv("DCR_API", "https://repository.dcrgraphs.net/api")
        ).rstrip("/")

    def configured(self):
        return bool(self.graph_id and self.username and self.password)

    def _call(self, method, path, body=None):
        if not self.configured():
            raise DCRUnavailable(
                "DCR is not configured: set DCR_GRAPH_ID, DCR_USERNAME and DCR_PASSWORD"
            )
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        headers = {"Authorization": f"Basic {token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}/graphs/{quote(self.graph_id)}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urlopen(request, timeout=15) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as error:
            detail = error.read().decode(errors="replace")[:300]
            raise DCRUnavailable(f"DCR answered {error.code}: {detail}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise DCRUnavailable(f"DCR unreachable: {error}") from error

    def create_simulation(self, title=None):
        _, headers, _ = self._call(
            "POST", "/sims", {"title": title} if title else None
        )
        sim_id = next(
            (v for k, v in headers.items() if k.lower() == "x-dcr-simulation-id"),
            None,
        )
        if not sim_id:
            raise DCRUnavailable("DCR did not return x-dcr-simulation-id")
        return str(sim_id)

    def events(self, sim_id, filter_="enabled-or-pending"):
        _, _, raw = self._call(
            "GET", f"/sims/{quote(sim_id)}/events?filter={quote(filter_)}"
        )
        return parse_events(raw)

    def execute(self, sim_id, event_id, role=None):
        self._call(
            "POST",
            f"/sims/{quote(sim_id)}/events/{quote(event_id)}",
            {"role": role} if role else None,
        )


def parse_events(raw):
    """Parse the repository's <events isAccepting=..><event id=.. enabled=../></events>."""
    text = raw.decode() if isinstance(raw, bytes) else raw
    if text.lstrip().startswith('"'):
        # Tolerate the XML document arriving wrapped in a JSON string.
        text = json.loads(text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise DCRUnavailable(f"unreadable DCR events response: {error}") from error
    if root.tag != "events":
        raise DCRUnavailable(f"unexpected DCR events response root: {root.tag}")
    flag = lambda e, name: e.get(name, "false").lower() == "true"  # noqa: E731
    return dict(
        isAccepting=root.get("isAccepting", "false").lower() == "true",
        events=[
            dict(
                id=e.get("id"),
                label=e.get("label") or e.get("id"),
                enabled=flag(e, "enabled"),
                pending=flag(e, "pending"),
                included=flag(e, "included"),
                executed=flag(e, "executed"),
                roles=e.get("roles", ""),
            )
            for e in root.findall("event")
        ],
    )


RELATIONS = ("conditions", "responses", "includes", "excludes", "milestones")


class LocalDCR:
    """Execute the P4 graph locally when the DCR repository is not configured.

    Semantics match compliance/check.py. Each case stores its marking, so the
    investigation survives a service restart.
    """

    mode = "local-dcr-engine"
    graph_id = None

    def __init__(self, model_path, db_path):
        root = ET.parse(model_path).getroot()
        self.title = root.get("title", "")
        self.labels = {}
        for event in root.findall("./specification/resources/events/event"):
            self.labels[event.get("id")] = event.get("id")
        for label in root.findall("./specification/resources/labelMappings/labelMapping"):
            self.labels[label.get("eventId")] = label.get("labelId")
        self.relations = {
            plural: [(r.get("sourceId"), r.get("targetId"))
                     for r in root.findall(f"./specification/constraints/{plural}/*")]
            for plural in RELATIONS
        }
        marking = lambda name: sorted(  # noqa: E731
            e.get("id") for e in root.findall(f"./runtime/marking/{name}/event"))
        self.initial = dict(executed=marking("executed"), included=marking("included"),
                            pending=marking("pendingResponses"))
        self.db_path = str(db_path)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS dcr_local_cases "
                       "(id TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def configured(self):
        return True

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.isolation_level = None
        return _Tx(db)

    def _read(self, db, sim_id):
        row = db.execute("SELECT data FROM dcr_local_cases WHERE id=?", (sim_id,)).fetchone()
        if row is None:
            raise DCRUnavailable(f"unknown local DCR case {sim_id}")
        return json.loads(row[0])

    def blocked_reason(self, marking, event):
        included, executed, pending = (set(marking[k]) for k in ("included", "executed", "pending"))
        if event not in self.labels:
            return "unknown activity"
        if event not in included:
            return "activity is excluded"
        for source, target in self.relations["conditions"]:
            if target == event and source in included and source not in executed:
                return f"condition not met: {source}"
        for source, target in self.relations["milestones"]:
            if target == event and source in included and source in pending:
                return f"pending milestone: {source}"
        return None

    def fire(self, marking, event):
        included, executed, pending = (set(marking[k]) for k in ("included", "executed", "pending"))
        executed.add(event)
        pending.discard(event)
        pending.update(b for a, b in self.relations["responses"] if a == event)
        included.difference_update(b for a, b in self.relations["excludes"] if a == event)
        included.update(b for a, b in self.relations["includes"] if a == event)
        return dict(included=sorted(included), executed=sorted(executed), pending=sorted(pending))

    def create_simulation(self, title=None):
        sim_id = f"local-{uuid.uuid4().hex[:12]}"
        with self._db() as db:
            db.execute("INSERT INTO dcr_local_cases VALUES (?,?)",
                       (sim_id, json.dumps(dict(self.initial, title=title, trace=[]))))
        return sim_id

    def events(self, sim_id, filter_="enabled-or-pending"):
        with self._db() as db:
            case = self._read(db, sim_id)
        included, pending = set(case["included"]), set(case["pending"])
        events = [
            dict(id=e, label=self.labels[e], enabled=self.blocked_reason(case, e) is None,
                 pending=e in pending, included=e in included,
                 executed=e in case["executed"], roles="")
            for e in self.labels
        ]
        if filter_ == "enabled-or-pending":
            events = [e for e in events if e["enabled"] or (e["pending"] and e["included"])]
        return dict(isAccepting=not (pending & included), events=events)

    def execute(self, sim_id, event_id, role=None):
        with self._db() as db:
            case = self._read(db, sim_id)
            reason = self.blocked_reason(case, event_id)
            if reason:
                raise DCRUnavailable(f"local DCR refused {event_id}: {reason}")
            case.update(self.fire(case, event_id), trace=case["trace"] + [event_id])
            db.execute("UPDATE dcr_local_cases SET data=? WHERE id=?",
                       (json.dumps(case), sim_id))


class _Tx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, kind, *_):
        self.db.execute("ROLLBACK" if kind else "COMMIT")
        self.db.close()


def default_model_path():
    configured = os.getenv("DCR_MODEL_FILE")
    if configured:
        return configured
    return str(Path(__file__).resolve().parents[1] / "dcr" / "p4_quality_investigation.xml")


def make_dcr(registry_path):
    """Use the DCR repository when credentials are set, otherwise the local engine."""
    client = DCRClient()
    mode = os.getenv("DCR_MODE", "auto")
    if mode == "repository" or (mode == "auto" and client.configured()):
        return client
    return LocalDCR(default_model_path(), registry_path)
