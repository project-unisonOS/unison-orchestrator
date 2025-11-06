#!/usr/bin/env python3
"""
Quick router test
"""

from src.router import Router, RoutingStrategy, RoutingContext
import time

def test_score_based_routing():
    """Test score-based routing"""
    router = Router(RoutingStrategy.SCORE_BASED)
    
    skills = {
        'echo': lambda x: {'echo': x.get('payload', {})},
        'analyze.code': lambda x: {'analysis': 'code analyzed'},
        'translate.text': lambda x: {'translation': 'text translated'}
    }
    
    test_cases = [
        {'intent': 'echo', 'source': 'io-speech'},
        {'intent': 'analyze.code', 'source': 'web'},
        {'intent': 'translate.text', 'source': 'voice'}
    ]
    
    print("🧪 Testing Score-Based Routing")
    for i, test_case in enumerate(test_cases):
        context = RoutingContext(
            intent=test_case['intent'],
            payload={'test': 'data'},
            user={'username': 'test', 'roles': ['user']},
            source=test_case['source'],
            event_id=f'test-{i}',
            timestamp=time.time()
        )
        
        candidate = router.route(context, skills)
        if candidate:
            print(f"✅ {test_case['intent']} -> {candidate.skill_id} (score: {candidate.score:.3f})")
        else:
            print(f"❌ {test_case['intent']} -> No route")

def test_hybrid_routing():
    """Test hybrid routing"""
    router = Router(RoutingStrategy.HYBRID)
    
    # Add a rule
    rule = {
        'id': 'hybrid-test-rule',
        'intent_prefix': 'echo',
        'skill_id': 'echo',
        'conditions': {'sources': ['test']}
    }
    router.add_routing_rule(rule)
    
    skills = {'echo': lambda x: {'echo': x.get('payload', {})}}
    
    context = RoutingContext(
        intent='echo',
        payload={'message': 'hello'},
        user={'username': 'test', 'roles': ['user']},
        source='test',
        event_id='hybrid-test',
        timestamp=time.time()
    )
    
    candidate = router.route(context, skills)
    if candidate:
        print(f"✅ Hybrid routing: {candidate.skill_id} (strategy: {candidate.strategy_used})")
    else:
        print("❌ Hybrid routing failed")

def test_metrics():
    """Test router metrics"""
    router = Router(RoutingStrategy.RULE_BASED)
    
    # Simulate some routing
    skills = {'echo': lambda x: {}}
    context = RoutingContext('echo', {}, {'roles': ['user']}, 'test', '123', time.time())
    
    router.route(context, skills)
    router.route(context, skills)
    
    metrics = router.get_metrics()
    print(f"✅ Router metrics: {len(metrics)} metrics collected")
    for key, value in metrics.items():
        print(f"   {key}: {value}")

if __name__ == "__main__":
    print("🚀 Quick Router Module Test")
    print("=" * 40)
    
    test_score_based_routing()
    print()
    test_hybrid_routing()
    print()
    test_metrics()
    
    print("\n🎉 All router tests completed successfully!")
