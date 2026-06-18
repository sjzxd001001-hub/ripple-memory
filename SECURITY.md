# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Ripple Memory, please report it privately via GitHub's [private vulnerability reporting](https://github.com/sjzxd001001-hub/ripple-memory/security/advisories/new).

Do not open a public issue for security vulnerabilities.

## What to Expect

- We will acknowledge your report within a few days.
- We will work with you to understand and fix the issue.
- Once fixed, we will credit you (unless you prefer to remain anonymous).

## Scope

Ripple Memory runs entirely on your local machine. It does not send data to any external server. The main security surface is:

- Local TCP sockets used for inter-process communication (bound to 127.0.0.1 only)
- File system access within the configured data directory — protect this directory with appropriate file permissions, as it contains your full memory content
- Third-party dependencies (mcp, sentence-transformers)
- Embedding model download from HuggingFace (`huggingface.co`) — this is the only outbound network request, occurring once during initial setup
