import { TenantId, OrganisationId, WorkspaceId } from './ids';

export interface Tenant {
  id: TenantId;
  organisationId?: OrganisationId;
  name: string;
  displayName?: string;
  // metadata can contain non-sensitive configuration. Do not store secrets here.
  metadata?: Record<string, unknown>;
}

export interface TenantSettings {
  tenantId: TenantId;
  // feature flags, limits, and other tenant-scoped configuration
  featureFlags?: Record<string, boolean>;
  limits?: Record<string, number>;
}
