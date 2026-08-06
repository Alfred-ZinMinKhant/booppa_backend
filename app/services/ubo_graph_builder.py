"""
ubo_graph_builder.py — Component 6.3 (UBO relationship graph builder).

Builds a directed graph of relationships between vendors, directors, shareholders,
and beneficial owners using networkx.
"""

from dataclasses import dataclass
from typing import List, Optional

try:
    import networkx as nx
except ImportError:
    nx = None


@dataclass
class Entity:
    entity_id: str          # UEN for companies, internal ID for natural persons
    name: str
    entity_type: str        # "company" | "person"


@dataclass
class Relationship:
    source_id: str
    target_id: str
    relationship_type: str   # "director" | "shareholder" | "nominee_director" | "beneficial_owner" | "nominator"
    percentage: Optional[float] = None


def build_graph(entities: List[Entity], relationships: List[Relationship]):
    """
    Builds a directed graph: an edge from person/company to target company.
    """
    if nx is None:
        raise RuntimeError("networkx package is required for ubo_graph_builder")
    g = nx.DiGraph()

    for e in entities:
        g.add_node(e.entity_id, name=e.name, entity_type=e.entity_type)

    for r in relationships:
        g.add_edge(
            r.source_id,
            r.target_id,
            relationship_type=r.relationship_type,
            percentage=r.percentage,
        )

    return g


def find_shared_control_patterns(g: nx.DiGraph, min_shared_companies: int = 2) -> List[dict]:
    """
    Finds people/entities holding roles across multiple companies at once.
    """
    patterns = []
    for node_id in g.nodes():
        node_data = g.nodes[node_id]
        if node_data.get("entity_type") != "person":
            continue

        out_edges = list(g.out_edges(node_id, data=True))
        if len(out_edges) >= min_shared_companies:
            companies = [
                {
                    "company_id": tgt,
                    "company_name": g.nodes[tgt].get("name", ""),
                    "relationship_type": data.get("relationship_type"),
                }
                for _, tgt, data in out_edges
            ]
            patterns.append({
                "person_id": node_id,
                "person_name": node_data.get("name", ""),
                "company_count": len(out_edges),
                "companies": companies,
            })

    patterns.sort(key=lambda p: p["company_count"], reverse=True)
    return patterns


def graph_to_visualization_json(g: nx.DiGraph) -> dict:
    """
    Exports graph for frontend rendering (d3.js / vis.js).
    """
    nodes = [
        {"id": n, "label": data.get("name", n), "type": data.get("entity_type", "")}
        for n, data in g.nodes(data=True)
    ]
    edges = [
        {
            "from": u,
            "to": v,
            "label": data.get("relationship_type", ""),
            "percentage": data.get("percentage"),
        }
        for u, v, data in g.edges(data=True)
    ]
    return {"nodes": nodes, "edges": edges}


def from_csp_records(
    director_rows: List[dict],
    shareholder_rows: List[dict],
    ubo_rows: List[dict],
    company_rows: List[dict],
) -> nx.DiGraph:
    """
    INTEGRATION POINT — query results from CspNomineeDirector, CspNomineeShareholder,
    and CspBeneficialOwner.
    """
    entities = []
    relationships = []

    for c in company_rows:
        entities.append(Entity(entity_id=c["uen"], name=c["name"], entity_type="company"))

    seen_people = set()

    def _ensure_person(name: str) -> str:
        person_id = f"person::{name.strip().lower()}"
        if person_id not in seen_people:
            entities.append(Entity(entity_id=person_id, name=name, entity_type="person"))
            seen_people.add(person_id)
        return person_id

    for d in director_rows:
        person_id = _ensure_person(d["nominee_full_name"])
        relationships.append(Relationship(
            source_id=person_id, target_id=d["company_uen"],
            relationship_type="nominee_director",
        ))
        nominator_id = _ensure_person(d["nominator_name"])
        relationships.append(Relationship(
            source_id=nominator_id, target_id=d["company_uen"],
            relationship_type="nominator",
        ))

    for s in shareholder_rows:
        person_id = _ensure_person(s["nominee_full_name"])
        relationships.append(Relationship(
            source_id=person_id, target_id=s["company_uen"],
            relationship_type="nominee_shareholder", percentage=s.get("share_percentage"),
        ))

    for u in ubo_rows:
        person_id = _ensure_person(u["ubo_full_name"])
        relationships.append(Relationship(
            source_id=person_id, target_id=u["company_uen"],
            relationship_type="beneficial_owner", percentage=u.get("ownership_percentage"),
        ))

    return build_graph(entities, relationships)
