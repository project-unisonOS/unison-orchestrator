"""
Router Module for Unison Orchestrator

Provides pluggable routing strategies for skill/intent routing.
Supports rule-based routing and score-based routing with configurable strategies.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Tuple, Callable
from dataclasses import dataclass
from enum import Enum
import re
import logging
import time
import uuid
from collections import defaultdict

logger = logging.getLogger(__name__)

class RoutingStrategy(Enum):
    """Available routing strategies"""
    RULE_BASED = "rule_based"
    SCORE_BASED = "score_based"
    HYBRID = "hybrid"

@dataclass
class RouteCandidate:
    """Represents a potential routing candidate"""
    skill_id: str
    handler: Callable
    score: float
    metadata: Dict[str, Any]
    strategy_used: str

@dataclass
class RoutingContext:
    """Context information for routing decisions"""
    intent: str
    payload: Dict[str, Any]
    user: Dict[str, Any]
    source: str
    event_id: str
    timestamp: float

class RoutingStrategyBase(ABC):
    """Base class for routing strategies"""
    
    @abstractmethod
    def route(self, context: RoutingContext, skills: Dict[str, Callable]) -> Optional[RouteCandidate]:
        """
        Route the request to the appropriate skill
        
        Args:
            context: Routing context with request information
            skills: Available skills indexed by intent prefix
            
        Returns:
            RouteCandidate if a match is found, None otherwise
        """
        pass
    
    @abstractmethod
    def get_strategy_name(self) -> str:
        """Get the name of this routing strategy"""
        pass

class RuleBasedRouter(RoutingStrategyBase):
    """Rule-based routing using intent prefix matching and conditions"""
    
    def __init__(self, rules: List[Dict[str, Any]] = None):
        self.rules = rules or []
        self.logger = logging.getLogger(f"{__name__}.RuleBasedRouter")
    
    def add_rule(self, rule: Dict[str, Any]):
        """Add a routing rule"""
        self.rules.append(rule)
        self.logger.info(f"Added routing rule: {rule.get('intent_prefix')}")
    
    def route(self, context: RoutingContext, skills: Dict[str, Callable]) -> Optional[RouteCandidate]:
        """Route based on configured rules"""
        intent = context.intent
        
        for rule in self.rules:
            if self._matches_rule(intent, context, rule):
                skill_id = rule.get('skill_id')
                if skill_id in skills:
                    metadata = {
                        'rule_id': rule.get('id'),
                        'priority': rule.get('priority', 0),
                        'conditions_matched': rule.get('conditions', {})
                    }
                    return RouteCandidate(
                        skill_id=skill_id,
                        handler=skills[skill_id],
                        score=1.0,  # Rule-based matches get perfect score
                        metadata=metadata,
                        strategy_used=self.get_strategy_name()
                    )
        
        # Fallback to direct prefix matching
        for skill_prefix in skills.keys():
            if intent.startswith(skill_prefix):
                return RouteCandidate(
                    skill_id=skill_prefix,
                    handler=skills[skill_prefix],
                    score=0.8,  # Slightly lower score for fallback matches
                    metadata={'match_type': 'prefix_fallback'},
                    strategy_used=self.get_strategy_name()
                )
        
        return None
    
    def _matches_rule(self, intent: str, context: RoutingContext, rule: Dict[str, Any]) -> bool:
        """Check if intent and context match the rule"""
        # Check intent prefix
        intent_prefix = rule.get('intent_prefix')
        if intent_prefix and not intent.startswith(intent_prefix):
            return False
        
        # Check conditions
        conditions = rule.get('conditions', {})
        
        # Check source conditions
        if 'sources' in conditions:
            allowed_sources = conditions['sources']
            if context.source not in allowed_sources:
                return False
        
        # Check user role conditions
        if 'required_roles' in conditions:
            required_roles = conditions['required_roles']
            user_roles = context.user.get('roles', [])
            if not any(role in user_roles for role in required_roles):
                return False
        
        # Check payload conditions
        if 'payload_conditions' in conditions:
            payload_conditions = conditions['payload_conditions']
            for key, expected_value in payload_conditions.items():
                actual_value = context.payload.get(key)
                if actual_value != expected_value:
                    return False
        
        # Check time windows
        if 'time_window' in conditions:
            time_window = conditions['time_window']
            current_hour = time.gmtime(context.timestamp).tm_hour
            start_hour = time_window.get('start', 0)
            end_hour = time_window.get('end', 23)
            if not (start_hour <= current_hour <= end_hour):
                return False
        
        return True
    
    def get_strategy_name(self) -> str:
        return "rule_based"

class ScoreBasedRouter(RoutingStrategyBase):
    """Score-based routing using intent similarity and context scoring"""
    
    def __init__(self, weights: Dict[str, float] = None):
        self.weights = weights or {
            'intent_similarity': 0.5,
            'context_match': 0.3,
            'user_preference': 0.2
        }
        self.logger = logging.getLogger(f"{__name__}.ScoreBasedRouter")
    
    def route(self, context: RoutingContext, skills: Dict[str, Callable]) -> Optional[RouteCandidate]:
        """Route based on scoring algorithm"""
        candidates = []
        
        for skill_id in skills.keys():
            score = self._calculate_score(context, skill_id)
            if score > 0.1:  # Minimum threshold
                candidates.append(RouteCandidate(
                    skill_id=skill_id,
                    handler=skills[skill_id],
                    score=score,
                    metadata={'score_breakdown': self._get_score_breakdown(context, skill_id)},
                    strategy_used=self.get_strategy_name()
                ))
        
        # Return the highest-scoring candidate
        if candidates:
            candidates.sort(key=lambda x: x.score, reverse=True)
            best = candidates[0]
            self.logger.info(f"Score-based routing selected {best.skill_id} with score {best.score:.3f}")
            return best
        
        return None
    
    def _calculate_score(self, context: RoutingContext, skill_id: str) -> float:
        """Calculate routing score for a skill"""
        intent = context.intent
        
        # Intent similarity score
        similarity_score = self._intent_similarity(intent, skill_id)
        
        # Context match score
        context_score = self._context_match_score(context, skill_id)
        
        # User preference score (placeholder for future ML-based personalization)
        preference_score = self._user_preference_score(context, skill_id)
        
        # Weighted combination
        total_score = (
            similarity_score * self.weights['intent_similarity'] +
            context_score * self.weights['context_match'] +
            preference_score * self.weights['user_preference']
        )
        
        return min(1.0, total_score)  # Cap at 1.0
    
    def _intent_similarity(self, intent: str, skill_id: str) -> float:
        """Calculate intent-skill similarity score"""
        if intent.startswith(skill_id):
            # Exact prefix match gets high score
            if intent == skill_id:
                return 1.0
            else:
                # Partial prefix match
                overlap_ratio = len(skill_id) / len(intent)
                return 0.7 + (0.3 * overlap_ratio)
        
        # Check for semantic similarity (simple keyword matching)
        intent_words = set(intent.lower().split('.'))
        skill_words = set(skill_id.lower().split('.'))
        
        if intent_words and skill_words:
            intersection = intent_words.intersection(skill_words)
            union = intent_words.union(skill_words)
            jaccard_similarity = len(intersection) / len(union) if union else 0
            return jaccard_similarity * 0.5
        
        return 0.0
    
    def _context_match_score(self, context: RoutingContext, skill_id: str) -> float:
        """Calculate context-based scoring"""
        score = 0.0
        
        # Source-specific scoring
        source_mappings = {
            'io-speech': ['echo', 'translate'],
            'io-vision': ['analyze', 'generate'],
            'web': ['summarize', 'analyze']
        }
        
        for source, preferred_skills in source_mappings.items():
            if context.source == source:
                for preferred in preferred_skills:
                    if preferred in skill_id:
                        score += 0.3
                        break
        
        # Payload-based scoring
        payload = context.payload
        if 'prompt' in payload and 'inference' in skill_id:
            score += 0.2
        elif 'key' in payload and 'storage' in skill_id:
            score += 0.2
        elif 'keys' in payload and 'context' in skill_id:
            score += 0.2
        
        return min(1.0, score)
    
    def _user_preference_score(self, context: RoutingContext, skill_id: str) -> float:
        """Calculate user preference score (placeholder for future ML)"""
        # Simple heuristic based on user roles
        user_roles = context.user.get('roles', [])
        
        if 'admin' in user_roles:
            return 0.1  # Slight preference for admin-specific skills
        elif 'developer' in user_roles:
            if 'analyze' in skill_id or 'generate' in skill_id:
                return 0.1
        elif 'user' in user_roles:
            if 'echo' in skill_id or 'translate' in skill_id:
                return 0.1
        
        return 0.0
    
    def _get_score_breakdown(self, context: RoutingContext, skill_id: str) -> Dict[str, float]:
        """Get detailed score breakdown for debugging"""
        return {
            'intent_similarity': self._intent_similarity(context.intent, skill_id),
            'context_match': self._context_match_score(context, skill_id),
            'user_preference': self._user_preference_score(context, skill_id)
        }
    
    def get_strategy_name(self) -> str:
        return "score_based"

class HybridRouter(RoutingStrategyBase):
    """Hybrid router combining rule-based and score-based strategies"""
    
    def __init__(self, rule_router: RuleBasedRouter = None, score_router: ScoreBasedRouter = None):
        self.rule_router = rule_router or RuleBasedRouter()
        self.score_router = score_router or ScoreBasedRouter()
        self.logger = logging.getLogger(f"{__name__}.HybridRouter")
    
    def route(self, context: RoutingContext, skills: Dict[str, Callable]) -> Optional[RouteCandidate]:
        """Route using hybrid strategy"""
        # Try rule-based first
        rule_candidate = self.rule_router.route(context, skills)
        
        if rule_candidate and rule_candidate.score >= 0.9:
            # High-confidence rule match, use it
            return rule_candidate
        
        # Get score-based candidate
        score_candidate = self.score_router.route(context, skills)
        
        if rule_candidate and score_candidate:
            # Compare candidates
            if rule_candidate.score >= score_candidate.score:
                return rule_candidate
            else:
                return score_candidate
        elif rule_candidate:
            return rule_candidate
        elif score_candidate:
            return score_candidate
        
        return None
    
    def get_strategy_name(self) -> str:
        return "hybrid"

class Router:
    """Main router class that manages routing strategies"""
    
    def __init__(self, strategy: RoutingStrategy = RoutingStrategy.RULE_BASED):
        self.strategy = strategy
        self.router = self._create_router(strategy)
        self.metrics = defaultdict(int)
        self.logger = logging.getLogger(__name__)
    
    def _create_router(self, strategy: RoutingStrategy) -> RoutingStrategyBase:
        """Create router instance based on strategy"""
        if strategy == RoutingStrategy.RULE_BASED:
            return RuleBasedRouter()
        elif strategy == RoutingStrategy.SCORE_BASED:
            return ScoreBasedRouter()
        elif strategy == RoutingStrategy.HYBRID:
            return HybridRouter()
        else:
            raise ValueError(f"Unknown routing strategy: {strategy}")
    
    def route(self, context: RoutingContext, skills: Dict[str, Callable]) -> Optional[RouteCandidate]:
        """Route request using configured strategy"""
        start_time = time.time()
        
        try:
            candidate = self.router.route(context, skills)
            
            # Update metrics
            self.metrics['routing_requests'] += 1
            self.metrics[f'routing_{self.router.get_strategy_name()}'] += 1
            
            if candidate:
                self.metrics['routing_success'] += 1
                self.logger.info(f"Routed {context.intent} to {candidate.skill_id} via {candidate.strategy_used}")
            else:
                self.metrics['routing_no_match'] += 1
                self.logger.warning(f"No routing match found for {context.intent}")
            
            return candidate
            
        except Exception as e:
            self.metrics['routing_errors'] += 1
            self.logger.error(f"Routing error for {context.intent}: {e}")
            return None
        finally:
            self.metrics['routing_duration_ms'] += (time.time() - start_time) * 1000
    
    def add_routing_rule(self, rule: Dict[str, Any]):
        """Add a routing rule (only works with rule-based or hybrid strategies)"""
        if isinstance(self.router, (RuleBasedRouter, HybridRouter)):
            if isinstance(self.router, HybridRouter):
                self.router.rule_router.add_rule(rule)
            else:
                self.router.add_rule(rule)
        else:
            self.logger.warning("Cannot add rules to score-based router")
    
    def set_strategy(self, strategy: RoutingStrategy):
        """Change routing strategy"""
        self.strategy = strategy
        self.router = self._create_router(strategy)
        self.logger.info(f"Switched to {strategy.value} routing strategy")
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get routing metrics"""
        return dict(self.metrics)
    
    def get_strategy_name(self) -> str:
        """Get current strategy name"""
        return self.router.get_strategy_name()

# Default router instance
default_router = Router(RoutingStrategy.RULE_BASED)
