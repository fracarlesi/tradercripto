# Traefik Integration Guide (T084-T089)

## Overview

This document describes the Traefik reverse proxy integration for the Trader Bitcoin application, enabling:
- Automatic SSL/TLS with Let's Encrypt
- HTTP to HTTPS redirection
- Load balancing with health checks
- Custom domain routing

---

## Architecture

```
Internet
    ↓
Traefik (Reverse Proxy)
    ↓
├─→ trader_app:5611 (oaa.finan.club)
├─→ PostgreSQL:5432 (internal only)
└─→ Redis:6379 (internal only)
```

---

## Traefik Labels Configuration (T084)

The application service uses Traefik labels for automatic configuration:

### Basic Routing

```yaml
labels:
  # Enable Traefik for this service
  - "traefik.enable=true"

  # Router configuration
  - "traefik.http.routers.trader.rule=Host(`oaa.finan.club`)"
  - "traefik.http.routers.trader.entrypoints=websecure"

  # SSL/TLS configuration
  - "traefik.http.routers.trader.tls.certresolver=letsencrypt"

  # Service configuration
  - "traefik.http.services.trader.loadbalancer.server.port=5611"
```

### Health Check Integration (T085)

```yaml
labels:
  # Health check endpoint
  - "traefik.http.services.trader.loadbalancer.healthcheck.path=/api/health"
  - "traefik.http.services.trader.loadbalancer.healthcheck.interval=30s"
```

---

## Traefik Network Configuration (T086)

### Network Setup

The application connects to two networks:

1. **trader_network** (Internal):
   - Communication between app, PostgreSQL, and Redis
   - Not exposed to internet

2. **traefik** (External):
   - Pre-existing network for Traefik
   - Exposes app to internet via Traefik

```yaml
networks:
  trader_network:
    driver: bridge
    name: trader_network
  traefik:
    external: true  # Must be created before docker-compose up
```

### Network Isolation (T087)

**Internal Services** (not exposed):
- PostgreSQL (postgres:5432)
- Redis (redis:6379)
- Only accessible via trader_network

**External Service** (exposed via Traefik):
- Application (app:5611)
- Accessible via both trader_network and traefik

---

## Traefik Prerequisites

### 1. Create Traefik Network (T088)

Before starting the application stack, create the Traefik network:

```bash
docker network create traefik
```

### 2. Deploy Traefik (T088)

Create `traefik/docker-compose.yml`:

```yaml
version: '3.8'

services:
  traefik:
    image: traefik:v2.10
    container_name: traefik
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    ports:
      - "80:80"
      - "443:443"
    environment:
      # Let's Encrypt configuration
      - LETSENCRYPT_EMAIL=your-email@example.com
    volumes:
      - /etc/localtime:/etc/localtime:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./traefik.yml:/traefik.yml:ro
      - ./acme.json:/acme.json
      - ./dynamic_conf:/dynamic_conf:ro
    networks:
      - traefik
    labels:
      # Enable Traefik dashboard (optional)
      - "traefik.enable=true"
      - "traefik.http.routers.dashboard.rule=Host(`traefik.yourdomain.com`)"
      - "traefik.http.routers.dashboard.entrypoints=websecure"
      - "traefik.http.routers.dashboard.tls.certresolver=letsencrypt"
      - "traefik.http.routers.dashboard.service=api@internal"

networks:
  traefik:
    external: true
```

### 3. Traefik Configuration File (T088)

Create `traefik/traefik.yml`:

```yaml
api:
  dashboard: true
  insecure: false

entryPoints:
  web:
    address: ":80"
    http:
      redirections:
        entryPoint:
          to: websecure
          scheme: https
          permanent: true

  websecure:
    address: ":443"
    http:
      tls:
        certResolver: letsencrypt

providers:
  docker:
    endpoint: "unix:///var/run/docker.sock"
    exposedByDefault: false
    network: traefik

certificatesResolvers:
  letsencrypt:
    acme:
      email: your-email@example.com
      storage: /acme.json
      httpChallenge:
        entryPoint: web

log:
  level: INFO

accessLog:
  filePath: "/var/log/traefik/access.log"
  bufferingSize: 100
```

### 4. Prepare SSL Storage (T088)

```bash
cd traefik
touch acme.json
chmod 600 acme.json
mkdir -p dynamic_conf
```

---

## Deployment Steps (T089)

### Step 1: Start Traefik

```bash
cd traefik
docker-compose up -d
```

Verify Traefik is running:
```bash
docker ps | grep traefik
docker logs traefik
```

### Step 2: Update DNS

Point your domain to your server:
```
A Record: oaa.finan.club → YOUR_SERVER_IP
```

Verify DNS propagation:
```bash
dig oaa.finan.club +short
```

### Step 3: Start Application Stack

```bash
cd /path/to/trader_bitcoin
docker-compose up -d
```

### Step 4: Verify SSL Certificate

Wait 1-2 minutes for Let's Encrypt to issue certificate:

```bash
# Check Traefik logs for certificate
docker logs traefik | grep letsencrypt

# Test HTTPS
curl -I https://oaa.finan.club

# Verify redirect from HTTP to HTTPS
curl -I http://oaa.finan.club
```

### Step 5: Check Application Health

```bash
# Via Traefik (HTTPS)
curl https://oaa.finan.club/api/health

# Direct to container (HTTP)
curl http://localhost:5611/api/health
```

---

## Testing Traefik Integration (T089)

### Test Script

Create `backend/scripts/testing/test_traefik_integration.sh`:

```bash
#!/bin/bash

echo "Testing Traefik Integration..."

# Test 1: Check Traefik is running
if docker ps | grep -q traefik; then
    echo "✅ Traefik is running"
else
    echo "❌ Traefik is not running"
    exit 1
fi

# Test 2: Check network exists
if docker network inspect traefik > /dev/null 2>&1; then
    echo "✅ Traefik network exists"
else
    echo "❌ Traefik network not found"
    exit 1
fi

# Test 3: Check app is connected to traefik network
if docker inspect trader_app | grep -q "traefik"; then
    echo "✅ App is connected to Traefik network"
else
    echo "❌ App not connected to Traefik network"
    exit 1
fi

# Test 4: Test HTTP to HTTPS redirect
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -L http://oaa.finan.club)
if [ "$HTTP_STATUS" == "200" ]; then
    echo "✅ HTTP redirects to HTTPS"
else
    echo "❌ HTTP redirect failed (status: $HTTP_STATUS)"
fi

# Test 5: Test HTTPS access
HTTPS_STATUS=$(curl -s -o /dev/null -w "%{http_code}" https://oaa.finan.club/api/health)
if [ "$HTTPS_STATUS" == "200" ]; then
    echo "✅ HTTPS access works"
else
    echo "❌ HTTPS access failed (status: $HTTPS_STATUS)"
fi

# Test 6: Check SSL certificate
SSL_INFO=$(echo | openssl s_client -connect oaa.finan.club:443 2>/dev/null | openssl x509 -noout -issuer)
if echo "$SSL_INFO" | grep -q "Let's Encrypt"; then
    echo "✅ SSL certificate from Let's Encrypt"
else
    echo "⚠️  SSL certificate not from Let's Encrypt"
fi

echo ""
echo "Traefik integration tests complete!"
```

---

## Troubleshooting

### Issue 1: Certificate Not Issued

**Symptoms**: HTTPS not working, certificate errors

**Solution**:
```bash
# Check Traefik logs
docker logs traefik | grep -i error

# Verify domain DNS
dig oaa.finan.club +short

# Check port 80/443 are accessible
curl -I http://YOUR_SERVER_IP
curl -I https://YOUR_SERVER_IP
```

### Issue 2: 502 Bad Gateway

**Symptoms**: Traefik returns 502 when accessing domain

**Solution**:
```bash
# Check app is healthy
docker ps
docker logs trader_app

# Check app is on traefik network
docker network inspect traefik

# Verify health check endpoint
curl http://localhost:5611/api/health
```

### Issue 3: HTTP Not Redirecting

**Symptoms**: HTTP requests don't redirect to HTTPS

**Solution**:
- Verify `traefik.yml` has redirect configuration
- Check Traefik logs for errors
- Restart Traefik: `docker-compose restart traefik`

---

## Security Considerations (T087)

### Network Isolation

✅ **Internal services isolated**:
- PostgreSQL and Redis only on trader_network
- Not accessible from internet

✅ **Application exposed via Traefik**:
- Only app on traefik network
- All traffic proxied through Traefik
- SSL/TLS encryption enforced

### Firewall Rules

Recommended firewall configuration:

```bash
# Allow SSH
sudo ufw allow 22/tcp

# Allow HTTP/HTTPS (for Traefik)
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Deny direct access to application port
sudo ufw deny 5611/tcp

# Enable firewall
sudo ufw enable
```

---

## Monitoring

### Health Check Endpoint

Traefik monitors application health:
- **Endpoint**: `/api/health`
- **Interval**: 30 seconds
- **Action**: Removes unhealthy instances from load balancer

### Traefik Dashboard (Optional)

Access Traefik dashboard at `https://traefik.yourdomain.com` to:
- View active routers and services
- Monitor SSL certificates
- Check health check status
- View access logs

---

## Production Checklist (T089)

Before deploying to production:

- [ ] Traefik network created
- [ ] DNS A record points to server
- [ ] Ports 80/443 open in firewall
- [ ] Email configured for Let's Encrypt
- [ ] `acme.json` has correct permissions (600)
- [ ] HTTP to HTTPS redirect working
- [ ] SSL certificate issued successfully
- [ ] Application accessible via HTTPS
- [ ] Health checks passing
- [ ] Internal services (PostgreSQL/Redis) not exposed
- [ ] Logs are being collected
- [ ] Monitoring configured

---

## References

- [Traefik Documentation](https://doc.traefik.io/traefik/)
- [Let's Encrypt Documentation](https://letsencrypt.org/docs/)
- [Docker Networks](https://docs.docker.com/network/)

---

**Last Updated**: 2025-10-31 (T084-T089 - User Story 4)
