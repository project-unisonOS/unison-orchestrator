"""
Simple test to verify tracing functionality works
"""

import sys
import os

# Add the unison-common source to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'unison-common', 'src'))

try:
    from unison_common.tracing import TracingConfig, TraceContext, DistributedTracer
    print("✅ Successfully imported tracing modules")
    
    # Test basic functionality
    config = TracingConfig()
    print(f"✅ TracingConfig created: {config.service_name}")
    
    context = TraceContext()
    print(f"✅ TraceContext created: {context.trace_id}")
    
    headers = context.to_headers()
    print(f"✅ Headers generated: {len(headers)} headers")
    
    # Test disabled tracer
    config.enabled = False
    tracer = DistributedTracer(config)
    print(f"✅ DistributedTracer created (disabled): {tracer.config.enabled}")
    
    print("\n🎉 All basic tracing functionality tests passed!")
    
except ImportError as e:
    print(f"❌ Import error: {e}")
    print(f"Python path: {sys.path[:3]}")  # Show first few paths
    sys.exit(1)
except Exception as e:
    print(f"❌ Error: {e}")
    sys.exit(1)
