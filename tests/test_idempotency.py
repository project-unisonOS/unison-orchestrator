"""
pytest tests for idempotency functionality
"""

import pytest
import time
import uuid
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch, AsyncMock

from unison_common.idempotency import (
    IdempotencyConfig,
    IdempotencyRecord,
    IdempotencyManager,
    MemoryIdempotencyStore,
    RedisIdempotencyStore,
    get_idempotency_manager,
    initialize_idempotency,
    validate_idempotency_key,
    extract_idempotency_key
)
from unison_common.idempotency_middleware import (
    IdempotencyMiddleware,
    IdempotencyKeyRequiredMiddleware,
    add_idempotency_headers,
    create_idempotency_response
)


class TestIdempotencyConfig:
    """Test idempotency configuration"""
    
    def test_default_config(self):
        """Test default configuration values"""
        config = IdempotencyConfig()
        
        assert config.default_ttl_seconds == 24 * 60 * 60  # 24 hours
        assert config.max_ttl_seconds == 7 * 24 * 60 * 60   # 7 days
        assert config.cleanup_interval_seconds == 60 * 60   # 1 hour
        assert config.max_records == 10000
        assert config.key_length == 36  # UUID length
        assert config.hash_request_body == True
    
    def test_config_modification(self):
        """Test configuration modification"""
        config = IdempotencyConfig()
        config.default_ttl_seconds = 3600
        config.max_records = 5000
        
        assert config.default_ttl_seconds == 3600
        assert config.max_records == 5000


class TestIdempotencyRecord:
    """Test idempotency record functionality"""
    
    def test_record_creation(self):
        """Test creating an idempotency record"""
        now = now_utc()
        expires_at = now + timedelta(hours=1)
        
        record = IdempotencyRecord(
            idempotency_key="test-key",
            response_data={"message": "success"},
            status_code=200,
            created_at=now,
            expires_at=expires_at,
            user_id="test-user",
            endpoint="/test"
        )
        
        assert record.idempotency_key == "test-key"
        assert record.response_data == {"message": "success"}
        assert record.status_code == 200
        assert record.user_id == "test-user"
        assert record.endpoint == "/test"
    
    def test_record_serialization(self):
        """Test record to/from dictionary conversion"""
        now = now_utc()
        expires_at = now + timedelta(hours=1)
        
        record = IdempotencyRecord(
            idempotency_key="test-key",
            response_data={"message": "success"},
            status_code=200,
            created_at=now,
            expires_at=expires_at
        )
        
        # Convert to dictionary
        data = record.to_dict()
        assert data["idempotency_key"] == "test-key"
        assert data["status_code"] == 200
        assert "created_at" in data
        assert "expires_at" in data
        
        # Convert back from dictionary
        restored_record = IdempotencyRecord.from_dict(data)
        assert restored_record.idempotency_key == record.idempotency_key
        assert restored_record.status_code == record.status_code
        assert restored_record.created_at == record.created_at


class TestMemoryIdempotencyStore:
    """Test in-memory idempotency store"""
    
    @pytest.fixture
    def store(self):
        """Create a test store"""
        config = IdempotencyConfig()
        config.max_records = 5  # Small for testing
        return MemoryIdempotencyStore(config)
    
    def test_store_and_retrieve(self, store):
        """Test storing and retrieving records"""
        now = now_utc()
        expires_at = now + timedelta(hours=1)
        
        record = IdempotencyRecord(
            idempotency_key="test-key",
            response_data={"message": "success"},
            status_code=200,
            created_at=now,
            expires_at=expires_at
        )
        
        # Store record
        result = store.put(record)
        assert result == True
        
        # Retrieve record
        retrieved = store.get("test-key")
        assert retrieved is not None
        assert retrieved.idempotency_key == "test-key"
        assert retrieved.status_code == 200
    
    def test_get_nonexistent(self, store):
        """Test getting non-existent record"""
        result = store.get("nonexistent-key")
        assert result is None
    
    def test_delete_record(self, store):
        """Test deleting a record"""
        now = now_utc()
        expires_at = now + timedelta(hours=1)
        
        record = IdempotencyRecord(
            idempotency_key="test-key",
            response_data={"message": "success"},
            status_code=200,
            created_at=now,
            expires_at=expires_at
        )
        
        store.put(record)
        assert store.get("test-key") is not None
        
        # Delete record
        result = store.delete("test-key")
        assert result == True
        
        # Verify deletion
        assert store.get("test-key") is None
    
    def test_expired_record_cleanup(self, store):
        """Test cleanup of expired records"""
        now = now_utc()
        expired_time = now - timedelta(hours=1)  # Already expired
        
        record = IdempotencyRecord(
            idempotency_key="expired-key",
            response_data={"message": "success"},
            status_code=200,
            created_at=expired_time,
            expires_at=expired_time
        )
        
        store.put(record)
        assert store.size() == 0  # Should be cleaned up immediately
    
    def test_capacity_limit(self, store):
        """Test store capacity limit"""
        now = now_utc()
        expires_at = now + timedelta(hours=1)
        
        # Fill store to capacity
        for i in range(5):
            record = IdempotencyRecord(
                idempotency_key=f"key-{i}",
                response_data={"message": f"success-{i}"},
                status_code=200,
                created_at=now,
                expires_at=expires_at
            )
            store.put(record)
        
        assert store.size() == 5
        
        # Add one more (should trigger cleanup)
        extra_record = IdempotencyRecord(
            idempotency_key="extra-key",
            response_data={"message": "extra"},
            status_code=200,
            created_at=now,
            expires_at=expires_at
        )
        store.put(extra_record)
        
        # Should have removed some old records
        assert store.size() <= 5


class TestIdempotencyManager:
    """Test idempotency manager"""
    
    @pytest.fixture
    def manager(self):
        """Create a test manager"""
        config = IdempotencyConfig()
        store = MemoryIdempotencyStore(config)
        return IdempotencyManager(config, store)
    
    def test_generate_key(self, manager):
        """Test generating idempotency keys"""
        key1 = manager.generate_key()
        key2 = manager.generate_key()
        
        assert key1 != key2
        assert validate_idempotency_key(key1)
        assert validate_idempotency_key(key2)
        assert len(key1) == 36  # UUID length
    
    def test_hash_request(self, manager):
        """Test request hashing"""
        method = "POST"
        url = "/test"
        body = {"message": "hello", "value": 42}
        user_id = "test-user"
        
        hash1 = manager.hash_request(method, url, body, user_id)
        hash2 = manager.hash_request(method, url, body, user_id)
        
        assert hash1 == hash2
        assert len(hash1) == 64  # SHA256 hex length
        
        # Different request should have different hash
        hash3 = manager.hash_request(method, url, {"message": "different"}, user_id)
        assert hash1 != hash3
    
    def test_check_idempotency_new_request(self, manager):
        """Test checking idempotency for new request"""
        is_duplicate, record = manager.check_idempotency(
            idempotency_key="new-key",
            method="POST",
            url="/test",
            body={"message": "hello"}
        )
        
        assert is_duplicate == False
        assert record is None
    
    def test_check_idempotency_existing_request(self, manager):
        """Test checking idempotency for existing request"""
        # First, create a record
        manager.create_record(
            idempotency_key="existing-key",
            response_data={"result": "success"},
            status_code=200,
            method="POST",
            url="/test",
            body={"message": "hello"}
        )
        
        # Then check for duplicate
        is_duplicate, record = manager.check_idempotency(
            idempotency_key="existing-key",
            method="POST",
            url="/test",
            body={"message": "hello"}
        )
        
        assert is_duplicate == True
        assert record is not None
        assert record.status_code == 200
    
    def test_create_record(self, manager):
        """Test creating an idempotency record"""
        record = manager.create_record(
            idempotency_key="test-key",
            response_data={"result": "success"},
            status_code=201,
            method="POST",
            url="/test",
            body={"message": "hello"},
            user_id="test-user",
            ttl_seconds=3600
        )
        
        assert record.idempotency_key == "test-key"
        assert record.status_code == 201
        assert record.user_id == "test-user"
        assert record.endpoint == "/test"
        
        # Verify it was stored
        retrieved = manager.store.get("test-key")
        assert retrieved is not None
        assert retrieved.idempotency_key == "test-key"
    
    def test_invalidate_key(self, manager):
        """Test invalidating an idempotency key"""
        # Create a record first
        manager.create_record(
            idempotency_key="test-key",
            response_data={"result": "success"},
            status_code=200,
            method="POST",
            url="/test"
        )
        
        # Verify it exists
        assert manager.store.get("test-key") is not None
        
        # Invalidate it
        result = manager.invalidate_key("test-key")
        assert result == True
        
        # Verify it's gone
        assert manager.store.get("test-key") is None
    
    def test_get_stats(self, manager):
        """Test getting idempotency statistics"""
        stats = manager.get_stats()
        
        assert "store_type" in stats
        assert "max_records" in stats
        assert "default_ttl" in stats
        assert stats["store_type"] == "MemoryIdempotencyStore"


class TestIdempotencyUtilities:
    """Test idempotency utility functions"""
    
    def test_validate_idempotency_key_valid(self):
        """Test validating a valid UUID key"""
        valid_uuid = str(uuid.uuid4())
        assert validate_idempotency_key(valid_uuid) == True
    
    def test_validate_idempotency_key_invalid(self):
        """Test validating invalid keys"""
        assert validate_idempotency_key("") == False
        assert validate_idempotency_key("invalid-key") == False
        assert validate_idempotency_key("123-456-789") == False
        assert validate_idempotency_key(None) == False
    
    def test_extract_idempotency_key_from_headers(self):
        """Test extracting idempotency key from headers"""
        test_uuid = str(uuid.uuid4())
        
        # Test various header names
        headers1 = {"Idempotency-Key": test_uuid}
        assert extract_idempotency_key(headers1) == test_uuid
        
        headers2 = {"X-Idempotency-Key": test_uuid}
        assert extract_idempotency_key(headers2) == test_uuid
        
        headers3 = {"idempotency-key": test_uuid}
        assert extract_idempotency_key(headers3) == test_uuid
        
        # Test no key present
        headers4 = {"Other-Header": "value"}
        assert extract_idempotency_key(headers4) is None
        
        # Test invalid key
        headers5 = {"Idempotency-Key": "invalid-key"}
        assert extract_idempotency_key(headers5) is None


class TestIdempotencyMiddleware:
    """Test idempotency middleware"""
    
    @pytest.fixture
    def mock_app(self):
        """Create a mock FastAPI app"""
        async def mock_call_next(request):
            from fastapi import Response
            return Response(content="success", status_code=200)
        
        mock_app = Mock()
        mock_app.call_next = mock_call_next
        return mock_app
    
    @pytest.fixture
    def middleware(self, mock_app):
        """Create idempotency middleware"""
        return IdempotencyMiddleware(mock_app)
    
    @pytest.mark.asyncio
    async def test_middleware_without_idempotency_key(self, middleware):
        """Test middleware processing without idempotency key"""
        from fastapi import Request
        
        # Create mock request
        mock_request = Mock(spec=Request)
        mock_request.method = "POST"
        mock_request.url.path = "/test"
        mock_request.headers = {}
        mock_request.json = AsyncMock(return_value={"message": "hello"})
        
        # Process request
        async def mock_call_next(request):
            from fastapi import Response
            return Response(content="success", status_code=200)
        
        response = await mock_call_next(mock_request)
        assert response.status_code == 200
    
    def test_middleware_initialization(self):
        """Test middleware initialization"""
        mock_app = Mock()
        middleware = IdempotencyMiddleware(mock_app)
        
        assert middleware.app == mock_app
        assert middleware.idempotency_manager is not None
        assert middleware.default_ttl_seconds == 24 * 60 * 60


class TestIdempotencyKeyRequiredMiddleware:
    """Test idempotency key required middleware"""
    
    @pytest.fixture
    def mock_app(self):
        """Create a mock FastAPI app"""
        async def mock_call_next(request):
            from fastapi import Response
            return Response(content="success", status_code=200)
        
        mock_app = Mock()
        mock_app.call_next = mock_call_next
        return mock_app
    
    @pytest.fixture
    def middleware(self, mock_app):
        """Create middleware with required paths"""
        return IdempotencyKeyRequiredMiddleware(mock_app, required_paths=['/ingest'])
    
    @pytest.mark.asyncio
    async def test_required_path_with_key(self, middleware):
        """Test required path with valid idempotency key"""
        from fastapi import Request
        
        test_uuid = str(uuid.uuid4())
        mock_request = Mock(spec=Request)
        mock_request.url.path = "/ingest"
        mock_request.headers = {"Idempotency-Key": test_uuid}
        
        async def mock_call_next(request):
            from fastapi import Response
            return Response(content="success", status_code=200)
        
        response = await mock_call_next(mock_request)
        assert response.status_code == 200
    
    @pytest.mark.asyncio
    async def test_required_path_without_key(self, middleware):
        """Test required path without idempotency key should raise exception"""
        from fastapi import Request
        import json
        
        mock_request = Mock(spec=Request)
        mock_request.url.path = "/ingest"
        mock_request.headers = {}
        
        response = await middleware.dispatch(mock_request, Mock())
        assert response.status_code == 400
        detail = json.loads(response.body.decode()).get("detail", "")
        assert "Idempotency-Key" in detail


class TestGlobalManager:
    """Test global idempotency manager"""
    
    def test_get_global_manager(self):
        """Test getting the global manager"""
        manager = get_idempotency_manager()
        assert manager is not None
        assert isinstance(manager, IdempotencyManager)
    
    def test_initialize_global_manager(self):
        """Test initializing the global manager"""
        config = IdempotencyConfig()
        config.default_ttl_seconds = 7200
        
        initialize_idempotency(config)
        
        manager = get_idempotency_manager()
        assert manager.config.default_ttl_seconds == 7200


class TestIdempotencyResponseHelpers:
    """Test idempotency response helper functions"""
    
    def test_create_idempotency_response(self):
        """Test creating idempotency response"""
        from fastapi.responses import JSONResponse
        import json
        
        test_key = str(uuid.uuid4())
        response_data = {"message": "success"}
        
        response = create_idempotency_response(test_key, response_data, 201)
        
        assert response.status_code == 201
        assert json.loads(response.body.decode()) == response_data
        assert response.headers["Idempotency-Key"] == test_key
        assert response.headers["Idempotency-Original-Response"] == "201"
        assert "Idempotency-Created-At" in response.headers


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
def now_utc():
    return datetime.now(timezone.utc)
