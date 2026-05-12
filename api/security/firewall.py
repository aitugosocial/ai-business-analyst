"""
Dynamic Firewall with Real-time Rule Processing
Implements IP blocking, rate limiting, WAF rules, and pattern matching
"""

from fastapi import Request, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, List, Optional, Any
import json
import re
from datetime import datetime, timedelta
from collections import defaultdict
from config.logging import get_logger

logger = get_logger(__name__)

class FirewallRule:
    def __init__(self, id: int, name: str, type: str, status: str, priority: str, 
                 rule_config: dict, hits: int = 0):
        self.id = id
        self.name = name
        self.type = type  # ip_block, rate_limit, waf_rule, geo_block, pattern_match
        self.status = status  # active, inactive
        self.priority = priority  # high, medium, low
        self.rule_config = rule_config
        self.hits = hits


class FirewallManager:
    def __init__(self):
        self.rules: List[FirewallRule] = []
        self.request_counts: Dict[str, Dict[str, Any]] = defaultdict(dict)
        
    def load_rules(self, db: Session):
        """Load active firewall rules from database"""
        try:
            result = db.execute(text("""
                SELECT * FROM firewall_rules 
                WHERE is_active = true 
                ORDER BY 
                    CASE priority 
                        WHEN 'high' THEN 1 
                        WHEN 'medium' THEN 2 
                        WHEN 'low' THEN 3 
                    END
            """))
            
            self.rules = []
            for row in result:
                self.rules.append(FirewallRule(
                    id=row.id,
                    name=row.name,
                    type=row.type,
                    status='active',
                    priority=row.priority,
                    rule_config=json.loads(row.rule_config) if isinstance(row.rule_config, str) else row.rule_config,
                    hits=row.hits or 0
                ))
            
            logger.info(f"Loaded {len(self.rules)} active firewall rules")
        except Exception as e:
            logger.error(f"Failed to load firewall rules: {e}")
    
    async def process_request(self, request: Request, db: Session) -> Optional[dict]:
        """
        Process request through firewall rules
        Returns None if allowed, dict with error if blocked
        """
        try:
            client_ip = self._get_client_ip(request)
            user_agent = request.headers.get("user-agent", "unknown")
            path = request.url.path
            method = request.method
            
            # Identify if request has body (POST/PUT/PATCH)
            body_content = ""
            if method in ["POST", "PUT", "PATCH"]:
                try:
                    # We need to read the body without consuming it permanently for the endpoint
                    # This is tricky in FastAPI/Starlette. 
                    # The Middleware calling this must handle the body restoration.
                    # Here we assume request.state.body has been set by middleware if available
                    if hasattr(request.state, "body"):
                        body_content = request.state.body.decode("utf-8", errors="ignore")
                except Exception as e:
                    logger.debug(f"Could not read body for WAF: {e}")

            # Reload rules periodically (every 100 requests)
            if not hasattr(self, '_request_count'):
                self._request_count = 0
            self._request_count += 1
            if self._request_count % 100 == 0:
                self.load_rules(db)
            
            # 1. Behavior Analysis (Auto-Detection)
            behavior_block = self._analyze_behavior(client_ip, path, method)
            if behavior_block:
                 return behavior_block

            # 2. Process each configured rule
            for rule in self.rules:
                should_block = self._evaluate_rule(rule, {
                    'ip': client_ip,
                    'user_agent': user_agent,
                    'path': path,
                    'method': method,
                    'body': body_content,
                    'content_type': request.headers.get('content-type', ''),
                    'query_params': dict(request.query_params)
                })
                
                if should_block:
                    # Increment hit counter
                    self._increment_rule_hits(db, rule.id)
                    
                    # Log blocked request
                    db.execute(text("""
                        INSERT INTO security_events (type, severity, ip_address, description, status, details)
                        VALUES (:type, :severity, :ip, :desc, :status, :details)
                    """), {
                        'type': 'firewall_block',
                        'severity': 'high' if rule.priority == 'high' else 'medium',
                        'ip': client_ip,
                        'desc': f"Request blocked by firewall rule: {rule.name}",
                        'status': 'blocked',
                        'details': json.dumps({'rule': rule.name, 'path': path, 'method': method})
                    })
                    db.commit()
                    
                    logger.warning(f"Request blocked by firewall: rule={rule.name}, ip={client_ip}, path={path}")
                    
                    return {
                        'error': 'Access denied',
                        'message': 'Request blocked by firewall'
                    }
            
            return None  # Allow request
            
        except Exception as e:
            logger.error(f"Firewall processing error: {e}")
            return None  # Don't block on errors

    def _get_client_ip(self, request: Request) -> str:
        """
        Extract the true Public IP from request headers.
        Prioritizes X-Forwarded-For to handle proxies/load balancers.
        Ignores private/internal IPs if a valid public IP is found.
        """
        try:
            # 1. Check X-Forwarded-For (standard proxy header)
            x_forwarded_for = request.headers.get("X-Forwarded-For")
            if x_forwarded_for:
                # Header format: client, proxy1, proxy2
                # We want the FIRST valid public IP
                ips = [ip.strip() for ip in x_forwarded_for.split(",")]
                for ip_str in ips:
                    if self._is_public_ip(ip_str):
                        return ip_str
            
            # 2. Fallback to direct client host
            return request.client.host if request.client else "unknown"
        except Exception:
            return request.client.host if request.client else "unknown"

    def _is_public_ip(self, ip_str: str) -> bool:
        """Check if IP is public (IPv4 or IPv6)"""
        import ipaddress
        try:
            ip = ipaddress.ip_address(ip_str)
            return not ip.is_private and not ip.is_loopback and not ip.is_link_local
        except ValueError:
            return False

    def _analyze_behavior(self, ip: str, path: str, method: str) -> Optional[dict]:
        """
        Analyze request behavior for suspicious patterns.
        Currently implements: rapid request flood detection.
        """
        # Simple memory-based rate tracking (reset every minute by _check_rate_limit cleanup or logic)
        # For this demo, we'll check if a single IP hits > 20 req/sec (aggressive flood)
        # Note: Proper behavior analysis requires Redis or persistent state, using volatile dict for now.
        
        now = datetime.now()
        key = f"behavior:{ip}"
        
        if not hasattr(self, '_behavior_tracking'):
            self._behavior_tracking = {}
            
        if key not in self._behavior_tracking:
            self._behavior_tracking[key] = {'count': 1, 'start': now}
        else:
            data = self._behavior_tracking[key]
            # Reset if window > 1 second
            if (now - data['start']).total_seconds() > 1:
                self._behavior_tracking[key] = {'count': 1, 'start': now}
            else:
                data['count'] += 1
                if data['count'] > 25: # Blocking threshold: increased to >25 reqs/sec for SPA compatibility
                    logger.warning(f"BEHAVIOR BLOCK: Request flood detected from IP {ip}. Path: {path}")
                    return {
                        'error': 'Access denied', 
                        'message': 'Suspicious behavior detected (Request Flood)'
                    }
        return None

    
    def _evaluate_rule(self, rule: FirewallRule, context: dict) -> bool:
        """Evaluate a single firewall rule"""
        if rule.type == 'ip_block':
            return self._check_ip_block(rule, context['ip'])
        elif rule.type == 'rate_limit':
            return self._check_rate_limit(rule, context['ip'])
        elif rule.type == 'waf_rule':
            return self._check_waf_rule(rule, context)
        elif rule.type == 'pattern_match':
            return self._check_pattern_match(rule, context)
        return False
    
    def _check_ip_block(self, rule: FirewallRule, ip: str) -> bool:
        """Check if IP is in blocked list"""
        blocked_ips = rule.rule_config.get('blocked_ips', [])
        return ip in blocked_ips
    
    def _check_rate_limit(self, rule: FirewallRule, ip: str) -> bool:
        """Check rate limit for IP"""
        max_requests = rule.rule_config.get('max_requests', 100)
        time_window = rule.rule_config.get('time_window', 60000) / 1000  # Convert to seconds
        
        key = f"{rule.id}:{ip}"
        now = datetime.now()
        
        if key not in self.request_counts or 'reset_time' not in self.request_counts[key]:
            self.request_counts[key] = {
                'count': 1,
                'reset_time': now + timedelta(seconds=time_window)
            }
            return False
        
        record = self.request_counts[key]
        
        if now > record['reset_time']:
            # New window
            self.request_counts[key] = {
                'count': 1,
                'reset_time': now + timedelta(seconds=time_window)
            }
            return False
        
        record['count'] += 1
        
        if record['count'] > max_requests:
            return True  # Block - rate limit exceeded
        
        return False
    
    def _check_waf_rule(self, rule: FirewallRule, context: dict) -> bool:
        """Check WAF (Web Application Firewall) rule"""
        pattern = rule.rule_config.get('pattern')
        if not pattern:
            return False
        
        try:
            regex = re.compile(pattern, re.IGNORECASE)
            
            # Check path
            if regex.search(context['path']):
                logger.warning(f"WAF MATCH: Rule '{rule.name}' triggered by pattern '{pattern}' in path: '{context['path']}'")
                return True
            
            # Check user agent
            if regex.search(context['user_agent']):
                logger.warning(f"WAF MATCH: Rule '{rule.name}' triggered by pattern '{pattern}' in User-Agent: '{context['user_agent']}'")
                return True
                
            # Check Body (New) - Skip for multipart/form-data (avoids boundary false positives)
            if context.get('body') and 'multipart/form-data' not in context.get('content_type', '').lower():
                if regex.search(context['body']):
                    logger.warning(f"WAF MATCH: Rule '{rule.name}' triggered by pattern '{pattern}' in body: '{context['body'][:100]}...'")
                    return True

            # Check Query Parameters
            if context.get('query_params'):
                for key, value in context['query_params'].items():
                     if regex.search(str(value)):
                         logger.warning(f"WAF MATCH: Rule '{rule.name}' triggered by pattern '{pattern}' in query param '{key}': '{value}'")
                         return True

            
        except re.error as e:
            logger.error(f"Invalid regex pattern in WAF rule: {e}")
        
        return False
    
    def _check_pattern_match(self, rule: FirewallRule, context: dict) -> bool:
        """Check pattern match rule"""
        # Check user agent blocking
        blocked_agents = rule.rule_config.get('blocked_user_agents', [])
        if blocked_agents:
            user_agent_lower = context['user_agent'].lower()
            for blocked_agent in blocked_agents:
                if blocked_agent.lower() in user_agent_lower:
                    return True
        
        # Check method restrictions
        allowed_methods = rule.rule_config.get('allowed_methods', [])
        if allowed_methods and context['method'] not in allowed_methods:
            return True
        
        return False
    
    def _increment_rule_hits(self, db: Session, rule_id: int):
        """Increment hit counter for rule"""
        try:
            db.execute(text("""
                UPDATE firewall_rules 
                SET hits = hits + 1, updated_at = NOW() 
                WHERE id = :id
            """), {'id': rule_id})
            db.commit()
        except Exception as e:
            logger.error(f"Failed to increment rule hits: {e}")

# Import BaseHTTPMiddleware for the Firewall Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from database.pg_connections import SessionLocal

class FirewallMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 0. Skip firewall for OPTIONS requests (CORS preflight)
        if request.method == "OPTIONS":
            return await call_next(request)

        # 1. Skip firewall for specific paths (metrics, health, static, auth, and critical commerce)
        whitelist = (
            "/health", "/docs", "/openapi.json", "/assets",
            "/api/me", "/users/me", "/user/me",
            "/api/stripe", "/api/payments", "/api/commissions",
            "/api/control", "/api/referrals", "/api/earnings",
            "/api/customer-service/ws",  # WebSockets
            "/api/admin",                # Admin routes — already protected by admin_required
            "/api/business/analyze/stream",  # SSE streaming — body buffering breaks it
        )
        if request.url.path.startswith(whitelist):
             return await call_next(request)

        # 2. Read and Buffer Body for Inspection
        body = b""
        if request.method in ["POST", "PUT", "PATCH"]:
            try:
                body = await request.body()
                # logger.info(f"FirewallMiddleware: Read {len(body)} bytes")
                # Attach to request state for FirewallManager to use
                request.state.body = body
                
                # Re-seed the stream so the endpoint can read the body once.
                # Must be stateful: Starlette's middleware calls receive() again
                # during response streaming and expects http.disconnect, not
                # another http.request — otherwise it raises RuntimeError.
                _consumed = False
                async def receive():
                    nonlocal _consumed
                    if _consumed:
                        return {"type": "http.disconnect"}
                    _consumed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                request._receive = receive
                
                # logger.info(f"Body buffered: {body[:50]}...")
            except Exception as e:
                logger.error(f"FirewallMiddleware Body Error: {e}")
                pass

        # 3. Check Firewall
        db = SessionLocal()
        try:
           # Pass request to manager
           block_result = await firewall_manager.process_request(request, db)
           if block_result:
               return JSONResponse(
                   status_code=403, 
                   content=block_result
               )
        except Exception as e:
            # log error but don't crash app
            logger.error(f"Firewall middleware error: {e}")
        finally:
            db.close()

        # 4. Proceed
        response = await call_next(request)
        return response



# Global firewall manager instance
firewall_manager = FirewallManager()


def initialize_default_firewall_rules(db: Session):
    """Initialize default firewall rules or update existing ones with latest patterns"""
    try:
        default_rules = [
            {
                'name': 'SQL Injection Protection',
                'type': 'waf_rule',
                'priority': 'high',
                'description': 'Detect and block SQL injection attempts in URL and Body',
                'rule_config': {
                    'pattern': r'(?:\bUNION\s+ALL\s+SELECT\b|\bUNION\s+SELECT\b|\bSELECT\b.*\bFROM\b|\bINSERT\b.*\bINTO\b|\bUPDATE\b.*\bSET\b|\bDELETE\b.*\bFROM\b|\bDROP\b.*\bTABLE\b|\' OR \'|\" OR \"|\' OR 1=1|\' OR \'1\'=\'1|OR 1=1|--|#|\/\*)'
                }
            },
            {
                'name': 'XSS Attack Protection',
                'type': 'waf_rule',
                'priority': 'high',
                'description': 'Detect and block cross-site scripting attempts',
                'rule_config': {
                    'pattern': r'(<script|javascript:|onerror=|onload=|<iframe)'
                }
            },
            {
                'name': 'Block Malicious Bots',
                'type': 'pattern_match',
                'priority': 'medium',
                'description': 'Block requests from known malicious bots',
                'rule_config': {
                    'blocked_user_agents': ['sqlmap', 'nikto', 'masscan', 'nmap']
                }
            }
        ]

        for rule in default_rules:
            # Check if rule exists by name
            existing = db.execute(text("SELECT id FROM firewall_rules WHERE name = :name"), {"name": rule['name']}).fetchone()
            
            if existing:
                # Update existing rule
                db.execute(text("""
                    UPDATE firewall_rules 
                    SET type = :type, 
                        priority = :priority, 
                        description = :desc, 
                        rule_config = :config,
                        updated_at = NOW()
                    WHERE name = :name
                """), {
                    'name': rule['name'],
                    'type': rule['type'],
                    'priority': rule['priority'],
                    'desc': rule['description'],
                    'config': json.dumps(rule['rule_config'])
                })
            else:
                # Insert new rule
                db.execute(text("""
                    INSERT INTO firewall_rules (name, type, is_active, priority, description, rule_config, hits)
                    VALUES (:name, :type, true, :priority, :desc, :config, 0)
                """), {
                    'name': rule['name'],
                    'type': rule['type'],
                    'priority': rule['priority'],
                    'desc': rule['description'],
                    'config': json.dumps(rule['rule_config'])
                })
        
        db.commit()
        logger.info("✓ Firewall rules synchronized successfully")
        
    except Exception as e:
        logger.error(f"Failed to initialize firewall rules: {e}")
        db.rollback()
