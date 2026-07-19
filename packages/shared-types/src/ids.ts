// Canonical branded identifier types (nominal typing via branding)
export type Brand<K, T> = K & { readonly __brand: T };

export type UserId = Brand<string, 'UserId'>;
export type OrganisationId = Brand<string, 'OrganisationId'>;
export type TenantId = Brand<string, 'TenantId'>;
export type WorkspaceId = Brand<string, 'WorkspaceId'>;
export type ProductId = Brand<string, 'ProductId'>;
export type SubscriptionId = Brand<string, 'SubscriptionId'>;
export type AuditEventId = Brand<string, 'AuditEventId'>;
export type TraceId = Brand<string, 'TraceId'>;
