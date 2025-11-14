"""Shim exports for legacy router module."""

from router import RouteCandidate, Router, RoutingContext, RoutingStrategy

__all__ = [
    "RouteCandidate",
    "Router",
    "RoutingContext",
    "RoutingStrategy",
]