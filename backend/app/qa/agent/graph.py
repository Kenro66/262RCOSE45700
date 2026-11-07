"""Compile one bounded StateGraph per request, without global mutable state."""

from langgraph.graph import END, START, StateGraph

from .nodes import Nodes
from .state import GraphState


def build_graph(nodes: Nodes):
    builder = StateGraph(GraphState)
    names = ("check_conditions", "clarify", "retrieve", "check_evidence", "rewrite_query",
             "check_applicability", "check_conflict", "generate_answer", "insufficient", "finalize")
    for name in names:
        builder.add_node(name, nodes.tracked(name))

    def conditions_route(state):
        if state["status"] is not None:
            return "finalize"
        if state["missing_conditions"]:
            return "clarify"
        if state["evidence"]:
            return "check_evidence"
        return "retrieve" if state["retrieval_count"] < 3 else "insufficient"

    def evidence_route(state):
        if state["status"] is not None:
            return "finalize"
        if state["sufficient"]:
            return "check_applicability"
        return "rewrite_query" if state["retrieval_count"] < 3 else "insufficient"

    def applicability_route(state):
        if state["status"] is not None:
            return "finalize"
        return "clarify" if state["missing_conditions"] else "check_conflict"

    builder.add_edge(START, "check_conditions")
    builder.add_conditional_edges("check_conditions", conditions_route,
                                  ["finalize", "clarify", "check_evidence", "retrieve", "insufficient"])
    builder.add_edge("clarify", "finalize")
    builder.add_conditional_edges("retrieve", lambda s: "finalize" if s["status"] else "check_evidence",
                                  ["finalize", "check_evidence"])
    builder.add_conditional_edges("check_evidence", evidence_route,
                                  ["finalize", "check_applicability", "rewrite_query", "insufficient"])
    builder.add_conditional_edges("rewrite_query", lambda s: "finalize" if s["status"] else "retrieve",
                                  ["finalize", "retrieve"])
    builder.add_conditional_edges("check_applicability", applicability_route,
                                  ["finalize", "clarify", "check_conflict"])
    builder.add_conditional_edges("check_conflict", lambda s: "finalize" if s["status"] else "generate_answer",
                                  ["finalize", "generate_answer"])
    builder.add_edge("generate_answer", "finalize")
    builder.add_edge("insufficient", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()
