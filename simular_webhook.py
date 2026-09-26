import hashlib
import hmac
import json
import urllib.request

SECRET = "segredo-teste-123"  # mesmo valor do GITHUB_WEBHOOK_SECRET
URL = "http://localhost:8000/webhooks/github"

payload = {
    "ref": "refs/heads/main",
    "before": "0000000000000000000000000000000000000000",
    "after": "945f81b2cc5e802d287e54fa07be1f48fc1850f9",
    "repository": {
        "full_name": "itsAntonioDev/Orquestrador",
        "clone_url": "https://github.com/itsAntonioDev/Orquestrador.git",
        "html_url": "https://github.com/itsAntonioDev/Orquestrador",
        "default_branch": "main",
    },
    "pusher": {"name": "itsAntonioDev"},
    "head_commit": {
        "id": "945f81b2cc5e802d287e54fa07be1f48fc1850f9",
        "message": "demo para linkedin",
        "author": {"name": "itsAntonioDev"},
    },
}

body = json.dumps(payload).encode("utf-8")
signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

req = urllib.request.Request(
    URL,
    data=body,
    headers={
        "Content-Type": "application/json",
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": signature,
    },
)

try:
    with urllib.request.urlopen(req) as resp:
        print(resp.status, resp.read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
