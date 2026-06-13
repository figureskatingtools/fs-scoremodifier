// Zero-dependency Node.js server for Azure App Service
// Serves static files, provides /userinfo endpoint, and proxies /api/* to Function App
const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');

const PORT = process.env.PORT || 8080;
const FUNCTION_APP_URL = process.env.FUNCTION_APP_URL || '';
const PUBLIC_DIR = path.join(__dirname, 'public');

const MIME_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.ico': 'image/x-icon',
    '.svg': 'image/svg+xml',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
    '.ttf': 'font/ttf',
    '.map': 'application/json',
};

// ── /userinfo endpoint ──
// Reads X-MS-CLIENT-PRINCIPAL header set by Easy Auth (no tokens exposed)
function handleUserInfo(req, res) {
    const principal = req.headers['x-ms-client-principal'];
    const principalName = req.headers['x-ms-client-principal-name'];

    if (!principal && !principalName) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ authenticated: false }));
        return;
    }

    let userInfo = { authenticated: true, userDetails: principalName || 'unknown' };

    if (principal) {
        try {
            const decoded = Buffer.from(principal, 'base64').toString('utf-8');
            const parsed = JSON.parse(decoded);
            
            // Extract display name from claims if available
            let displayName = parsed.userDetails || '';
            const claims = parsed.claims || [];
            if (Array.isArray(claims)) {
                const nameClaim = claims.find(c => 
                    c.typ === 'name' || 
                    c.typ === 'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name'
                );
                const emailClaim = claims.find(c => 
                    c.typ === 'preferred_username' || 
                    c.typ === 'email' ||
                    c.typ === 'emails' ||
                    c.typ === 'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress'
                );
                // Prefer email, then name, then userDetails, then principalName
                displayName = (emailClaim && emailClaim.val) || 
                              (nameClaim && nameClaim.val) || 
                              parsed.userDetails || 
                              principalName || 
                              'unknown';
            }
            
            userInfo = {
                authenticated: true,
                userId: parsed.userId,
                identityProvider: parsed.identityProvider,
                userDetails: displayName || principalName || 'unknown',
                userRoles: parsed.userRoles || [],
            };
        } catch (e) {
            // parsing failed, use principalName fallback
        }
    }

    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(userInfo));
}

// ── Proxy /api/* to Function App ──
// Forwards Easy Auth identity headers; never exposes tokens to the client
function proxyToFunctionApp(req, res) {
    if (!FUNCTION_APP_URL) {
        res.writeHead(502, { 'Content-Type': 'text/plain' });
        res.end('FUNCTION_APP_URL not configured');
        return;
    }

    let targetUrl;
    try {
        targetUrl = new URL(req.url, FUNCTION_APP_URL);
    } catch (e) {
        res.writeHead(400, { 'Content-Type': 'text/plain' });
        res.end('Bad Request');
        return;
    }

    // Build outbound headers — forward user identity via custom header
    // (Easy Auth on Function App strips X-MS-CLIENT-PRINCIPAL from external requests)
    const outHeaders = {
        'host': targetUrl.host,
        'content-type': req.headers['content-type'] || '',
        'accept': req.headers['accept'] || '*/*',
    };

    // Extract user email from Easy Auth and pass via custom header
    const principalHeader = req.headers['x-ms-client-principal'];
    const principalName = req.headers['x-ms-client-principal-name'];
    
    // Shared secret proving this request came from the proxy (the Function App
    // is public; this stops direct callers from spoofing the email header).
    if (process.env.PROXY_SHARED_SECRET) {
        outHeaders['x-proxy-secret'] = process.env.PROXY_SHARED_SECRET;
    }

    if (principalName) {
        outHeaders['x-forwarded-user-email'] = principalName;
    }
    if (principalHeader) {
        try {
            const decoded = Buffer.from(principalHeader, 'base64').toString('utf-8');
            const parsed = JSON.parse(decoded);
            if (parsed.userDetails) {
                outHeaders['x-forwarded-user-email'] = parsed.userDetails;
            }
        } catch (_e) { /* ignore parse errors */ }
    }

    // Forward content-length if present
    if (req.headers['content-length']) {
        outHeaders['content-length'] = req.headers['content-length'];
    }

    const options = {
        hostname: targetUrl.hostname,
        port: 443,
        path: targetUrl.pathname + (targetUrl.search || ''),
        method: req.method,
        headers: outHeaders,
    };

    const proxyReq = https.request(options, (proxyRes) => {
        // Forward CORS and other response headers
        const respHeaders = {};
        for (const [key, val] of Object.entries(proxyRes.headers)) {
            // Skip hop-by-hop headers
            if (['connection', 'transfer-encoding', 'keep-alive'].includes(key.toLowerCase())) continue;
            respHeaders[key] = val;
        }
        res.writeHead(proxyRes.statusCode, respHeaders);
        proxyRes.pipe(res);
    });

    proxyReq.on('error', (e) => {
        console.error('Proxy error:', e.message);
        if (!res.headersSent) {
            res.writeHead(502, { 'Content-Type': 'text/plain' });
            res.end('Bad Gateway');
        }
    });

    req.pipe(proxyReq);
}

// ── Static file serving with SPA fallback ──
// Vite emits content-hashed filenames under /assets/, so those can be cached
// forever; index.html must always be revalidated or browsers keep serving a
// stale page that references old (deleted) bundles after a deploy.
function cacheControlFor(urlPath) {
    return urlPath.startsWith('/assets/')
        ? 'public, max-age=31536000, immutable'
        : 'no-cache';
}

function serveStatic(req, res) {
    const urlPath = decodeURIComponent(req.url.split('?')[0]);
    let filePath = path.join(PUBLIC_DIR, urlPath === '/' ? 'index.html' : urlPath);

    // Security: prevent path traversal
    if (!filePath.startsWith(PUBLIC_DIR)) {
        res.writeHead(403);
        res.end('Forbidden');
        return;
    }

    fs.stat(filePath, (err, stats) => {
        if (!err && stats.isFile()) {
            const ext = path.extname(filePath);
            const mime = MIME_TYPES[ext] || 'application/octet-stream';
            res.writeHead(200, { 'Content-Type': mime, 'Cache-Control': cacheControlFor(urlPath) });
            fs.createReadStream(filePath).pipe(res);
        } else {
            // SPA fallback: serve index.html for any unmatched route
            const indexPath = path.join(PUBLIC_DIR, 'index.html');
            fs.stat(indexPath, (err2) => {
                if (err2) {
                    res.writeHead(404);
                    res.end('Not Found');
                    return;
                }
                res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-cache' });
                fs.createReadStream(indexPath).pipe(res);
            });
        }
    });
}

// ── Security Headers ──
const SECURITY_HEADERS = {
    'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'",
};

// ── Request router ──
const server = http.createServer((req, res) => {
    const pathname = req.url.split('?')[0];

    // Apply security headers to all responses
    for (const [key, val] of Object.entries(SECURITY_HEADERS)) {
        res.setHeader(key, val);
    }

    if (pathname === '/userinfo') {
        handleUserInfo(req, res);
    } else if (pathname.startsWith('/api/')) {
        proxyToFunctionApp(req, res);
    } else {
        serveStatic(req, res);
    }
});

server.listen(PORT, () => {
    console.log(`Server listening on port ${PORT}`);
});
