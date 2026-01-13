# BOOPPA v10.0 API Reference

## Base URL
```
https://api.booppa.com/api/v1
```

## Authentication
Most endpoints require JWT authentication.

## Endpoints

### Health Checks
- `GET /health` - Basic service health
- `GET /api/v1/health` - Comprehensive health with service status

### Reports
- `POST /api/v1/reports` - Create new audit report
- `GET /api/v1/reports/{id}` - Get report status and details

### Authentication
- `POST /api/v1/auth/token` - Get JWT access token

## Example Usage

### Create Report
```bash
curl -X POST http://localhost:8000/api/v1/reports \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -d '{
    "framework": "MTCS",
    "company_name": "Example Corp",
    "assessment_data": {
      "controls": ["access_control", "encryption"],
      "score": 95
    }
  }'
```

### Get Report Status
```bash
curl -X GET http://localhost:8000/api/v1/reports/12345678-1234-1234-1234-123456789012 \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```
