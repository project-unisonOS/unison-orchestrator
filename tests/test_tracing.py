"""
pytest tests for distributed tracing functionality
"""

import pytest
import time
import uuid
import os
from unittest.mock import Mock, patch
from unison_common.tracing import (
    TracingConfig,
    DistributedTracer,
    TraceContext,
    get_tracer,
    initialize_tracing,
    trace_span,
    trace_async_span,
    add_span_attributes,
    add_span_event,
    set_span_error,
    set_span_ok,
    CorrelationMiddleware,
    get_correlation_id,
    get_trace_context,
    trace_http_request,
    trace_service_call,
    trace_database_operation,
)

class TestTracingConfig:
    """Test tracing configuration"""
    
    def test_default_config(self):
        """Test default configuration values"""
        config = TracingConfig()
        
        assert config.service_name == "unison-service"
        assert config.service_version == "1.0.0"
        assert config.environment == "development"
        assert config.jaeger_endpoint == "http://jaeger:14268/api/traces"
        assert config.otlp_endpoint == "http://jaeger:4317"
        assert config.sample_rate == 1.0
        assert config.enabled == True
        assert config.propagator == "b3"
    
    def test_environment_override(self):
        """Test environment variable overrides"""
        with patch.dict(os.environ, {
            'OTEL_SERVICE_NAME': 'test-service',
            'OTEL_SERVICE_VERSION': '2.0.0',
            'OTEL_ENVIRONMENT': 'test',
            'OTEL_SAMPLE_RATE': '0.5',
            'OTEL_ENABLED': 'false',
            'OTEL_PROPAGATOR': 'jaeger'
        }):
            config = TracingConfig()
            
            assert config.service_name == "test-service"
            assert config.service_version == "2.0.0"
            assert config.environment == "test"
            assert config.sample_rate == 0.5
            assert config.enabled == False
            assert config.propagator == "jaeger"

class TestTraceContext:
    """Test trace context functionality"""
    
    def test_trace_context_creation(self):
        """Test creating a new trace context"""
        context = TraceContext()
        
        assert context.trace_id is not None
        assert context.span_id is not None
        assert context.parent_span_id is None
        assert context.baggage == {}
        assert context.start_time > 0
    
    def test_trace_context_with_parameters(self):
        """Test creating trace context with parameters"""
        trace_id = str(uuid.uuid4())
        span_id = str(uuid.uuid4())[:16]
        baggage = {"user_id": "123", "session_id": "45"}
        
        context = TraceContext(
            trace_id=trace_id,
            span_id=span_id,
            baggage=baggage
        )
        
        assert context.trace_id == trace_id
        assert context.span_id == span_id
        assert context.baggage == baggage
    
    def test_trace_context_to_headers(self):
        """Test converting trace context to headers"""
        context = TraceContext(
            trace_id="test-trace-id",
            span_id="test-span-id",
            parent_span_id="test-parent-id",
            baggage={"user_id": "123", "session_id": "45"}
        )
        
        headers = context.to_headers()
        
        assert headers["X-Request-Id"] == "test-trace-id"
        assert headers["X-Trace-Id"] == "test-trace-id"
        assert headers["X-Span-Id"] == "test-span-id"
        assert headers["X-Parent-Span-Id"] == "test-parent-id"
        assert headers["X-Baggage-user_id"] == "123"
        assert headers["X-Baggage-session_id"] == "45"
    
    def test_trace_context_from_headers(self):
        """Test creating trace context from headers"""
        headers = {
            "X-Request-Id": "test-trace-id",
            "X-Span-Id": "test-span-id",
            "X-Parent-Span-Id": "test-parent-id",
            "X-Baggage-user_id": "123",
            "X-Baggage-session_id": "45"
        }
        
        context = TraceContext.from_headers(headers)
        
        assert context.trace_id == "test-trace-id"
        assert context.span_id == "test-span-id"
        assert context.parent_span_id == "test-parent-id"
        assert context.baggage["user_id"] == "123"
        assert context.baggage["session_id"] == "45"

class TestDistributedTracer:
    """Test distributed tracer functionality"""
    
    @pytest.fixture
    def disabled_tracer(self):
        """Create a disabled tracer for testing"""
        config = TracingConfig()
        config.enabled = False
        return DistributedTracer(config)
    
    @pytest.fixture
    def mock_tracer(self):
        """Create a mock tracer for testing"""
        with patch('unison_common.tracing.trace') as mock_trace:
            # Mock the trace module
            mock_trace.get_tracer_provider.return_value = Mock()
            mock_trace.get_tracer.return_value = Mock()
            mock_trace.get_current_span.return_value = Mock()
            
            config = TracingConfig()
            config.enabled = True
            config.jaeger_endpoint = None  # Disable actual exporters
            config.otlp_endpoint = None
            
            tracer = DistributedTracer(config)
            return tracer
    
    def test_tracer_creation_disabled(self, disabled_tracer):
        """Test creating disabled tracer"""
        assert disabled_tracer.config.enabled == False
        assert disabled_tracer._initialized == False
    
    def test_create_trace_context_disabled(self, disabled_tracer):
        """Test creating trace context with disabled tracer"""
        context = disabled_tracer.create_trace_context()
        
        assert isinstance(context, TraceContext)
        assert context.trace_id is not None
    
    def test_create_trace_context_from_headers(self, disabled_tracer):
        """Test creating trace context from headers"""
        headers = {"X-Request-Id": "test-id"}
        context = disabled_tracer.create_trace_context(headers)
        
        assert context.trace_id == "test-id"
    
    def test_inject_headers_disabled(self, disabled_tracer):
        """Test injecting headers with disabled tracer"""
        headers = disabled_tracer.inject_headers()
        
        assert "X-Request-Id" in headers
        assert "X-Trace-Id" in headers
        assert "X-Span-Id" in headers
    
    def test_extract_context_disabled(self, disabled_tracer):
        """Test extracting context with disabled tracer"""
        headers = {"X-Request-Id": "test-id"}
        context = disabled_tracer.extract_context(headers)
        
        assert context.trace_id == "test-id"

class TestTracingDecorators:
    """Test tracing decorators"""
    
    def test_trace_span_decorator(self):
        """Test trace span decorator"""
        @trace_span("test-operation")
        def test_function(x, y):
            return x + y
        
        result = test_function(1, 2)
        assert result == 3
    
    @pytest.mark.asyncio
    async def test_trace_async_span_decorator(self):
        """Test trace async span decorator"""
        @trace_async_span("test-async-operation")
        async def test_async_function(x, y):
            return x + y
        
        result = await test_async_function(1, 2)
        assert result == 3

class TestCorrelationMiddleware:
    """Test correlation middleware"""
    
    @pytest.fixture
    def mock_app(self):
        """Create mock FastAPI app"""
        app = Mock()
        return app
    
    def test_middleware_creation(self, mock_app):
        """Test creating correlation middleware"""
        middleware = CorrelationMiddleware(mock_app)
        assert middleware.app == mock_app
    
    def test_correlation_id_extraction(self):
        """Test correlation ID extraction from request"""
        # Mock request with headers
        mock_request = Mock()
        mock_request.state = {}
        
        # Test with no correlation ID
        with patch('unison_common.tracing.uuid.uuid4') as mock_uuid:
            mock_uuid.return_value = "test-uuid"
            
            correlation_id = get_correlation_id(mock_request)
            assert correlation_id == "test-uuid"
    
    def test_trace_context_extraction(self):
        """Test trace context extraction from request"""
        # Mock request with trace context
        mock_request = Mock()
        mock_request.state = {
            "trace_context": TraceContext(trace_id="test-trace-id")
        }
        
        context = get_trace_context(mock_request)
        assert context.trace_id == "test-trace-id"

class TestTracingIntegration:
    """Test tracing integration with orchestrator"""
    
    @pytest.fixture
    def test_config(self):
        """Create test configuration"""
        config = TracingConfig()
        config.enabled = False  # Disable for testing
        config.service_name = "test-orchestrator"
        return config
    
    def test_global_tracer_initialization(self, test_config):
        """Test global tracer initialization"""
        initialize_tracing(test_config)
        
        tracer = get_tracer()
        assert tracer.config.service_name == "test-orchestrator"
        assert tracer.config.enabled == False
    
    def test_span_attributes(self):
        """Test adding span attributes"""
        # This test verifies the functions don't crash when tracing is disabled
        add_span_attributes({"test": "value"})
        add_span_event("test-event", {"key": "value"})
        set_span_error(Exception("test error"))
        set_span_ok("test success")
        
        # If we get here without exceptions, the test passes
        assert True

class TestTracingUtilities:
    """Test tracing utility functions"""
    
    def test_trace_http_request(self):
        """Test HTTP request tracing"""
        # Test with disabled tracing - should not crash
        trace_http_request(
            method="GET",
            url="http://example.com",
            status_code=200,
            duration_ms=100.0,
            headers={"X-Test": "value"}
        )
        assert True
    
    def test_trace_service_call(self):
        """Test service call tracing"""
        # Test with disabled tracing - should not crash
        trace_service_call(
            service_name="test-service",
            operation="test-operation",
            duration_ms=50.0,
            success=True
        )
        assert True
    
    def test_trace_database_operation(self):
        """Test database operation tracing"""
        # Test with disabled tracing - should not crash
        trace_database_operation(
            query_type="GET",
            table="test-table",
            duration_ms=25.0,
            success=True
        )
        assert True

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
