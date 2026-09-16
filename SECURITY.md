# Security Policy

## Supported branch

Security fixes are made against the default branch while the project remains in Beta.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability or attach sensitive reproduction data. Use GitHub's private vulnerability reporting for this repository when it is enabled, or contact the maintainer through the repository owner's published contact channel.

Include a minimal reproduction, affected version, Windows version, and impact. Remove notes, attachments, tokens, personal data, and proprietary material before sending.

## Security boundaries

Pocket Memory is designed to run its local web service on loopback addresses only. It is not designed as a multi-user server or an internet-facing service. Do not expose its local port through a proxy, port forwarding, or a public network without a separate authentication and security design review.
