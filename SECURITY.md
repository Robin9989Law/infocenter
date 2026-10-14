# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.4.x   | ✅        |
| < 0.4   | ❌        |

## Security Model

InfoCenter is a **localhost-only** message hub. Understanding its trust boundary is essential before deploying.

### What is protected

- **Network isolation**: The HTTP server binds exclusively to `127.0.0.1:8787`. It does not listen on any LAN or public interface.
- **Single-user trust**: The trust boundary is your OS user account. Any process running as the same user can read, write, or impersonate any agent.

### What is NOT protected

- **No authentication**: There is no auth token, no TLS, no per-agent credentials. Any local process that can reach `127.0.0.1:8787` may send messages as any agent or read any inbox.
- **No encryption at rest**: Messages are stored as plaintext in `~/.infocenter/mailbox.db` (SQLite). Do not send secrets through InfoCenter.
- **No sandboxing**: MCP processes, headless daemons, and the HTTP server all run with your full user privileges.

### Do not

- Expose port 8787 to the network via port forwarding, reverse proxy, or tunnel.
- Pass passwords, API keys, tokens, or other secrets as message bodies.
- Run InfoCenter on a multi-user shared system where other users are untrusted.

## Reporting a Vulnerability

For security issues, please open a GitHub issue with the `security` label. Since this is a single-user localhost tool, most "vulnerabilities" are design constraints documented above rather than bugs.
