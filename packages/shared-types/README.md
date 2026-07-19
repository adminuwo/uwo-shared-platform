# @uwo/shared-types

Canonical shared TypeScript contracts for the UWO shared platform.

This package provides:

- Branded ID types for strong nominal typing (UserId, TenantId, etc.)
- API envelope types (ApiSuccess, ApiError, PaginatedResponse)
- Light-weight interfaces for identity, tenant, entitlement, and audits

Guidelines
- This package contains types only; there is intentionally no runtime business logic.
- Do not include secrets, passwords, tokens, or raw sensitive payloads in audit details or other fields.
- Build and typecheck this package via the workspace root commands (turbo/pnpm). Each package includes its own build/typecheck scripts.
