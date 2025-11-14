"""
pytest tests for event replay functionality
"""

import pytest
import time
import uuid
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch, AsyncMock

from unison_common.replay_store import (
    ReplayConfig,
    StoredEnvelope,
    ReplaySession,
    ReplayManager,
    MemoryReplayStore,
    get_replay_manager,
    initialize_replay
)
from unison_common.replay_endpoints import (
    replay_trace_by_id,
    get_replay_history,
    get_trace_summary,
    list_traces,
    delete_trace,
    get_replay_statistics,
    get_replay_session,
    store_processing_envelope
)


class TestReplayConfig:
    """Test replay configuration"""
    
    def test_default_config(self):
        """Test default configuration values"""
        config = ReplayConfig()
        
        assert config.default_retention_days == 30
        assert config.max_envelopes_per_trace == 1000
        assert config.max_session_duration_hours == 24
        assert config.cleanup_interval_hours == 6
        assert config.max_stored_envelopes == 100000
        assert config.compression_enabled == True
        assert config.index_by_correlation_id == True
    
    def test_config_modification(self):
        """Test configuration modification"""
        config = ReplayConfig()
        config.default_retention_days = 7
        config.max_envelopes_per_trace = 500
        
        assert config.default_retention_days == 7
        assert config.max_envelopes_per_trace == 500


class TestStoredEnvelope:
    """Test stored envelope functionality"""
    
    def test_envelope_creation(self):
        """Test creating a stored envelope"""
        now = now_utc()
        
        envelope = StoredEnvelope(
            envelope_id="test-envelope-id",
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            envelope_data={"message": "hello", "source": "test"},
            timestamp=now,
            event_type="test_event",
            source="test_service",
            user_id="test-user",
            processing_time_ms=150.5,
            status_code=200
        )
        
        assert envelope.envelope_id == "test-envelope-id"
        assert envelope.trace_id == "test-trace-id"
        assert envelope.correlation_id == "test-correlation-id"
        assert envelope.event_type == "test_event"
        assert envelope.source == "test_service"
        assert envelope.user_id == "test-user"
        assert envelope.processing_time_ms == 150.5
        assert envelope.status_code == 200
    
    def test_envelope_serialization(self):
        """Test envelope to/from dictionary conversion"""
        now = now_utc()
        
        envelope = StoredEnvelope(
            envelope_id="test-envelope-id",
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            envelope_data={"message": "hello"},
            timestamp=now,
            event_type="test_event",
            source="test_service"
        )
        
        # Convert to dictionary
        data = envelope.to_dict()
        assert data["envelope_id"] == "test-envelope-id"
        assert data["trace_id"] == "test-trace-id"
        assert "timestamp" in data
        
        # Convert back from dictionary
        restored_envelope = StoredEnvelope.from_dict(data)
        assert restored_envelope.envelope_id == envelope.envelope_id
        assert restored_envelope.trace_id == envelope.trace_id
        assert restored_envelope.timestamp == envelope.timestamp


class TestReplaySession:
    """Test replay session functionality"""
    
    def test_session_creation(self):
        """Test creating a replay session"""
        now = now_utc()
        
        session = ReplaySession(
            session_id="test-session-id",
            trace_id="test-trace-id",
            created_at=now,
            created_by="test-user",
            status="created",
            total_envelopes=10,
            replayed_envelopes=0,
            failed_envelopes=0,
            errors=[]
        )
        
        assert session.session_id == "test-session-id"
        assert session.trace_id == "test-trace-id"
        assert session.created_by == "test-user"
        assert session.status == "created"
        assert session.total_envelopes == 10
        assert session.replayed_envelopes == 0
        assert session.failed_envelopes == 0
        assert session.errors == []
    
    def test_session_serialization(self):
        """Test session to/from dictionary conversion"""
        now = now_utc()
        
        session = ReplaySession(
            session_id="test-session-id",
            trace_id="test-trace-id",
            created_at=now,
            created_by="test-user",
            status="completed",
            total_envelopes=10,
            replayed_envelopes=8,
            failed_envelopes=2,
            errors=["Error 1", "Error 2"]
        )
        
        # Convert to dictionary
        data = session.to_dict()
        assert data["session_id"] == "test-session-id"
        assert data["status"] == "completed"
        assert "created_at" in data
        
        # Convert back from dictionary
        restored_session = ReplaySession.from_dict(data)
        assert restored_session.session_id == session.session_id
        assert restored_session.status == session.status
        assert restored_session.created_at == session.created_at


class TestMemoryReplayStore:
    """Test in-memory replay store"""
    
    @pytest.fixture
    def store(self):
        """Create a test store"""
        config = ReplayConfig()
        config.max_envelopes_per_trace = 5  # Small for testing
        return MemoryReplayStore(config)
    
    def test_store_and_retrieve_envelope(self, store):
        """Test storing and retrieving envelopes"""
        now = now_utc()
        
        envelope = StoredEnvelope(
            envelope_id="test-envelope-id",
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            envelope_data={"message": "hello"},
            timestamp=now,
            event_type="test_event",
            source="test_service"
        )
        
        # Store envelope
        result = store.store_envelope(envelope)
        assert result == True
        
        # Retrieve envelopes for trace
        envelopes = store.get_envelopes_by_trace("test-trace-id")
        assert len(envelopes) == 1
        assert envelopes[0].envelope_id == "test-envelope-id"
    
    def test_get_envelopes_by_correlation(self, store):
        """Test getting envelopes by correlation ID"""
        now = now_utc()
        
        # Store envelopes with same correlation ID
        envelope1 = StoredEnvelope(
            envelope_id="envelope-1",
            trace_id="trace-1",
            correlation_id="same-correlation",
            envelope_data={"message": "hello1"},
            timestamp=now,
            event_type="test_event",
            source="test_service"
        )
        
        envelope2 = StoredEnvelope(
            envelope_id="envelope-2",
            trace_id="trace-2",
            correlation_id="same-correlation",
            envelope_data={"message": "hello2"},
            timestamp=now + timedelta(seconds=1),
            event_type="test_event",
            source="test_service"
        )
        
        store.store_envelope(envelope1)
        store.store_envelope(envelope2)
        
        # Retrieve by correlation ID
        envelopes = store.get_envelopes_by_correlation("same-correlation")
        assert len(envelopes) == 2
        assert envelopes[0].envelope_id == "envelope-1"
        assert envelopes[1].envelope_id == "envelope-2"
    
    def test_create_replay_session(self, store):
        """Test creating replay sessions"""
        now = now_utc()
        
        # Store some envelopes
        envelope = StoredEnvelope(
            envelope_id="test-envelope-id",
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            envelope_data={"message": "hello"},
            timestamp=now,
            event_type="test_event",
            source="test_service"
        )
        store.store_envelope(envelope)
        
        # Create replay session
        session = store.create_replay_session("test-trace-id", "test-user")
        
        assert session.trace_id == "test-trace-id"
        assert session.created_by == "test-user"
        assert session.status == "created"
        assert session.total_envelopes == 1
        assert session.replayed_envelopes == 0
        assert session.failed_envelopes == 0
    
    def test_update_replay_session(self, store):
        """Test updating replay sessions"""
        session = store.create_replay_session("test-trace-id", "test-user")
        
        # Update session
        success = store.update_replay_session(
            session.session_id,
            status="completed",
            replayed_envelopes=5,
            failed_envelopes=1
        )
        
        assert success == True
        
        # Verify update
        updated_session = store.get_replay_session(session.session_id)
        assert updated_session.status == "completed"
        assert updated_session.replayed_envelopes == 5
        assert updated_session.failed_envelopes == 1
    
    def test_delete_trace(self, store):
        """Test deleting traces"""
        now = now_utc()
        
        envelope = StoredEnvelope(
            envelope_id="test-envelope-id",
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            envelope_data={"message": "hello"},
            timestamp=now,
            event_type="test_event",
            source="test_service"
        )
        
        store.store_envelope(envelope)
        assert len(store.get_envelopes_by_trace("test-trace-id")) == 1
        
        # Delete trace
        success = store.delete_trace("test-trace-id")
        assert success == True
        
        # Verify deletion
        assert len(store.get_envelopes_by_trace("test-trace-id")) == 0
    
    def test_get_statistics(self, store):
        """Test getting store statistics"""
        # Store some test data
        now = now_utc()
        
        envelope1 = StoredEnvelope(
            envelope_id="envelope-1",
            trace_id="trace-1",
            correlation_id="correlation-1",
            envelope_data={"message": "hello1"},
            timestamp=now,
            event_type="test_event",
            source="test_service"
        )
        
        envelope2 = StoredEnvelope(
            envelope_id="envelope-2",
            trace_id="trace-2",
            correlation_id="correlation-2",
            envelope_data={"message": "hello2"},
            timestamp=now,
            event_type="test_event",
            source="test_service"
        )
        
        store.store_envelope(envelope1)
        store.store_envelope(envelope2)
        store.create_replay_session("trace-1", "test-user")
        
        stats = store.get_statistics()
        
        assert stats["total_envelopes"] == 2
        assert stats["total_traces"] == 2
        assert stats["store_type"] == "MemoryReplayStore"
        assert stats["correlation_index_size"] == 2


class TestReplayManager:
    """Test replay manager"""
    
    @pytest.fixture
    def manager(self):
        """Create a test manager"""
        config = ReplayConfig()
        store = MemoryReplayStore(config)
        return ReplayManager(config, store)
    
    def test_store_event_envelope(self, manager):
        """Test storing event envelopes"""
        envelope_data = {"message": "hello", "source": "test"}
        
        result = manager.store_event_envelope(
            envelope_data=envelope_data,
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            event_type="test_event",
            source="test_service",
            user_id="test-user",
            processing_time_ms=150.5,
            status_code=200
        )
        
        assert result == True
        
        # Verify it was stored
        envelopes = manager.store.get_envelopes_by_trace("test-trace-id")
        assert len(envelopes) == 1
        assert envelopes[0].envelope_data == envelope_data
        assert envelopes[0].event_type == "test_event"
        assert envelopes[0].processing_time_ms == 150.5
    
    def test_replay_trace(self, manager):
        """Test replaying a trace"""
        # Store some test envelopes
        envelope_data = {"message": "hello", "source": "test"}
        manager.store_event_envelope(
            envelope_data=envelope_data,
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            event_type="test_event",
            source="test_service"
        )
        
        # Replay trace
        session = manager.replay_trace("test-trace-id", "test-user")
        
        assert session.trace_id == "test-trace-id"
        assert session.created_by == "test-user"
        assert session.status == "completed"  # Should complete successfully
        assert session.total_envelopes == 1
        assert session.replayed_envelopes == 1
        assert session.failed_envelopes == 0
    
    def test_get_replay_history(self, manager):
        """Test getting replay history"""
        # Store test envelope
        envelope_data = {"message": "hello", "source": "test"}
        manager.store_event_envelope(
            envelope_data=envelope_data,
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            event_type="test_event",
            source="test_service",
            user_id="test-user",
            processing_time_ms=150.5,
            status_code=200
        )
        
        # Get history
        history = manager.get_replay_history("test-trace-id")
        
        assert len(history) == 1
        assert history[0]["envelope_data"] == envelope_data
        assert history[0]["event_type"] == "test_event"
        assert history[0]["user_id"] == "test-user"
        assert history[0]["processing_time_ms"] == 150.5
        assert history[0]["status_code"] == 200
    
    def test_get_trace_summary(self, manager):
        """Test getting trace summary"""
        now = now_utc()
        
        # Store test envelopes
        manager.store_event_envelope(
            envelope_data={"message": "hello1"},
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            event_type="event1",
            source="service1",
            user_id="test-user"
        )
        
        manager.store_event_envelope(
            envelope_data={"message": "hello2"},
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            event_type="event2",
            source="service2",
            user_id="test-user"
        )
        
        # Get summary
        summary = manager.get_trace_summary("test-trace-id")
        
        assert summary["found"] == True
        assert summary["trace_id"] == "test-trace-id"
        assert summary["correlation_id"] == "test-correlation-id"
        assert summary["total_envelopes"] == 2
        assert summary["user_id"] == "test-user"
        assert "event1" in summary["event_types"]
        assert "event2" in summary["event_types"]
        assert "service1" in summary["sources"]
        assert "service2" in summary["sources"]
    
    def test_get_statistics(self, manager):
        """Test getting replay manager statistics"""
        stats = manager.get_statistics()
        
        assert "replay_system" in stats
        assert stats["replay_system"]["store_type"] == "MemoryReplayStore"
        assert "config" in stats
        assert stats["config"]["default_retention_days"] == 30


class TestGlobalManager:
    """Test global replay manager"""
    
    def test_get_global_manager(self):
        """Test getting the global manager"""
        manager = get_replay_manager()
        assert manager is not None
        assert isinstance(manager, ReplayManager)
    
    def test_initialize_global_manager(self):
        """Test initializing the global manager"""
        config = ReplayConfig()
        config.default_retention_days = 7
        
        initialize_replay(config)
        
        manager = get_replay_manager()
        assert manager.config.default_retention_days == 7


class TestReplayEndpoints:
    """Test replay endpoint functions"""
    
    @pytest.fixture
    def mock_user(self):
        """Create a mock user"""
        return {
            "username": "test-user",
            "roles": ["user"]
        }
    
    @pytest.fixture
    def mock_admin_user(self):
        """Create a mock admin user"""
        return {
            "username": "admin-user",
            "roles": ["admin"]
        }
    
    @pytest.mark.asyncio
    async def test_replay_trace_by_id_success(self, mock_user):
        """Test successful trace replay"""
        # Initialize with test data
        config = ReplayConfig()
        store = MemoryReplayStore(config)
        manager = ReplayManager(config, store)
        
        # Store test envelope
        manager.store_event_envelope(
            envelope_data={"message": "test"},
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            event_type="test_event",
            source="test_service",
            user_id="test-user"
        )
        
        with patch('unison_common.replay_endpoints.get_replay_manager', return_value=manager):
            result = await replay_trace_by_id("test-trace-id", mock_user)
            
            assert result["trace_id"] == "test-trace-id"
            assert result["status"] == "completed"
            assert result["total_envelopes"] == 1
            assert result["replayed_envelopes"] == 1
            assert result["failed_envelopes"] == 0
    
    @pytest.mark.asyncio
    async def test_replay_trace_by_id_not_found(self, mock_user):
        """Test replay trace when trace not found"""
        config = ReplayConfig()
        store = MemoryReplayStore(config)
        manager = ReplayManager(config, store)
        
        with patch('unison_common.replay_endpoints.get_replay_manager', return_value=manager):
            with pytest.raises(Exception) as exc_info:
                await replay_trace_by_id("nonexistent-trace", mock_user)
            
            assert "404" in str(exc_info.value) or "not found" in str(exc_info.value).lower()
    
    @pytest.mark.asyncio
    async def test_get_replay_history_success(self, mock_user):
        """Test successful replay history retrieval"""
        config = ReplayConfig()
        store = MemoryReplayStore(config)
        manager = ReplayManager(config, store)
        
        # Store test envelope
        manager.store_event_envelope(
            envelope_data={"message": "test"},
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            event_type="test_event",
            source="test_service",
            user_id="test-user"
        )
        
        with patch('unison_common.replay_endpoints.get_replay_manager', return_value=manager):
            result = await get_replay_history("test-trace-id", mock_user)
            
            assert result["trace_id"] == "test-trace-id"
            assert result["total_envelopes"] == 1
            assert len(result["envelopes"]) == 1
            assert result["envelopes"][0]["event_type"] == "test_event"
    
    @pytest.mark.asyncio
    async def test_get_trace_summary_success(self, mock_user):
        """Test successful trace summary retrieval"""
        config = ReplayConfig()
        store = MemoryReplayStore(config)
        manager = ReplayManager(config, store)
        
        # Store test envelope
        manager.store_event_envelope(
            envelope_data={"message": "test"},
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            event_type="test_event",
            source="test_service",
            user_id="test-user"
        )
        
        with patch('unison_common.replay_endpoints.get_replay_manager', return_value=manager):
            result = await get_trace_summary("test-trace-id", mock_user)
            
            assert result["found"] == True
            assert result["trace_id"] == "test-trace-id"
            assert result["total_envelopes"] == 1
            assert result["user_id"] == "test-user"
    
    @pytest.mark.asyncio
    async def test_list_traces_success(self, mock_user):
        """Test successful trace listing"""
        config = ReplayConfig()
        store = MemoryReplayStore(config)
        manager = ReplayManager(config, store)
        
        # Store test envelopes
        manager.store_event_envelope(
            envelope_data={"message": "test1"},
            trace_id="trace-1",
            correlation_id="correlation-1",
            event_type="test_event",
            source="test_service",
            user_id="test-user"
        )
        
        manager.store_event_envelope(
            envelope_data={"message": "test2"},
            trace_id="trace-2",
            correlation_id="correlation-2",
            event_type="test_event",
            source="test_service",
            user_id="test-user"
        )
        
        with patch('unison_common.replay_endpoints.get_replay_manager', return_value=manager):
            result = await list_traces(10, 0, mock_user)
            
            assert len(result["traces"]) == 2
            assert result["pagination"]["total"] == 2
            assert result["pagination"]["limit"] == 10
            assert result["pagination"]["offset"] == 0
    
    @pytest.mark.asyncio
    async def test_delete_trace_success(self, mock_admin_user):
        """Test successful trace deletion"""
        config = ReplayConfig()
        store = MemoryReplayStore(config)
        manager = ReplayManager(config, store)
        
        # Store test envelope
        manager.store_event_envelope(
            envelope_data={"message": "test"},
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            event_type="test_event",
            source="test_service",
            user_id="test-user"
        )
        
        with patch('unison_common.replay_endpoints.get_replay_manager', return_value=manager):
            result = await delete_trace("test-trace-id", mock_admin_user)
            
            assert result["trace_id"] == "test-trace-id"
            assert result["deleted"] == True
            assert result["deleted_by"] == "admin-user"
    
    @pytest.mark.asyncio
    async def test_get_replay_statistics_success(self, mock_admin_user):
        """Test successful replay statistics retrieval"""
        config = ReplayConfig()
        store = MemoryReplayStore(config)
        manager = ReplayManager(config, store)
        
        with patch('unison_common.replay_endpoints.get_replay_manager', return_value=manager):
            result = await get_replay_statistics(mock_admin_user)
            
            assert "replay_system" in result
            assert result["replay_system"]["store_type"] == "MemoryReplayStore"
            assert result["requested_by"] == "admin-user"


class TestStoreProcessingEnvelope:
    """Test store_processing_envelope utility function"""
    
    def test_store_processing_envelope_success(self):
        """Test successful envelope storage"""
        envelope_data = {"message": "test", "source": "test"}
        
        result = store_processing_envelope(
            envelope_data=envelope_data,
            trace_id="test-trace-id",
            correlation_id="test-correlation-id",
            event_type="test_event",
            source="test_service",
            user_id="test-user"
        )
        
        assert result == True
        
        # Verify it was stored
        manager = get_replay_manager()
        envelopes = manager.store.get_envelopes_by_trace("test-trace-id")
        assert len(envelopes) == 1
        assert envelopes[0].envelope_data == envelope_data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
def now_utc():
    return datetime.now(timezone.utc)
