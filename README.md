# unison-orchestrator

The orchestrator is the central decision layer for the Unison system, coordinating communication between all modules and managing event flow.

## Purpose

The orchestrator service:
- Accepts user intent from I/O modules
- Queries `unison-context` for current user state and preferences
- Routes work to appropriate skills and generation providers
- Enforces `unison-policy` for safety, consent, and authorization
- Coordinates responses through `unison-io-*` modules (speech, canvas, etc.)
- Provides authentication and authorization middleware

## Current Status

### ✅ Implemented
- FastAPI-based HTTP service with health endpoints
- Event envelope validation and sanitization
- Authentication middleware with JWT verification
- Role-based access control (RBAC)
- Security headers and rate limiting
- Policy evaluation integration
- Skill registry and dispatch system
- Comprehensive audit logging
- Network-segmented deployment configuration

### 🚧 In Progress
- Advanced skill composition and chaining
- Real-time event streaming
- Performance optimization and caching

### 📋 Planned
- Workflow orchestration engine
- Event replay and recovery
- Advanced monitoring and metrics

## Quick Start

### Local Development
```bash
# Clone and setup
git clone https://github.com/project-unisonOS/unison-orchestrator
cd unison-orchestrator

# Install dependencies
pip install -r requirements.txt

# Run with authentication
export UNISON_JWT_SECRET="your-secret-key"
python src/server.py
```

### Docker Deployment
```bash
# Using the development stack
cd ../unison-devstack
docker-compose up -d orchestrator

# Health check
curl http://localhost:8080/health
```

### Security-Hardened Deployment
```bash
# Using the security configuration
cd ../unison-devstack
docker-compose -f docker-compose.security.yml up -d

# Access through API gateway
curl https://localhost/api/health
```

## API Reference

### Core Endpoints
- `GET /health` - Service health check
- `GET /ready` - Dependency readiness check
- `GET /metrics` - Prometheus metrics
- `POST /event` - Main event processing endpoint
- `GET /skills` - List registered skills
- `POST /skills` - Register new skill (admin only)

### Authentication
All protected endpoints require JWT authentication:
```bash
# Get token from auth service
curl -X POST http://localhost:8088/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password&username=admin&password=admin123"

# Use token for authenticated requests
curl -X POST http://localhost:8080/event \
  -H "Authorization: Bearer <access-token>" \
  -H "Content-Type: application/json" \
  -d '{"intent": "echo", "payload": {"message": "Hello World"}}'
```

[Full API Documentation](../../unison-docs/developer/api-reference/orchestrator.md)

## Configuration

### Environment Variables
```bash
# Service Configuration
UNISON_CONTEXT_HOST=context          # Context service host
UNISON_CONTEXT_PORT=8081            # Context service port
UNISON_STORAGE_HOST=storage         # Storage service host
UNISON_STORAGE_PORT=8082            # Storage service port
UNISON_POLICY_HOST=policy           # Policy service host
UNISON_POLICY_PORT=8083             # Policy service port
UNISON_INFERENCE_HOST=inference     # Inference service host
UNISON_INFERENCE_PORT=8087          # Inference service port

# Security Configuration
UNISON_JWT_SECRET=your-secret-key   # JWT signing secret
UNISON_ALLOWED_HOSTS=localhost      # Allowed hostnames
UNISON_CORS_ORIGINS=http://localhost:3000  # CORS origins

# Rate Limiting
UNISON_GLOBAL_RATE_LIMIT=100        # Global requests/minute
UNISON_USER_RATE_LIMIT=200          # Per-user requests/minute
```

## Development

### Setup
```bash
# Install development dependencies
pip install -r requirements-dev.txt

# Run tests
pytest tests/

# Run with debug logging
LOG_LEVEL=DEBUG python src/server.py
```

### Testing
```bash
# Unit tests
pytest tests/unit/

# Integration tests
pytest tests/integration/

# Security tests
pytest tests/security/

# Load testing
locust -f tests/load/locustfile.py
```

### Contributing
1. Fork the repository
2. Create a feature branch
3. Make your changes with tests
4. Ensure all tests pass and code follows style guidelines
5. Submit a pull request with description

[Development Guide](../../unison-docs/developer/contributing.md)

## Security

### Authentication
- JWT-based authentication with access/refresh tokens
- Role-based access control (admin, operator, developer, user, service)
- Token blacklisting and revocation via Redis
- Service-to-service authentication

### Rate Limiting
- IP-based rate limiting (100 requests/minute)
- Per-user rate limiting (200 requests/minute)
- Configurable limits per endpoint type

### Security Headers
- Content Security Policy (CSP)
- HTTP Strict Transport Security (HSTS)
- X-Frame-Options, X-Content-Type-Options
- CORS configuration

[Security Documentation](../../unison-docs/operations/security.md)

## Architecture

The orchestrator follows a modular architecture:

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   I/O Modules   │───▶│   Orchestrator   │───▶│  Skills/Gen     │
│ (speech, vision)│    │   (Decision)     │    │    Services     │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                │
                                ▼
                       ┌──────────────────┐
                       │   Policy Engine  │
                       │ (Safety/Authz)   │
                       └──────────────────┘
                                │
                                ▼
                       ┌──────────────────┐
                       │  Context Store   │
                       │ (User State)     │
                       └──────────────────┘
```

[Architecture Documentation](../../unison-docs/developer/architecture.md)

## Monitoring

### Health Checks
- `/health` - Basic service health
- `/ready` - Dependency health (context, storage, policy, inference)
- `/metrics` - Prometheus-compatible metrics

### Logging
Structured JSON logging with correlation IDs:
- Event processing logs
- Authentication/authorization events
- Policy evaluation results
- Performance metrics
- Error tracking

### Metrics
Key metrics available:
- Request counts by endpoint
- Authentication success/failure rates
- Event processing latency
- Policy decision statistics
- Error rates by type

[Monitoring Guide](../../unison-docs/operations/monitoring.md)

## Related Services

### Dependencies
- **unison-auth** - Authentication and token management
- **unison-context** - User state and preferences
- **unison-storage** - Data persistence and retrieval
- **unison-policy** - Safety and authorization rules
- **unison-inference** - AI/ML generation services

### Data Flow
1. User intent received from I/O modules
2. Context queried for user state
3. Policy evaluated for safety/authorization
4. Skills dispatched for processing
5. Results coordinated back to I/O modules

## Troubleshooting

### Common Issues

**Authentication Failures**
```bash
# Check auth service connectivity
curl http://localhost:8088/health

# Verify JWT secret matches auth service
grep UNISON_JWT_SECRET .env
```

**Policy Evaluation Errors**
```bash
# Check policy service health
curl http://localhost:8083/health

# Verify policy rules are loaded
curl http://localhost:8083/rules/summary
```

**Context Service Issues**
```bash
# Check context service connectivity
curl http://localhost:8081/health

# Test context retrieval
curl -X POST http://localhost:8081/query \
  -H "Content-Type: application/json" \
  -d '{"keys": ["user.preferences"]}'
```

### Debug Mode
```bash
# Enable verbose logging
LOG_LEVEL=DEBUG UNISON_DEBUG_AUTH=true python src/server.py

# Check service dependencies
curl http://localhost:8080/ready
```

[Troubleshooting Guide](../../unison-docs/user/troubleshooting.md)

## Version Compatibility

| Orchestrator Version | Unison Common | Auth Service | Minimum Docker |
|---------------------|---------------|--------------|----------------|
| 1.0.0               | 1.0.0         | 1.0.0        | 20.10+         |
| 0.9.x               | 0.9.x         | 0.9.x        | 20.04+         |

[Compatibility Matrix](../../unison-spec/specs/version-compatibility.md)

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.

## Support

- **Documentation**: [Project Unison Docs](https://github.com/project-unisonOS/unison-docs)
- **Issues**: [GitHub Issues](https://github.com/project-unisonOS/unison-orchestrator/issues)
- **Discussions**: [GitHub Discussions](https://github.com/project-unisonOS/unison-orchestrator/discussions)
- **Security**: Report security issues to security@unisonos.org
