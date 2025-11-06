"""
Simple test to verify replay functionality works
"""

import sys
import os

# Add the unison-common source to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'unison-common', 'src'))

try:
    from unison_common.replay_store import ReplayConfig, ReplayManager, StoredEnvelope
    from unison_common.replay_endpoints import store_processing_envelope
    
    print("✅ Successfully imported replay modules")
    
    # Test basic functionality
    config = ReplayConfig()
    print(f"✅ ReplayConfig created: Retention={config.default_retention_days} days, Max envelopes per trace={config.max_envelopes_per_trace}")
    
    manager = ReplayManager(config)
    print("✅ ReplayManager created")
    
    # Test storing an envelope
    envelope_data = {
        "message": "hello world",
        "source": "test",
        "user": "test-user",
        "timestamp": "2025-01-15T10:30:00Z"
    }
    
    result = manager.store_event_envelope(
        envelope_data=envelope_data,
        trace_id="test-trace-123",
        correlation_id="test-correlation-456",
        event_type="ingest_request",
        source="orchestrator",
        user_id="test-user",
        processing_time_ms=150.5,
        status_code=200
    )
    
    assert result == True
    print("✅ Successfully stored event envelope")
    
    # Test retrieving envelopes
    envelopes = manager.store.get_envelopes_by_trace("test-trace-123")
    assert len(envelopes) == 1
    assert envelopes[0].trace_id == "test-trace-123"
    assert envelopes[0].event_type == "ingest_request"
    assert envelopes[0].processing_time_ms == 150.5
    print("✅ Successfully retrieved stored envelope")
    
    # Test trace summary
    summary = manager.get_trace_summary("test-trace-123")
    assert summary["found"] == True
    assert summary["total_envelopes"] == 1
    assert summary["user_id"] == "test-user"
    print("✅ Successfully generated trace summary")
    
    # Test replay functionality
    session = manager.replay_trace("test-trace-123", "test-user")
    assert session.trace_id == "test-trace-123"
    assert session.created_by == "test-user"
    assert session.status == "completed"
    assert session.total_envelopes == 1
    assert session.replayed_envelopes == 1
    print("✅ Successfully replayed trace")
    
    # Test replay history
    history = manager.get_replay_history("test-trace-123")
    assert len(history) == 1
    assert history[0]["event_type"] == "ingest_request"
    assert history[0]["envelope_data"]["message"] == "hello world"
    print("✅ Successfully retrieved replay history")
    
    # Test statistics
    stats = manager.get_statistics()
    assert stats["replay_system"]["total_envelopes"] == 1
    assert stats["replay_system"]["total_traces"] == 1
    assert stats["replay_system"]["store_type"] == "MemoryReplayStore"
    print("✅ Successfully retrieved replay statistics")
    
    # Test utility function
    result = store_processing_envelope(
        envelope_data={"test": "data"},
        trace_id="utility-test-trace",
        correlation_id="utility-test-correlation",
        event_type="test_event",
        source="test_service",
        user_id="test-user"
    )
    
    assert result == True
    print("✅ Successfully used store_processing_envelope utility")
    
    # Test correlation ID lookup
    correlation_envelopes = manager.store.get_envelopes_by_correlation("test-correlation-456")
    assert len(correlation_envelopes) == 1
    print("✅ Successfully retrieved envelopes by correlation ID")
    
    print("\n🎉 All basic replay functionality tests passed!")
    
except ImportError as e:
    print(f"❌ Import error: {e}")
    print(f"Python path: {sys.path[:3]}")  # Show first few paths
    sys.exit(1)
except Exception as e:
    print(f"❌ Error: {e}")
    sys.exit(1)
