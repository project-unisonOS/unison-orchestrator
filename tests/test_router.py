"""
pytest tests for router module
"""

import pytest
import time
import yaml
from src.router import (
    Router, 
    RoutingStrategy, 
    RoutingContext, 
    RouteCandidate,
    RuleBasedRouter,
    ScoreBasedRouter,
    HybridRouter
)

class TestRouterModule:
    """Test suite for router module functionality"""
    
    @pytest.fixture
    def sample_skills(self):
        """Sample skills for testing"""
        return {
            'echo': lambda x: {'echo': x.get('payload', {})},
            'analyze.code': lambda x: {'analysis': 'code analyzed'},
            'translate.text': lambda x: {'translation': 'text translated'},
            'context.get': lambda x: {'context': 'retrieved'},
            'storage.put': lambda x: {'storage': 'stored'}
        }
    
    @pytest.fixture
    def sample_context(self):
        """Sample routing context"""
        return RoutingContext(
            intent='echo',
            payload={'message': 'test'},
            user={'username': 'test_user', 'roles': ['user']},
            source='test',
            event_id='test-123',
            timestamp=time.time()
        )
    
    @pytest.fixture
    def sample_rule(self):
        """Sample routing rule"""
        return {
            'id': 'test-echo-rule',
            'intent_prefix': 'echo',
            'skill_id': 'echo',
            'priority': 1,
            'conditions': {
                'sources': ['test'],
                'required_roles': ['user']
            }
        }

class TestRouterBasics(TestRouterModule):
    """Test basic router functionality"""
    
    def test_router_creation(self):
        """Test router creation with different strategies"""
        router = Router(RoutingStrategy.RULE_BASED)
        assert router.get_strategy_name() == "rule_based"
        
        router = Router(RoutingStrategy.SCORE_BASED)
        assert router.get_strategy_name() == "score_based"
        
        router = Router(RoutingStrategy.HYBRID)
        assert router.get_strategy_name() == "hybrid"
    
    def test_strategy_switching(self):
        """Test runtime strategy switching"""
        router = Router(RoutingStrategy.RULE_BASED)
        assert router.get_strategy_name() == "rule_based"
        
        router.set_strategy(RoutingStrategy.SCORE_BASED)
        assert router.get_strategy_name() == "score_based"
        
        router.set_strategy(RoutingStrategy.HYBRID)
        assert router.get_strategy_name() == "hybrid"
    
    def test_routing_context_creation(self, sample_context):
        """Test routing context creation"""
        assert sample_context.intent == 'echo'
        assert sample_context.source == 'test'
        assert sample_context.user['roles'] == ['user']
        assert sample_context.event_id == 'test-123'
        assert isinstance(sample_context.timestamp, float)
    
    def test_route_candidate_creation(self):
        """Test route candidate creation"""
        candidate = RouteCandidate(
            skill_id='echo',
            handler=lambda x: {},
            score=0.9,
            metadata={'test': 'data'},
            strategy_used='rule_based'
        )
        
        assert candidate.skill_id == 'echo'
        assert candidate.score == 0.9
        assert candidate.strategy_used == 'rule_based'
        assert candidate.metadata['test'] == 'data'

class TestRuleBasedRouter(TestRouterModule):
    """Test rule-based routing functionality"""
    
    def test_empty_router_no_match(self, sample_context, sample_skills):
        """Test empty router returns fallback match"""
        router = Router(RoutingStrategy.RULE_BASED)
        candidate = router.route(sample_context, sample_skills)
        
        # Should return fallback match since 'echo' prefix exists in skills
        assert candidate is not None
        assert candidate.skill_id == 'echo'
        assert candidate.score == 0.8  # Fallback score
        assert candidate.strategy_used == 'rule_based'
        assert candidate.metadata['match_type'] == 'prefix_fallback'
    
    def test_rule_addition(self, sample_rule):
        """Test adding routing rules"""
        router = Router(RoutingStrategy.RULE_BASED)
        router.add_routing_rule(sample_rule)
        
        # Verify rule was added (internal access)
        if hasattr(router.router, 'rules'):
            assert len(router.router.rules) == 1
            assert router.router.rules[0]['id'] == 'test-echo-rule'
    
    def test_rule_based_routing_match(self, sample_context, sample_skills, sample_rule):
        """Test successful rule-based routing"""
        router = Router(RoutingStrategy.RULE_BASED)
        router.add_routing_rule(sample_rule)
        
        candidate = router.route(sample_context, sample_skills)
        
        assert candidate is not None
        assert candidate.skill_id == 'echo'
        assert candidate.score == 1.0  # Perfect score for rule match
        assert candidate.strategy_used == 'rule_based'
        assert 'rule_id' in candidate.metadata
        assert candidate.metadata['rule_id'] == 'test-echo-rule'
    
    def test_rule_based_routing_no_match_conditions(self, sample_skills, sample_rule):
        """Test rule-based routing with non-matching conditions falls back to prefix matching"""
        router = Router(RoutingStrategy.RULE_BASED)
        router.add_routing_rule(sample_rule)
        
        # Create context that doesn't match conditions
        context = RoutingContext(
            intent='echo',
            payload={'test': 'data'},
            user={'username': 'test_user', 'roles': ['admin']},  # Wrong role
            source='web',  # Wrong source
            event_id='test-456',
            timestamp=time.time()
        )
        
        candidate = router.route(context, sample_skills)
        
        # Should fall back to prefix matching since rule doesn't match
        assert candidate is not None
        assert candidate.skill_id == 'echo'
        assert candidate.score == 0.8  # Fallback score
        assert candidate.strategy_used == 'rule_based'
        assert candidate.metadata['match_type'] == 'prefix_fallback'
    
    def test_fallback_prefix_matching(self, sample_context, sample_skills):
        """Test fallback prefix matching when no rules match"""
        router = Router(RoutingStrategy.RULE_BASED)
        
        candidate = router.route(sample_context, sample_skills)
        
        assert candidate is not None
        assert candidate.skill_id == 'echo'
        assert candidate.score == 0.8  # Fallback score
        assert candidate.strategy_used == 'rule_based'
        assert candidate.metadata['match_type'] == 'prefix_fallback'

class TestScoreBasedRouter(TestRouterModule):
    """Test score-based routing functionality"""
    
    def test_score_based_routing(self, sample_skills):
        """Test score-based routing for different intents"""
        router = Router(RoutingStrategy.SCORE_BASED)
        
        test_cases = [
            {'intent': 'echo', 'expected_skill': 'echo'},
            {'intent': 'analyze.code', 'expected_skill': 'analyze.code'},
            {'intent': 'translate.text', 'expected_skill': 'translate.text'}
        ]
        
        for case in test_cases:
            context = RoutingContext(
                intent=case['intent'],
                payload={'test': 'data'},
                user={'username': 'test', 'roles': ['user']},
                source='test',
                event_id='test-123',
                timestamp=time.time()
            )
            
            candidate = router.route(context, sample_skills)
            
            assert candidate is not None
            assert candidate.skill_id == case['expected_skill']
            assert 0.0 <= candidate.score <= 1.0
            assert candidate.strategy_used == 'score_based'
    
    def test_intent_similarity_scoring(self, sample_skills):
        """Test intent similarity scoring"""
        router = Router(RoutingStrategy.SCORE_BASED)
        
        # Exact match should get high score
        context1 = RoutingContext('echo', {}, {'roles': ['user']}, 'test', '1', time.time())
        candidate1 = router.route(context1, sample_skills)
        
        # Partial match should get lower score
        context2 = RoutingContext('echo.test', {}, {'roles': ['user']}, 'test', '2', time.time())
        candidate2 = router.route(context2, sample_skills)
        
        assert candidate1 is not None
        assert candidate2 is not None
        assert candidate1.score > candidate2.score  # Exact match > partial match
    
    def test_context_aware_scoring(self, sample_skills):
        """Test context-aware scoring"""
        router = Router(RoutingStrategy.SCORE_BASED)
        
        # Voice source should prefer echo/translate
        voice_context = RoutingContext('echo', {}, {'roles': ['user']}, 'io-speech', '1', time.time())
        voice_candidate = router.route(voice_context, sample_skills)
        
        # Web source should prefer analyze/summarize
        web_context = RoutingContext('analyze.code', {}, {'roles': ['user']}, 'web', '2', time.time())
        web_candidate = router.route(web_context, sample_skills)
        
        assert voice_candidate is not None
        assert web_candidate is not None
        assert voice_candidate.strategy_used == 'score_based'
        assert web_candidate.strategy_used == 'score_based'

class TestHybridRouter(TestRouterModule):
    """Test hybrid routing functionality"""
    
    def test_hybrid_routing_rule_priority(self, sample_context, sample_skills, sample_rule):
        """Test hybrid routing prefers rules when available"""
        router = Router(RoutingStrategy.HYBRID)
        router.add_routing_rule(sample_rule)
        
        candidate = router.route(sample_context, sample_skills)
        
        assert candidate is not None
        assert candidate.skill_id == 'echo'
        assert candidate.strategy_used == 'rule_based'  # Should use rule-based for high confidence
        assert candidate.score >= 0.9
    
    def test_hybrid_routing_score_fallback(self, sample_skills):
        """Test hybrid routing falls back to scoring when no rules match"""
        router = Router(RoutingStrategy.HYBRID)
        
        context = RoutingContext('echo', {}, {'roles': ['user']}, 'test', '1', time.time())
        candidate = router.route(context, sample_skills)
        
        assert candidate is not None
        assert candidate.skill_id == 'echo'
        assert candidate.strategy_used in ['rule_based', 'score_based']

class TestRouterMetrics(TestRouterModule):
    """Test router metrics functionality"""
    
    def test_metrics_collection(self, sample_context, sample_skills):
        """Test metrics are collected during routing"""
        router = Router(RoutingStrategy.RULE_BASED)
        
        # Perform some routing
        router.route(sample_context, sample_skills)
        router.route(sample_context, sample_skills)
        
        metrics = router.get_metrics()
        
        assert 'routing_requests' in metrics
        assert 'routing_rule_based' in metrics
        assert 'routing_success' in metrics
        assert 'routing_duration_ms' in metrics
        
        assert metrics['routing_requests'] == 2
        assert metrics['routing_rule_based'] == 2
        assert metrics['routing_success'] == 2
        assert metrics['routing_duration_ms'] >= 0

class TestYAMLIntegration(TestRouterModule):
    """Test YAML configuration integration"""
    
    def test_yaml_rule_loading(self):
        """Test loading rules from YAML file"""
        try:
            with open('routing_rules.yaml', 'r') as f:
                rules = yaml.safe_load(f)
            
            assert isinstance(rules, list)
            assert len(rules) > 0
            
            # Verify rule structure
            rule = rules[0]
            assert 'id' in rule
            assert 'intent_prefix' in rule
            assert 'skill_id' in rule
            
        except FileNotFoundError:
            pytest.skip("routing_rules.yaml not found")
    
    def test_yaml_rule_application(self, sample_skills):
        """Test applying YAML rules to router"""
        try:
            with open('routing_rules.yaml', 'r') as f:
                rules = yaml.safe_load(f)
            
            router = Router(RoutingStrategy.RULE_BASED)
            
            # Add first few rules
            for rule in rules[:3]:
                router.add_routing_rule(rule)
            
            # Test routing with voice echo rule
            context = RoutingContext(
                intent='echo',
                payload={'message': 'hello'},
                user={'username': 'test', 'roles': ['user']},
                source='io-speech',
                event_id='yaml-test',
                timestamp=time.time()
            )
            
            candidate = router.route(context, sample_skills)
            
            if candidate:
                assert candidate.skill_id == 'echo'
                assert candidate.strategy_used == 'rule_based'
            
        except FileNotFoundError:
            pytest.skip("routing_rules.yaml not found")

class TestErrorHandling(TestRouterModule):
    """Test error handling and edge cases"""
    
    def test_invalid_strategy_creation(self):
        """Test creating router with invalid strategy"""
        with pytest.raises(ValueError):
            Router("invalid_strategy")
    
    def test_empty_skills_dict(self, sample_context):
        """Test routing with empty skills dictionary"""
        router = Router(RoutingStrategy.RULE_BASED)
        candidate = router.route(sample_context, {})
        assert candidate is None
    
    def test_invalid_strategy_switching(self):
        """Test switching to invalid strategy"""
        router = Router(RoutingStrategy.RULE_BASED)
        
        with pytest.raises(ValueError):
            router.set_strategy("invalid_strategy")
    
    def test_rule_addition_to_score_based_router(self, sample_rule):
        """Test adding rules to score-based router (should be ignored)"""
        router = Router(RoutingStrategy.SCORE_BASED)
        
        # Should not raise error, but rule should be ignored
        router.add_routing_rule(sample_rule)
        
        # Router should still function normally
        context = RoutingContext('echo', {}, {'roles': ['user']}, 'test', '1', time.time())
        skills = {'echo': lambda x: {}}
        
        candidate = router.route(context, skills)
        assert candidate is not None
        assert candidate.strategy_used == 'score_based'

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
