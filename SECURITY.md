# Security policy

## Reporting a vulnerability

Report privately through GitHub: **[open a security advisory](https://github.com/metacognitionai/zeos_demos/security/advisories/new)**.
Do not open a public issue for a vulnerability.

Please include what you did and what you expected, and the kernel's journal if you have
one: every demonstration writes one, and it records what the kernel actually permitted.

## Supported versions

The demonstrations track `main` and have no releases. Fixes go onto `main`.

## What is in scope

A demonstration is a driver around the ZEOS kernel: the adapters that turn a browser
message or a model's answer into a pipe write, and a pipe write into an effect. The
kernel decides what an effect is allowed to do; the driver must not decide otherwise
behind its back. So, in scope:

- **The driver laundering provenance.** Content the kernel refused to let out reaching
  an effect by another route through the driver, for example findings a job could not
  mail arriving in the outbox through the page.
- **Secrets.** An API key or mail password read from `.env` reaching a page, a letter, a
  journal or a log.
- **Effects beyond what was configured.** Mail leaving the machine when the transport
  or `MAIL_LIVE` says it should not, or reaching an address other than the one set.

## What is not

- **The kernel's own guarantees.** Ring and capability enforcement, the integrity
  watermark, determinism and descriptor loading belong to
  [ZEOS](https://github.com/metacognitionai/zeos) and are reported through
  [its policy](https://github.com/metacognitionai/zeos/blob/main/SECURITY.md).
- **A model doing what a prompt asked.** The demonstrations show a model *reading* an
  injected page and the kernel refusing the *effect*. A prompt that changes what a model
  says is not a finding; one that makes an effect happen is.
- **A demonstration's server bound to a non-loopback address.** They serve to
  `127.0.0.1` and are not built to be exposed to a network.
