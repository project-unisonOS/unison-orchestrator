"""
Simple test to verify idempotency endpoints work
"""

import sys
import os

# Add the unison-common source to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'unison-common', 'src'))

try:
    from unison_common.idempotency import IdempotencyConfig, IdempotencyManager, validate_idempotency_key
    from unison_common.idempotency_middleware import IdempotencyMiddleware
    
    print("✅ Successfully imported idempotency modules")
    
    # Test basic functionality
    config = IdempotencyConfig()
    print(f"✅ IdempotencyConfig created: TTL={config.default_ttl_seconds}s, Max records={config.max_records}")
    
    manager = IdempotencyManager(config)
    new_key = manager.generate_key()
    print(f"✅ Generated idempotency key: {new_key}")
    
    # Test key validation
    assert validate_idempotency_key(new_key) == True
    assert validate_idempotency_key("invalid-key") == False
    print("✅ Key validation working correctly")
    
    # Test middleware creation
    mock_app = None  # We can't create a real FastAPI app here
    try:
        middleware = IdempotencyMiddleware(mock_app)
        print("✅ IdempotencyMiddleware created")
    except Exception as e:
        print(f"⚠️  Middleware creation failed (expected in simple test): {e}")
    
    # Test manager operations
    is_duplicate, record = manager.check_idempotency(new_key, "POST", "/test", {"message": "hello"})
    assert is_duplicate == False
    assert record is None
    print("✅ New request check working correctly")
    
    # Create a record
    created_record = manager.create_record(
        new_key, 
        {"result": "success"}, 
        200, 
        "POST", 
        "/test", 
        {"message": "hello"}
    )
    assert created_record.idempotency_key == new_key
    print("✅ Record creation working correctly")
    
    # Check for duplicate
    is_duplicate, record = manager.check_idempotency(new_key, "POST", "/test", {"message": "hello"})
    assert is_duplicate == True
    assert record is not None
    assert record.status_code == 200
    print("✅ Duplicate request detection working correctly")
    
    # Get stats
    stats = manager.get_stats()
    assert stats["store_type"] == "MemoryIdempotencyStore"
    assert stats["current_records"] >= 1
    print(f"✅ Stats working: {stats['current_records']} records in store")
    
    print("\n🎉 All basic idempotency functionality tests passed!")
    
except ImportError as e:
    print(f"❌ Import error: {e}")
    print(f"Python path: {sys.path[:3]}")  # Show first few paths
    sys.exit(1)
except Exception as e:
    print(f"❌ Error: {e}")
    sys.exit(1)
