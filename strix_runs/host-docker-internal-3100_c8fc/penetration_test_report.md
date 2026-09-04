# Security Penetration Test Report

**Generated:** 2026-09-04 15:17:43 UTC

# Executive Summary

# Executive Summary

An external security assessment of **OWASP Juice Shop** (http://host.docker.internal:3100) identified **multiple critical and high-severity vulnerabilities** that enable unauthorized access to user data, account takeover, and privilege escalation.

## Overall Risk Posture
**CRITICAL** — Multiple confirmed vulnerabilities allow attackers to forge authentication tokens, access arbitrary user data, modify other users' accounts, and execute arbitrary JavaScript in victims' browsers.

## Key Findings

1. **Unsigned JWT Tokens (Critical)** — Application accepts JWT tokens with `alg: none`, allowing attackers to forge tokens for any user without a signature. Confirmed exploitation via `/api/Users` endpoint.

2. **Horizontal IDOR on User Profiles (High)** — Authenticated users can access any other user's profile data via `/api/Users/{id}` endpoint without authorization checks. Attackers enumerate user IDs to extract emails, roles, and personal metadata.

3. **Horizontal IDOR with Privilege Escalation on User Modification (High)** — PUT `/api/Users/{id}` endpoint lacks authorization checks. Attackers modify other users' email addresses and passwords, enabling full account takeover.

4. **Reflected DOM-based XSS in Search (High)** — `/search` endpoint reflects unsanitized user input directly into DOM. Attacker-crafted URLs execute arbitrary JavaScript in victim browsers without authentication.

## Business Impact
- **Confidentiality**: Complete exposure of all user accounts, email addresses, and profile data
- **Integrity**: Attackers can modify any user account (email, password) leading to account takeover
- **Availability**: Potential for mass account compromise and service disruption
- **Trust**: Compromised authentication system allows complete impersonation of any user including admins

## Immediate Actions Required
1. Disable JWT tokens with `alg: none` — enforce strong signature algorithms (RS256/HS256)
2. Implement proper authorization checks on all `/api/Users` endpoints
3. Sanitize user input before rendering in DOM; use DOMPurify or framework auto-escaping
4. Validate object ownership on all object-level operations before returning or modifying data

# Methodology

# Methodology

**Assessment Type:** Black-box external security testing

**Engagement Scope:** http://host.docker.internal:3100 (OWASP Juice Shop)

**Frameworks Applied:** OWASP Top 10, OWASP WSTG, common vulnerability research

**Testing Activities:**
- Comprehensive reconnaissance: 35+ API endpoints mapped, authentication model documented, attack surface enumerated
- SQL Injection testing on parameterized queries and search endpoints
- Cross-Site Scripting (XSS) testing on reflected and stored input vectors
- IDOR/Authorization testing on user-specific endpoints via ID enumeration
- Authentication and JWT validation testing including token forgery and algorithm confusion
- Business logic testing for price manipulation, workflow bypass, and race conditions

**Duration:** Approximately 15 minutes of active testing across 5 specialized vulnerability agents

**Constraints:** Assessment focused on highest-impact attack vectors within time and resource constraints.

# Technical Analysis

# Technical Analysis

## Vulnerability Summary

| Vulnerability | Severity | Impact | Status |
|--------------|----------|--------|--------|
| Unsigned JWT Tokens (alg: none) | Critical | Token forgery for any user | Confirmed |
| IDOR on /api/Users/{id} GET | High | Read all user profiles | Confirmed |
| IDOR on /api/Users/{id} PUT | High | Modify any user account | Confirmed |
| Reflected DOM XSS (/search) | High | Arbitrary script execution | Confirmed |
| Negative Quantity Validation | Medium | Potential price manipulation | Unresolved |
| Coupon Bypass | Medium | Discount manipulation | Unresolved |
| Basket IDOR | High | Cross-user basket access | Unresolved |

## Root Causes

### 1. Authentication Failure — Unsigned Tokens
The application accepts JWT tokens with `algorithm: none`, which disables signature verification. This stems from improper JWT validation in the authentication middleware. Attackers craft tokens by:
- Setting payload: `{"userId": "2", "role": "admin"}`
- Signing with `alg: none` (no signature required)
- Using the forged token on protected endpoints

**Confirmed Evidence:**
```
GET /api/Users/1
Header: Authorization: Bearer <forged-token-alg-none>
Response: 200 OK, User profile data returned
```

### 2. Object-Level Authorization Bypass — Missing Ownership Checks
The `/api/Users/{id}` endpoints perform no authorization checks before returning or modifying user data. The application trusts the client-supplied `userId` parameter without verifying the authenticated user owns that ID.

**Confirmed Evidence:**
```
Authenticated User 2 → PUT /api/Users/1 with new email
Response: 200 OK, User 1's email modified
No ownership validation before update
```

### 3. DOM-based XSS — Unescaped User Input
The search component directly injects the `q` query parameter into the DOM without HTML encoding. Angular template variables are rendered without escaping.

**Confirmed Evidence:**
```
GET /search?q=<img src=x onerror=alert('XSS')>
Result: <span id="searchValue"><img src=x onerror=alert('XSS')></span>
Script executes in victim's browser
```

## Systemic Issues

1. **Weak Input Validation Framework** — Application lacks centralized input validation; each endpoint reimplements checks inconsistently
2. **Authorization Gaps** — No consistent authorization decorator or middleware checking resource ownership
3. **Missing Security Headers** — No CSP to prevent XSS, no strict token validation
4. **Insufficient JWT Validation** — Algorithm not restricted to secure options (RS256/HS256)

# Recommendations

# Recommendations

## Immediate (Critical Priority)

**1. Disable Unsigned JWT Tokens**
- Update JWT validation to reject `alg: none` and only accept RS256 or HS256
- Enforce signature verification on all protected endpoints
- Invalidate all existing tokens and force re-authentication

**2. Implement Object-Level Authorization Checks**
- Add ownership validation before returning user data: verify `authenticated_user_id == requested_user_id` or `user.role == admin`
- Deny all cross-user access by default; require explicit role-based permission
- Apply to all endpoints: GET/PUT/DELETE `/api/Users/{id}`, `/api/Orders/{id}`, `/api/Cards/{id}`, `/api/Baskets/{id}`

**3. Sanitize DOM Output**
- Use HTML entity encoding on all user input reflected in the DOM
- Enable Angular's auto-escaping on all templates
- Implement Content Security Policy (CSP) header: `default-src 'self'; script-src 'self'`

## Short-term (High Priority)

**4. Add Input Validation Layer**
- Validate email format before accepting user modifications
- Restrict quantity/price fields to positive integers
- Whitelist allowed values for state/status fields

**5. Implement Authorization Middleware**
- Create centralized authorization decorator/middleware checking user roles and resource ownership
- Log all authorization failures for audit trails
- Test all endpoints for IDOR before deployment

**6. Security Testing**
- Add unit/integration tests validating authorization on all endpoints
- Implement SAST scanning for common injection patterns
- Perform annual penetration testing

## Validation

After applying fixes:
1. Attempt to forge JWT tokens with `alg: none` — should be rejected
2. Try accessing `/api/Users/{other_user_id}` as authenticated user — should return 403 Forbidden
3. Inject XSS payload in search — script execution should be blocked by CSP or HTML encoding
4. Verify all tests pass; retest findings to confirm closure

