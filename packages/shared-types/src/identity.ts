import { UserId, OrganisationId, TenantId, WorkspaceId } from './ids';

export interface UserIdentity {
  id: UserId;
  username?: string;
  displayName?: string;
  email?: string; // avoid including password or other credentials here
  avatarUrl?: string;
  locale?: string;
}

export interface OrganisationSummary {
  id: OrganisationId;
  name: string;
  displayName?: string;
}

export interface WorkspaceSummary {
  id: WorkspaceId;
  name: string;
  tenantId?: TenantId;
}
