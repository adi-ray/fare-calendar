# Security policy

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately through
[GitHub security advisories](https://github.com/adi-ray/fare-calendar/security/advisories/new).
You'll get an acknowledgement within 3 working days and a fix or mitigation plan within 14.

## Supported versions

| Version | Supported |
|---|---|
| 1.x | ✅ |
| < 1.0 | ❌ |

## How the project handles sensitive data

- **Fares stay where the logs live.** Real search logs, extracted observations and real forecasts are git-ignored and never leave the operator's machine.
- **Supplier mappings stay private.** Mappings for private supplier APIs are git-ignored; only the public Amadeus example is in the repo.
- **Gemma runs locally.** It reads samples of supplier responses through Ollama on the operator's machine, never a hosted API.
- **The deployed API holds only aggregated forecasts.** It never holds logs, passenger data or credentials for suppliers.
- **Access control:**
  - Agents sign in on `/login` and get a signed, HttpOnly, SameSite=Lax session cookie that expires after 12 hours.
  - Portals embed the calendar with an API key.
  - Uploads require a separate admin token.
  - Failed sign-ins are slowed down. Redirects after sign-in are restricted to the same site.
- **Rotation:** changing `FARECAL_BASIC_PASS`, or setting `FARECAL_SESSION_SECRET`, signs every agent out.
