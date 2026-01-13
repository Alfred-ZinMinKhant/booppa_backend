# BOOPPA v10.0 Enterprise - Deployment Guide

## Quick Deployment

### 1. Environment Setup
```bash
# Copy environment template
cp .env.example .env

# Edit with your credentials
nano .env
```

### 2. Database Setup
```bash
# Start PostgreSQL and Redis
docker-compose up -d postgres redis

# Wait for databases to be ready
sleep 10

# Initialize database (development)
docker-compose run --rm app python scripts/init_database.py

# Or use Alembic migrations (production)
docker-compose run --rm app alembic upgrade head
```

### 3. Start Services
```bash
# Start all services
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f app
```

### 4. Verify Installation
```bash
# Health check
curl http://localhost:8000/health

# API health
curl http://localhost:8000/api/v1/health
```

## Production Deployment

### AWS ECS/Fargate
- Build and push Docker image to ECR
- Create ECS cluster with Fargate
- Configure load balancer and auto-scaling
- Set up RDS PostgreSQL and ElastiCache Redis

### Kubernetes
```yaml
# Sample deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: booppa-api
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: app
        image: booppa:v10.0
        ports:
        - containerPort: 8000
```

## Monitoring & Observability

### Health Checks
- `GET /health` - Basic health check
- `GET /api/v1/health` - Comprehensive service health

### Metrics
- Prometheus metrics on `/metrics`
- Grafana dashboards for business metrics

### Logging
- Structured JSON logging
- CloudWatch / ELK stack integration

## Security Checklist

- [ ] Change default SECRET_KEY
- [ ] Configure proper CORS origins
- [ ] Set up AWS IAM roles with least privilege
- [ ] Enable database encryption at rest
- [ ] Configure SSL/TLS termination
- [ ] Set up WAF and DDoS protection
- [ ] Regular security patches and updates

## Troubleshooting

### Common Issues

#### Database Connection Failed
- Check DATABASE_URL in .env
- Verify PostgreSQL is running
- Check network connectivity

#### Redis Connection Issues
- Verify REDIS_URL format
- Check Redis server status

#### S3 Upload Fails
- Verify AWS credentials
- Check S3 bucket permissions
- Confirm AWS region matches bucket region

#### Logs Investigation
```bash
# View all logs
docker-compose logs

# View specific service
docker-compose logs app

# Follow logs in real-time
docker-compose logs -f worker
```
