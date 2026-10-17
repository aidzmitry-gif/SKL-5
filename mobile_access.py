"""Device-bound proof and fresh scope checks for the small WMS mobile facade."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import jwt
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.services.auth import (
    CurrentUser,
    EffectiveIdentityLookupError,
    resolve_effective_oidc_user,
)
from modules.wms.mobile_models import MobileDevice, MobileGrant, MobileProofUse

PROOF_HEADER = "X-WMS-Proof"
PROOF_TYP = "WMS-MOBILE-PROOF"
PROOF_MAX_AGE_SECONDS = 90
PROOF_FUTURE_SKEW_SECONDS = 5
_JTI = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


@dataclass(frozen=True)
class VerifiedProof:
    device_id: str
    device_revision: int
    grant_id: str | None
    grant_revision: int | None
    original_user_id: int | None = None
    original_sub: str | None = None
    issued_at: int | None = None


MobileMetadataAction = Literal[
    "item_identity_ensure", "item_label_prepare", "item_label_verify",
    "receiving_context",
]
_MOBILE_METADATA_ACTIONS = frozenset({
    "item_identity_ensure", "item_label_prepare", "item_label_verify",
})


@dataclass(frozen=True)
class OriginalMobileReceivingSourceAuthority:
    original_user_id: int
    original_sub: str
    bearer_issued_at: int
    roles: tuple[str, ...]
    organization_id: int
    warehouse_id: int | None


@dataclass(frozen=True)
class OriginalMobileTransferAuthority:
    identity: OriginalMobileReceivingSourceAuthority
    action: Literal["transfer_dispatch", "transfer_receive", "transfer_discrepancy_report"]
    proof: VerifiedProof


@dataclass(frozen=True)
class OriginalMobileFulfillmentSourceAuthority:
    identity: OriginalMobileReceivingSourceAuthority
    purpose: Literal["state", "pack"]
    proof: VerifiedProof | None = None


def capture_native_mobile_fulfillment_source_authority(
    principal: CurrentUser, organization_id: int, warehouse_id: int, *,
    purpose: Literal["state", "pack"],
) -> OriginalMobileFulfillmentSourceAuthority:
    """Freeze the managed bearer and explicit warehouse before proof awaits."""
    if purpose not in {"state", "pack"}:
        from core.services.logistics import MobileFulfillmentAuthorityDenied

        raise MobileFulfillmentAuthorityDenied()
    return OriginalMobileFulfillmentSourceAuthority(
        capture_native_mobile_receiving_source_authority(principal, organization_id, warehouse_id),
        purpose,
    )


async def revalidate_mobile_fulfillment_source_authority(
    session, core, principal: CurrentUser, authority: OriginalMobileFulfillmentSourceAuthority,
    exact_invoice: dict | None,
):
    """Recheck the original mobile bearer, exact purpose grant and warehouse."""
    from core.services.logistics import MobileFulfillmentAuthorityDenied
    from core.services.config import get_settings
    from modules.wms.models import WarehouseDirectory
    from modules.wms.physical_stock import control

    identity = authority.identity if isinstance(authority, OriginalMobileFulfillmentSourceAuthority) else None
    proof = authority.proof if isinstance(authority, OriginalMobileFulfillmentSourceAuthority) else None
    purpose = authority.purpose if isinstance(authority, OriginalMobileFulfillmentSourceAuthority) else None
    if (
        not isinstance(identity, OriginalMobileReceivingSourceAuthority)
        or purpose not in {"state", "pack"}
        or not isinstance(proof, VerifiedProof)
        or type(identity.organization_id) is not int or identity.organization_id < 1
        or type(identity.warehouse_id) is not int or identity.warehouse_id < 1
        or type(identity.original_user_id) is not int or identity.original_user_id < 1
        or not isinstance(identity.original_sub, str) or not identity.original_sub
        or type(identity.bearer_issued_at) is not int
        or identity.roles != ("wms_mobile",)
        or exact_invoice is not None and (
            type(exact_invoice) is not dict
            or exact_invoice.get("organization_id") != identity.organization_id
        )
        or proof.grant_id is None or type(proof.grant_revision) is not int
        or type(proof.issued_at) is not int
        or type(proof.original_user_id) is not int
        or proof.original_user_id != identity.original_user_id
        or type(proof.original_sub) is not str
        or proof.original_sub != identity.original_sub
        or type(principal.local_user_id) is not int
        or principal.local_user_id != identity.original_user_id
        or type(principal.keycloak_user_id) is not str
        or principal.keycloak_user_id != identity.original_sub
        or type(principal.issued_at) is not int
        or principal.issued_at != identity.bearer_issued_at
        or not isinstance(principal.roles, list)
        or tuple(principal.roles) != identity.roles
        or get_settings().auth_mode != "oidc"
    ):
        raise MobileFulfillmentAuthorityDenied()
    try:
        device = await _read_device(session, proof.device_id)
        fresh = await fresh_mobile_identity(session, _native_mobile_receiving_source_seed(identity))
    except HTTPException as exc:
        if exc.status_code in {401, 403, 404}:
            raise MobileFulfillmentAuthorityDenied(exc.status_code) from exc
        raise
    if (
        not _native_mobile_receiving_source_identity(core, fresh, identity)
        or device.owner_sub != identity.original_sub
        or device.state != "active" or device.revision != proof.device_revision
        or not _proof_issue_time_is_current(proof.issued_at)
    ):
        raise MobileFulfillmentAuthorityDenied()
    try:
        actor = await core.services.accounting.source_member(session, identity.organization_id, fresh)
    except HTTPException as exc:
        if exc.status_code in {401, 403, 404}:
            raise MobileFulfillmentAuthorityDenied(exc.status_code) from exc
        raise
    if actor != identity.original_sub:
        raise MobileFulfillmentAuthorityDenied()
    try:
        await _lock_mobile_metadata_owner(session, identity.original_user_id, identity.original_sub, fresh)
    except HTTPException as exc:
        if exc.status_code in {401, 403, 404}:
            raise MobileFulfillmentAuthorityDenied(exc.status_code) from exc
        raise
    grant = await session.scalar(select(MobileGrant).where(
        MobileGrant.id == proof.grant_id,
        MobileGrant.device_id == device.id,
        MobileGrant.organization_id == identity.organization_id,
        MobileGrant.warehouse_id == identity.warehouse_id,
        MobileGrant.state == "active",
    ).with_for_update().execution_options(populate_existing=True))
    required_actions = {"state"} if purpose == "state" else {"pack", "receipt"}
    try:
        purpose_allowed = grant is not None and required_actions.issubset(set(grant.actions))
    except TypeError:
        purpose_allowed = False
    if not purpose_allowed or grant.revision != proof.grant_revision:
        raise MobileFulfillmentAuthorityDenied()
    directory = await session.scalar(select(WarehouseDirectory).where(
        WarehouseDirectory.id == identity.warehouse_id,
        WarehouseDirectory.organization_id == identity.organization_id,
        WarehouseDirectory.is_active.is_(True),
    ).execution_options(populate_existing=True))
    if directory is None:
        raise MobileFulfillmentAuthorityDenied(404)
    try:
        await control(session, identity.warehouse_id, identity.organization_id, lock=False)
        final_fresh = await fresh_mobile_identity(session, _native_mobile_receiving_source_seed(identity))
    except HTTPException as exc:
        if exc.status_code in {401, 403, 404}:
            raise MobileFulfillmentAuthorityDenied(exc.status_code) from exc
        raise
    if (
        not _native_mobile_receiving_source_identity(core, final_fresh, identity)
        or not _proof_issue_time_is_current(proof.issued_at)
    ):
        raise MobileFulfillmentAuthorityDenied()
    return device, grant, final_fresh, actor


def make_mobile_fulfillment_read_capability(
    session, stock, authority, *, warehouse_id: int,
    binding_revision_id: str, exact_invoice: dict,
):
    from core.services.logistics import MobileFulfillmentReadCapability

    core = getattr(stock, "core", None)
    principal = getattr(stock, "user", None)
    identity = authority.identity if isinstance(authority, OriginalMobileFulfillmentSourceAuthority) else None
    transaction = session.get_transaction()
    if (
        not isinstance(identity, OriginalMobileReceivingSourceAuthority)
        or not isinstance(authority, OriginalMobileFulfillmentSourceAuthority)
        or authority.purpose not in {"state", "pack"}
        or core is None or getattr(stock, "services", None) is not core.services
        or not isinstance(principal, CurrentUser)
        or type(principal.local_user_id) is not int
        or principal.local_user_id != identity.original_user_id
        or type(principal.keycloak_user_id) is not str
        or principal.keycloak_user_id != identity.original_sub
        or type(principal.issued_at) is not int
        or principal.issued_at != identity.bearer_issued_at
        or not isinstance(principal.roles, list)
        or tuple(principal.roles) != identity.roles
        or authority.proof is None or transaction is None or not transaction.is_active
        or session.in_nested_transaction()
        or type(warehouse_id) is not int or warehouse_id != identity.warehouse_id
        or type(exact_invoice) is not dict
        or set(exact_invoice) != {
            "organization_id", "document_id", "expected_version", "expected_content_sha256",
        }
        or type(exact_invoice.get("organization_id")) is not int
        or exact_invoice.get("organization_id") != identity.organization_id
        or type(exact_invoice.get("document_id")) is not int or exact_invoice["document_id"] < 1
        or type(exact_invoice.get("expected_version")) is not int or exact_invoice["expected_version"] < 1
        or not isinstance(exact_invoice.get("expected_content_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", exact_invoice["expected_content_sha256"]) is None
        or type(binding_revision_id) is not str or not binding_revision_id
    ):
        from core.services.logistics import MobileFulfillmentAuthorityDenied

        raise MobileFulfillmentAuthorityDenied()

    async def revalidate(original_session, current_user, current_invoice):
        if original_session is not session or current_invoice != exact_invoice:
            from core.services.logistics import MobileFulfillmentAuthorityDenied

            raise MobileFulfillmentAuthorityDenied()
        _, _, fresh, _ = await revalidate_mobile_fulfillment_source_authority(
            original_session, core, current_user, authority, current_invoice,
        )
        return fresh

    revalidate.mobile_fulfillment_scope = (
        identity.organization_id, identity.warehouse_id, binding_revision_id,
        exact_invoice["document_id"], exact_invoice["expected_version"],
        exact_invoice["expected_content_sha256"],
    )

    return MobileFulfillmentReadCapability(
        organization_id=identity.organization_id,
        warehouse_id=identity.warehouse_id,
        binding_revision_id=binding_revision_id,
        document_id=exact_invoice["document_id"],
        expected_version=exact_invoice["expected_version"],
        expected_content_sha256=exact_invoice["expected_content_sha256"],
        original_session_reference=session,
        original_root_transaction_reference=transaction,
        typed_async_revalidate_callback=revalidate,
    )


async def revalidate_mobile_transfer_authority(session, core, authority, *, receipt=False):
    """Keep transfer device/org/User/grant locks tied to the original bearer."""
    from core.services.config import get_settings

    if not isinstance(authority, OriginalMobileTransferAuthority):
        _deny()
    identity, proof = authority.identity, authority.proof
    if (
        not isinstance(identity, OriginalMobileReceivingSourceAuthority)
        or not isinstance(proof, VerifiedProof)
        or authority.action not in {
            "transfer_dispatch", "transfer_receive", "transfer_discrepancy_report",
        }
        or type(identity.warehouse_id) is not int or identity.warehouse_id < 1
        or (
            (proof.original_user_id != identity.original_user_id
             or proof.original_sub != identity.original_sub)
            if authority.action == "transfer_discrepancy_report"
            else (proof.original_user_id not in {None, identity.original_user_id}
                  or proof.original_sub not in {None, identity.original_sub})
        )
        or proof.grant_id is None or proof.grant_revision is None
        or type(proof.issued_at) is not int
        or get_settings().auth_mode != "oidc"
    ):
        _deny()
    device = await _read_device(session, proof.device_id)
    fresh = await fresh_mobile_identity(session, _native_mobile_receiving_source_seed(identity))
    if (
        not _native_mobile_receiving_source_identity(core, fresh, identity)
        or device.owner_sub != identity.original_sub
        or device.state != "active" or device.revision != proof.device_revision
        or not _proof_issue_time_is_current(proof.issued_at)
    ):
        _deny()
    actor = await core.services.accounting.source_member(session, identity.organization_id, fresh)
    if actor != identity.original_sub:
        _deny()
    await _lock_mobile_metadata_owner(session, identity.original_user_id, identity.original_sub, fresh)
    grant = await session.scalar(select(MobileGrant).where(
        MobileGrant.id == proof.grant_id,
        MobileGrant.device_id == device.id,
        MobileGrant.organization_id == identity.organization_id,
        MobileGrant.warehouse_id == identity.warehouse_id,
        MobileGrant.state == "active",
    ).with_for_update().execution_options(populate_existing=True))
    required = {authority.action, "receipt"} if receipt else {authority.action}
    if (grant is None or grant.revision != proof.grant_revision
            or not required.issubset(set(grant.actions))):
        _deny()
    final_fresh = await fresh_mobile_identity(session, _native_mobile_receiving_source_seed(identity))
    if (not _native_mobile_receiving_source_identity(core, final_fresh, identity)
            or not _proof_issue_time_is_current(proof.issued_at)):
        _deny()
    return device, grant, final_fresh, actor


def capture_native_mobile_receiving_source_authority(
    principal: CurrentUser, organization_id: int, warehouse_id: int | None,
) -> OriginalMobileReceivingSourceAuthority:
    """Freeze bearer identity before phase proof reads the request body."""
    from core.services.config import get_settings

    roles = principal.roles
    if (
        get_settings().auth_mode != "oidc"
        or type(principal.local_user_id) is not int or principal.local_user_id < 1
        or not isinstance(principal.keycloak_user_id, str) or not principal.keycloak_user_id
        or type(principal.issued_at) is not int
        or not isinstance(roles, list) or tuple(roles) != ("wms_mobile",)
        or any(type(value) is not int or not 1 <= value <= 2147483647
            for value in ((organization_id,) if warehouse_id is None else (organization_id, warehouse_id)))
    ):
        _deny()
    return OriginalMobileReceivingSourceAuthority(
        principal.local_user_id, principal.keycloak_user_id, principal.issued_at,
        tuple(roles), organization_id, warehouse_id,
    )


def _native_mobile_receiving_source_identity(core, fresh, authority):
    from core.services.auth import has_permission

    return (
        isinstance(authority, OriginalMobileReceivingSourceAuthority)
        and fresh.local_user_id == authority.original_user_id
        and fresh.keycloak_user_id == authority.original_sub
        and fresh.issued_at == authority.bearer_issued_at
        and tuple(fresh.roles) == authority.roles == ("wms_mobile",)
        and fresh.local_status == "active"
        and has_permission(core, fresh, "wms.mobile")
    )


def _native_mobile_receiving_source_seed(authority: OriginalMobileReceivingSourceAuthority) -> CurrentUser:
    """Build fresh lookup input only from authority frozen before awaits."""
    if not isinstance(authority, OriginalMobileReceivingSourceAuthority):
        _deny()
    return CurrentUser(
        authority.original_sub, list(authority.roles),
        keycloak_user_id=authority.original_sub, local_status=None,
        issued_at=authority.bearer_issued_at, local_user_id=authority.original_user_id,
    )

@dataclass(frozen=True)
class OriginalMobileMetadataAuthority:
    original_user_id: int
    original_sub: str
    verified_proof: VerifiedProof
    organization_id: int
    warehouse_id: int
    action: MobileMetadataAction


def _deny(status: int = 403) -> None:
    raise HTTPException(status_code=status, detail="Мобильный доступ не подтверждён")


def _proof_issue_time_is_current(issued_at: Any) -> bool:
    now = int(time.time())
    return (
        type(issued_at) is int
        and issued_at <= now + PROOF_FUTURE_SKEW_SECONDS
        and now - issued_at <= PROOF_MAX_AGE_SECONDS
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _thumbprint(jwk: dict[str, Any]) -> str:
    _validate_public_jwk(jwk)
    canonical = json.dumps(
        {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return _sha256(canonical)


def _validate_public_jwk(jwk: dict[str, Any]) -> None:
    if set(jwk) != {"kty", "crv", "x", "y"} or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        _deny()
    for coordinate in (jwk.get("x"), jwk.get("y")):
        if not isinstance(coordinate, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", coordinate):
            _deny()
        try:
            decoded = base64.urlsafe_b64decode(coordinate + "=")
        except Exception:
            _deny()
        canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
        if len(decoded) != 32 or canonical != coordinate:
            _deny()


def _public_key(jwk: dict[str, Any]):
    _validate_public_jwk(jwk)
    try:
        return jwt.PyJWK.from_dict(jwk, algorithm="ES256").key
    except Exception as exc:
        raise HTTPException(status_code=403, detail="Мобильный доступ не подтверждён") from exc


def _request_binding(request: Request, body: bytes, bearer: str) -> dict[str, Any]:
    method = request.method.upper()
    path = request.scope.get("path")
    raw_path = request.scope.get("raw_path")
    query = request.scope.get("query_string", b"")
    if not isinstance(path, str) or not path.isascii() or not isinstance(raw_path, bytes):
        _deny()
    if raw_path != path.encode("ascii") or not isinstance(query, bytes):
        _deny()
    return {
        "ath": _sha256(bearer.encode("utf-8")),
        "htm": method,
        "htu": path,
        "qsh": _sha256(query),
        "bsh": _sha256(body),
    }


def _decode_proof(token: str, key, request: Request, body: bytes, principal: CurrentUser) -> tuple[dict, str]:
    try:
        header = jwt.get_unverified_header(token)
        if header != {"alg": "ES256", "typ": PROOF_TYP}:
            _deny()
        claims = jwt.decode(
            token,
            key,
            algorithms=["ES256"],
            options={"verify_aud": False, "verify_iss": False, "require": []},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=403, detail="Мобильный доступ не подтверждён") from exc

    required = {"v", "sub", "ath", "iat", "jti", "htm", "htu", "qsh", "bsh", "device_id", "device_revision"}
    optional = {"grant_id", "grant_revision"}
    if set(claims) - required - optional or not required.issubset(claims):
        _deny()
    issued = claims.get("iat")
    if (
        type(claims.get("v")) is not int
        or claims["v"] != 1
        or not _proof_issue_time_is_current(issued)
        or not isinstance(claims.get("jti"), str)
        or not _JTI.fullmatch(claims["jti"])
        or claims.get("sub") != principal.keycloak_user_id
    ):
        _deny()
    bearer = request.headers.get("authorization", "")
    scheme, _, token_value = bearer.partition(" ")
    if scheme.lower() != "bearer" or not token_value:
        _deny()
    binding = _request_binding(request, body, token_value)
    if any(claims.get(name) != value for name, value in binding.items()):
        _deny()
    if type(claims.get("device_revision")) is not int or claims["device_revision"] < 0:
        _deny()
    if claims.get("grant_id") is None:
        if "grant_revision" in claims:
            _deny()
    elif type(claims.get("grant_revision")) is not int or claims["grant_revision"] < 1:
        _deny()
    try:
        device_id = str(UUID(claims["device_id"]))
        if device_id != claims["device_id"]:
            _deny()
        grant_id = claims.get("grant_id")
        if grant_id is not None and str(UUID(grant_id)) != grant_id:
            _deny()
    except (ValueError, TypeError, AttributeError):
        _deny()
    return claims, token


def jwk_thumbprint(jwk: dict[str, Any]) -> str:
    return _thumbprint(jwk)


async def fresh_mobile_identity(session, principal: CurrentUser) -> CurrentUser:
    subject = principal.keycloak_user_id
    if not isinstance(subject, str) or not subject.strip():
        _deny()
    try:
        # The resolver's unmanaged-identity fallback returns its input. Reset the
        # cached status first so a deleted local User cannot retain active authority.
        seed = replace(principal, local_status=None)
        fresh = await resolve_effective_oidc_user(seed, session)
    except EffectiveIdentityLookupError as exc:
        raise HTTPException(status_code=403, detail="Текущий доступ недоступен") from exc
    if (
        fresh.keycloak_user_id != subject
        or fresh.local_status != "active"
        or fresh.roles != ["wms_mobile"]
    ):
        _deny()
    return fresh


async def _capture_mobile_metadata_owner(session, core, principal: CurrentUser) -> tuple[int, str]:
    from core.domain.models import User
    from core.services.auth import has_permission
    from core.services.config import get_settings

    subject = principal.keycloak_user_id
    if (
        get_settings().auth_mode != "oidc"
        or not isinstance(subject, str)
        or not subject
        or len(subject) > 64
    ):
        _deny()
    fresh = await fresh_mobile_identity(session, principal)
    if not has_permission(core, fresh, "wms.mobile"):
        _deny()
    managed = await session.scalar(select(User).where(
        User.keycloak_user_id == subject,
    ).execution_options(populate_existing=True))
    if (
        managed is None
        or type(managed.id) is not int
        or managed.id < 1
        or managed.status != "active"
        or managed.keycloak_user_id != subject
        or managed.role != "wms_mobile"
        or managed.role not in fresh.roles
    ):
        _deny()
    return managed.id, subject


async def _lock_mobile_metadata_owner(session, original_user_id: int, original_sub: str, fresh):
    from core.domain.models import User

    managed = await session.scalar(select(User).where(
        User.id == original_user_id,
    ).with_for_update().execution_options(populate_existing=True))
    if (
        managed is None
        or managed.id != original_user_id
        or managed.keycloak_user_id != original_sub
        or managed.status != "active"
        or managed.role != "wms_mobile"
        or managed.role not in fresh.roles
    ):
        _deny()
    return managed


def _require_mobile_metadata_action(grant: MobileGrant, action: MobileMetadataAction) -> None:
    actions = set(grant.actions)
    if action == "receiving_context":
        if "state" not in actions or not actions.intersection(_MOBILE_METADATA_ACTIONS):
            _deny()
    elif action not in _MOBILE_METADATA_ACTIONS or action not in actions:
        _deny()


def _operation_id(body: bytes) -> str | None:
    try:
        value = json.loads(body)
        operation_id = value.get("operation_id") if isinstance(value, dict) else None
        return str(UUID(operation_id)) if operation_id is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


async def _read_device(session, device_id: str) -> MobileDevice:
    query = select(MobileDevice).where(MobileDevice.id == device_id)
    query = query.with_for_update().execution_options(populate_existing=True)
    device = await session.scalar(query)
    if device is None:
        _deny()
    return device


async def consume_proof(
    request: Request,
    core,
    principal: CurrentUser,
    *,
    allowed_states: set[str] | None = None,
    require_grant: bool = False,
    metadata_action: MobileMetadataAction | None = None,
    receiving_source_authority: OriginalMobileReceivingSourceAuthority | None = None,
    receiving_source_action: Literal["transfer_discrepancy_report"] | None = None,
    receiving_source_receipt: bool = False,
    fulfillment_source_authority: OriginalMobileFulfillmentSourceAuthority | None = None,
) -> VerifiedProof:
    if metadata_action is not None and metadata_action not in {
        *_MOBILE_METADATA_ACTIONS, "receiving_context",
    }:
        _deny()
    if metadata_action is not None and not require_grant:
        _deny()
    if receiving_source_authority is not None and (
        metadata_action is not None or not require_grant
        or not isinstance(receiving_source_authority, OriginalMobileReceivingSourceAuthority)
    ):
        _deny()
    if fulfillment_source_authority is not None:
        identity = (
            fulfillment_source_authority.identity
            if isinstance(fulfillment_source_authority, OriginalMobileFulfillmentSourceAuthority)
            else None
        )
        if (
            not isinstance(identity, OriginalMobileReceivingSourceAuthority)
            or fulfillment_source_authority.purpose not in {"state", "pack"}
            or not require_grant or metadata_action is not None
            or receiving_source_authority is not None or receiving_source_action is not None
            or receiving_source_receipt
            or type(identity.organization_id) is not int or identity.organization_id < 1
            or type(identity.warehouse_id) is not int or identity.warehouse_id < 1
        ):
            _deny()
        if fulfillment_source_authority.purpose == "state":
            if request.method.upper() != "GET" or request.scope.get("path") != "/wms/mobile/state":
                _deny()
        elif request.method.upper() != "POST" or request.scope.get("path") != "/wms/mobile/commands":
            _deny()
    if (receiving_source_action is not None and (
        receiving_source_action != "transfer_discrepancy_report"
        or receiving_source_authority is None or not require_grant or metadata_action is not None
    )) or (type(receiving_source_receipt) is not bool
           or (receiving_source_receipt and receiving_source_action != "transfer_discrepancy_report")):
        _deny()
    token = request.headers.get(PROOF_HEADER)
    if not token:
        _deny()
    body = await request.body()
    if (fulfillment_source_authority is not None
            and fulfillment_source_authority.purpose == "pack"):
        try:
            command = json.loads(body)
            binding_id = command.get("binding_revision_id") if type(command) is dict else None
            canonical_binding = str(UUID(binding_id)) if type(binding_id) is str else None
        except (ValueError, TypeError, AttributeError):
            _deny()
        if (
            type(command) is not dict
            or command.get("kind") != "pack_v2"
            or type(binding_id) is not str
            or canonical_binding != binding_id
            or type(command.get("shipping_packages")) is not list
            or not command["shipping_packages"]
        ):
            _deny()
    factory = core.services.db.session_factory
    if factory is None:
        _deny(503)
    # Phase 1 owns and commits the replay fact before the request-session may
    # perform SQL. No ORM row is returned across this transaction boundary.
    try:
        async with factory() as proof_session:
            claims_unverified = jwt.decode(
                token,
                options={"verify_signature": False, "verify_exp": False},
                algorithms=["ES256"],
            )
            device_id = claims_unverified.get("device_id")
            if not isinstance(device_id, str):
                _deny()
            original_user_id = None
            original_sub = None
            source_identity = (
                fulfillment_source_authority.identity
                if fulfillment_source_authority is not None else receiving_source_authority
            )
            if source_identity is not None:
                # Independent source-read order starts with Device, before org/User/Grant.
                original_user_id = source_identity.original_user_id
                original_sub = source_identity.original_sub
                device = await _read_device(proof_session, device_id)
            else:
                if metadata_action is not None:
                    original_user_id, original_sub = await _capture_mobile_metadata_owner(
                        proof_session, core, principal,
                    )
                device = await _read_device(proof_session, device_id)
            fresh_seed = (
                _native_mobile_receiving_source_seed(source_identity)
                if source_identity is not None else principal
            )
            fresh = await fresh_mobile_identity(proof_session, fresh_seed)
            if fresh.keycloak_user_id != device.owner_sub:
                _deny()
            if receiving_source_authority is not None and not _native_mobile_receiving_source_identity(
                core, fresh, receiving_source_authority,
            ):
                _deny()
            if fulfillment_source_authority is not None and not _native_mobile_receiving_source_identity(
                core, fresh, fulfillment_source_authority.identity,
            ):
                _deny()
            if metadata_action is not None:
                from core.services.auth import has_permission

                if (
                    fresh.keycloak_user_id != original_sub
                    or not has_permission(core, fresh, "wms.mobile")
                ):
                    _deny()
            claims, _ = _decode_proof(token, _public_key(device.public_jwk), request, body, fresh)
            allowed_states = allowed_states or {"active"}
            if device.state not in allowed_states:
                _deny()
            discover_revision = request.scope.get("path") in {"/wms/mobile/status", "/wms/mobile/context"}
            if claims["device_revision"] != device.revision and not (
                discover_revision and claims["device_revision"] == 0
            ):
                _deny()
            grant_id = claims.get("grant_id")
            grant_revision = claims.get("grant_revision")
            if require_grant and not grant_id:
                _deny()
            if grant_id:
                if fulfillment_source_authority is not None:
                    identity = fulfillment_source_authority.identity
                    try:
                        from core.services.logistics import MobileFulfillmentAuthorityDenied

                        if (
                            type(original_user_id) is not int or original_user_id < 1
                            or type(original_sub) is not str or not original_sub
                            or grant_revision is None
                        ):
                            raise MobileFulfillmentAuthorityDenied()
                        local_proof = VerifiedProof(
                            device.id, device.revision, grant_id, grant_revision,
                            original_user_id, original_sub, claims["iat"],
                        )
                        proof_authority = replace(fulfillment_source_authority, proof=local_proof)
                        _, grant, final_fresh, _ = await revalidate_mobile_fulfillment_source_authority(
                            proof_session, core, _native_mobile_receiving_source_seed(identity),
                            proof_authority, None,
                        )
                        if grant.id != grant_id or grant.revision != grant_revision:
                            raise MobileFulfillmentAuthorityDenied()
                        final_claims, _ = _decode_proof(
                            token, _public_key(device.public_jwk), request, body, final_fresh,
                        )
                        if final_claims != claims:
                            raise MobileFulfillmentAuthorityDenied()
                        fresh = final_fresh
                    except MobileFulfillmentAuthorityDenied:
                        raise
                    except HTTPException as exc:
                        if exc.status_code in {401, 403, 404}:
                            raise MobileFulfillmentAuthorityDenied(exc.status_code) from exc
                        raise
                elif receiving_source_authority is not None:
                    try:
                        from modules.wms.physical_stock import control

                        actor = await core.services.accounting.source_member(
                            proof_session, receiving_source_authority.organization_id, fresh,
                        )
                        if actor != original_sub:
                            _deny()
                        await _lock_mobile_metadata_owner(
                            proof_session, original_user_id, original_sub, fresh,
                        )
                        statement = select(MobileGrant).where(
                            MobileGrant.id == grant_id,
                            MobileGrant.device_id == device.id,
                            MobileGrant.organization_id == receiving_source_authority.organization_id,
                            MobileGrant.state == "active",
                        )
                        if receiving_source_authority.warehouse_id is not None:
                            statement = statement.where(MobileGrant.warehouse_id == receiving_source_authority.warehouse_id)
                        grant = await proof_session.scalar(statement.with_for_update().execution_options(populate_existing=True))
                        required_actions = (
                            {receiving_source_action}
                            | ({"receipt"} if receiving_source_receipt else set())
                            if receiving_source_action is not None
                            else {"receipt" if receiving_source_authority.warehouse_id is None else "receive"}
                        )
                        if (grant is None or grant.revision != grant_revision
                            or not required_actions.issubset(set(grant.actions))):
                            _deny()
                        await control(proof_session, grant.warehouse_id, grant.organization_id, lock=False)
                        final_seed = _native_mobile_receiving_source_seed(receiving_source_authority)
                        final_fresh = await fresh_mobile_identity(proof_session, final_seed)
                        if not _native_mobile_receiving_source_identity(
                            core, final_fresh, receiving_source_authority,
                        ):
                            _deny()
                        final_claims, _ = _decode_proof(
                            token, _public_key(device.public_jwk), request, body, final_fresh,
                        )
                        if final_claims != claims:
                            _deny()
                    except HTTPException:
                        _deny()
                else:
                    grant = await proof_session.scalar(select(MobileGrant).where(
                        MobileGrant.id == grant_id,
                        MobileGrant.device_id == device.id,
                        MobileGrant.state == "active",
                    ))
                    if grant is None or grant.revision != grant_revision:
                        _deny()
                    try:
                        actor = await core.services.accounting.source_member(
                            proof_session, grant.organization_id, fresh,
                        )
                        if actor != fresh.keycloak_user_id:
                            _deny()
                        if metadata_action is not None:
                            _require_mobile_metadata_action(grant, metadata_action)
                            await _lock_mobile_metadata_owner(
                                proof_session, original_user_id, original_sub, fresh,
                            )
                        from modules.wms.physical_stock import control

                        await control(proof_session, grant.warehouse_id, grant.organization_id, lock=False)
                        if metadata_action is not None:
                            final_fresh = await fresh_mobile_identity(proof_session, principal)
                            if (
                                final_fresh.keycloak_user_id != original_sub
                                or not has_permission(core, final_fresh, "wms.mobile")
                            ):
                                _deny()
                            final_claims, _ = _decode_proof(
                                token, _public_key(device.public_jwk), request, body, final_fresh,
                            )
                            if final_claims != claims:
                                _deny()
                    except HTTPException:
                        _deny()
            now = datetime.now(UTC)
            row = MobileProofUse(
                device_id=device.id,
                jti=claims["jti"],
                request_digest=_request_digest(request, body, claims),
                expires_at=datetime.fromtimestamp(claims["iat"] + PROOF_MAX_AGE_SECONDS, UTC),
                operation_id=_operation_id(body),
                grant_id=grant_id,
                grant_revision=grant_revision,
                used_at=now,
            )
            proof_session.add(row)
            await proof_session.commit()
            return VerifiedProof(
                device.id, device.revision, grant_id, grant_revision,
                original_user_id, original_sub, claims["iat"],
            )
    except HTTPException:
        raise
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Мобильный запрос уже использован") from exc
    except Exception as exc:
        raise HTTPException(status_code=403, detail="Мобильный доступ не подтверждён") from exc


def _request_digest(request: Request, body: bytes, claims: dict) -> str:
    raw_path = request.scope.get("raw_path", b"")
    query = request.scope.get("query_string", b"")
    material = json.dumps(
        {
            "method": request.method.upper(),
            "path": raw_path.hex(),
            "query": _sha256(query),
            "body": _sha256(body),
            "ath": claims["ath"],
            "jti": claims["jti"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return _sha256(material)


async def lock_and_revalidate(
    session,
    core,
    principal: CurrentUser,
    proof: VerifiedProof,
    *,
    organization_id: int,
    warehouse_id: int | None,
) -> tuple[MobileDevice, MobileGrant, CurrentUser, str]:
    device = await _read_device(session, proof.device_id)
    fresh = await fresh_mobile_identity(session, principal)
    if (
        device.owner_sub != fresh.keycloak_user_id
        or device.state != "active"
        or device.revision != proof.device_revision
        or proof.grant_id is None
        or proof.grant_revision is None
    ):
        _deny()
    actor = await core.services.accounting.source_member(session, organization_id, fresh)
    statement = select(MobileGrant).where(
        MobileGrant.id == proof.grant_id,
        MobileGrant.device_id == device.id,
        MobileGrant.organization_id == organization_id,
        MobileGrant.state == "active",
    )
    if warehouse_id is not None:
        statement = statement.where(MobileGrant.warehouse_id == warehouse_id)
    grant = await session.scalar(statement.execution_options(populate_existing=True))
    if grant is None or grant.revision != proof.grant_revision:
        _deny()
    return device, grant, fresh, actor


async def revalidate_mobile_metadata_authority(
    session,
    core,
    principal: CurrentUser,
    authority: OriginalMobileMetadataAuthority,
):
    from core.services.auth import has_permission
    from core.services.config import get_settings
    from modules.wms.models import WarehouseDirectory
    from modules.wms.physical_stock import control

    if (
        not isinstance(authority, OriginalMobileMetadataAuthority)
        or type(authority.original_user_id) is not int
        or authority.original_user_id < 1
        or not isinstance(authority.original_sub, str)
        or not authority.original_sub
        or type(authority.organization_id) is not int
        or authority.organization_id < 1
        or type(authority.warehouse_id) is not int
        or authority.warehouse_id < 1
        or authority.action not in {*_MOBILE_METADATA_ACTIONS, "receiving_context"}
        or not isinstance(authority.verified_proof, VerifiedProof)
        or authority.verified_proof.original_user_id != authority.original_user_id
        or authority.verified_proof.original_sub != authority.original_sub
        or type(authority.verified_proof.issued_at) is not int
        or authority.verified_proof.grant_id is None
        or authority.verified_proof.grant_revision is None
        or get_settings().auth_mode != "oidc"
        or principal.keycloak_user_id != authority.original_sub
    ):
        _deny()

    proof = authority.verified_proof
    device = await _read_device(session, proof.device_id)
    fresh = await fresh_mobile_identity(session, principal)
    if (
        fresh.keycloak_user_id != authority.original_sub
        or not has_permission(core, fresh, "wms.mobile")
        or device.owner_sub != authority.original_sub
        or device.state != "active"
        or device.revision != proof.device_revision
    ):
        _deny()

    actor = await core.services.accounting.source_member(
        session, authority.organization_id, fresh,
    )
    if actor != authority.original_sub:
        _deny()
    await _lock_mobile_metadata_owner(
        session, authority.original_user_id, authority.original_sub, fresh,
    )
    grant = await session.scalar(select(MobileGrant).where(
        MobileGrant.id == proof.grant_id,
        MobileGrant.device_id == device.id,
        MobileGrant.organization_id == authority.organization_id,
        MobileGrant.warehouse_id == authority.warehouse_id,
        MobileGrant.state == "active",
    ).execution_options(populate_existing=True))
    if grant is None or grant.revision != proof.grant_revision:
        _deny()
    _require_mobile_metadata_action(grant, authority.action)
    warehouse = await control(
        session, authority.warehouse_id, authority.organization_id, lock=True,
    )
    directory = await session.scalar(select(WarehouseDirectory).where(
        WarehouseDirectory.id == authority.warehouse_id,
        WarehouseDirectory.organization_id == authority.organization_id,
        WarehouseDirectory.is_active.is_(True),
    ).execution_options(populate_existing=True))
    if directory is None:
        _deny(404)
    fresh = await fresh_mobile_identity(session, principal)
    if (
        fresh.keycloak_user_id != authority.original_sub
        or not has_permission(core, fresh, "wms.mobile")
        or not _proof_issue_time_is_current(proof.issued_at)
    ):
        _deny()
    return device, grant, fresh, actor, warehouse


async def revalidate_native_mobile_receiving_source_authority(
    session, core, principal: CurrentUser, authority: OriginalMobileReceivingSourceAuthority,
    proof: VerifiedProof, *, lock_warehouse: bool,
):
    """Recheck bearer, proof, device, organization and receive grant around source reads."""
    from core.services.config import get_settings
    from modules.wms.models import WarehouseDirectory
    from modules.wms.physical_stock import control

    if (
        not isinstance(authority, OriginalMobileReceivingSourceAuthority)
        or not isinstance(proof, VerifiedProof)
        or proof.original_user_id != authority.original_user_id
        or proof.original_sub != authority.original_sub
        or proof.grant_id is None or proof.grant_revision is None
        or type(proof.issued_at) is not int
        or get_settings().auth_mode != "oidc"
        or principal.local_user_id != authority.original_user_id
        or principal.keycloak_user_id != authority.original_sub
        or principal.issued_at != authority.bearer_issued_at
        or tuple(principal.roles) != authority.roles
    ):
        _deny()
    device = await _read_device(session, proof.device_id)
    fresh = await fresh_mobile_identity(session, _native_mobile_receiving_source_seed(authority))
    if (
        not _native_mobile_receiving_source_identity(core, fresh, authority)
        or device.owner_sub != authority.original_sub
        or device.state != "active" or device.revision != proof.device_revision
        or not _proof_issue_time_is_current(proof.issued_at)
    ):
        _deny()
    actor = await core.services.accounting.source_member(
        session, authority.organization_id, fresh,
    )
    if actor != authority.original_sub:
        _deny()
    await _lock_mobile_metadata_owner(
        session, authority.original_user_id, authority.original_sub, fresh,
    )
    grant = await session.scalar(select(MobileGrant).where(
        MobileGrant.id == proof.grant_id,
        MobileGrant.device_id == device.id,
        MobileGrant.organization_id == authority.organization_id,
        MobileGrant.warehouse_id == authority.warehouse_id,
        MobileGrant.state == "active",
    ).with_for_update().execution_options(populate_existing=True))
    if (grant is None or grant.revision != proof.grant_revision
        or "receive" not in set(grant.actions)):
        _deny()
    warehouse = None
    if lock_warehouse:
        directory = await session.scalar(select(WarehouseDirectory).where(
            WarehouseDirectory.id == authority.warehouse_id,
            WarehouseDirectory.organization_id == authority.organization_id,
            WarehouseDirectory.is_active.is_(True),
        ).with_for_update(read=True).execution_options(populate_existing=True))
        if directory is None:
            _deny(404)
        warehouse = await control(session, authority.warehouse_id, authority.organization_id, lock=True)
        if warehouse.mode != "erp" or warehouse.onec_ref is not None:
            _deny(404)
    final_fresh = await fresh_mobile_identity(session, _native_mobile_receiving_source_seed(authority))
    if (
        not _native_mobile_receiving_source_identity(core, final_fresh, authority)
        or not _proof_issue_time_is_current(proof.issued_at)
    ):
        _deny()
    return device, grant, final_fresh, actor, warehouse


def require_action(grant: MobileGrant, action: str, kind: str | None = None) -> None:
    if action not in grant.actions or kind in {
        "quality", "maintenance", "count_approve", "count_cancel", "cutover",
        "adopt_reservation", "correction_of",
    }:
        _deny()
    if kind == "task_complete" and action != "task_complete":
        _deny()
    if kind == "task_claim" and action != "task_claim":
        _deny()
