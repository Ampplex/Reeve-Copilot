"""Strict typed schemas for GSW-style semantic extraction."""

from typing import TypedDict


class EntityRole(TypedDict):
    entity: str
    role: str


class RelationItem(TypedDict):
    subject: str
    relation: str
    object: str


class ActionItem(TypedDict):
    actor: str
    verb: str
    object: str | None


class StateItem(TypedDict):
    entity: str
    attribute: str
    value: str


class SemanticExtraction(TypedDict):
    # False when the record asks for something rather than stating something.
    # A question asserts no fact, so it is not a memory — and stored as one it
    # is worse than useless, because a question about X embeds almost exactly
    # like a later query about X and outranks the fact that answers it.
    asserts_fact: bool
    summary: str
    emotion: str
    importance: float
    entities: list[str]
    roles: list[EntityRole]
    relations: list[RelationItem]
    actions: list[ActionItem]
    states: list[StateItem]
    location: str | None
    time: str | None
