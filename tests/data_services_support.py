from dataclasses import dataclass
from packages.contracts import VerifiedSubjectIdentity
from services.data_service_common import DataServiceAuthorizer,MemoryAuditSink
from services.platform_control_plane.authorization import ControlPlaneAuthorizer
from control_plane_support import ADMIN_A,PLATFORM,NOW,bootstrap_tenant_admin,make_fixture

@dataclass
class DataContext:
    control:object
    authorizer:DataServiceAuthorizer
    audit:MemoryAuditSink

def make_data_context()->DataContext:
    control=make_fixture();bootstrap_tenant_admin(control,"tenant-a",ADMIN_A);bootstrap_tenant_admin(control,"tenant-b",VerifiedSubjectIdentity("admin-b","tenant-b",NOW))
    cp=ControlPlaneAuthorizer(control.tenants,control.memberships,control.roles,control.subjects,frozenset({PLATFORM.subject}))
    return DataContext(control,DataServiceAuthorizer(control.tenants,control.subjects,cp,frozenset({PLATFORM.subject})),MemoryAuditSink())
