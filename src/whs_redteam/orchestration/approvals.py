"""Signed, bounded approvals for sensitive queued actions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import ValidationError

from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import CapabilityPolicyViolation
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import ActionProposal, ApprovalClaims
from whs_redteam.policy.capabilities import CapabilityPolicy
from whs_redteam.policy.scope import ScopeGuard


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign_approval(claims: ApprovalClaims, secret: bytes) -> str:
    """Administrative helper; production issuers should live outside the agent process."""

    if len(secret) < 32:
        raise ValueError("approval HMAC secret must be at least 32 bytes")
    payload = claims.model_dump_json().encode()
    encoded = _encode(payload)
    signature = hmac.new(secret, encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_encode(signature)}"


class ApprovalVerifier:
    def __init__(
        self,
        config: EngagementConfig,
        inventory: InventoryStore,
        secret: bytes,
    ) -> None:
        if len(secret) < 32:
            raise CapabilityPolicyViolation(
                "approval HMAC secret must be at least 32 bytes"
            )
        self.config = config
        self.inventory = inventory
        self.secret = secret

    @classmethod
    def from_environment(
        cls, config: EngagementConfig, inventory: InventoryStore
    ) -> ApprovalVerifier:
        variable = config.approval_hmac_secret_env
        if variable is None:
            raise CapabilityPolicyViolation(
                "approval_hmac_secret_env is required to verify approvals"
            )
        value = os.environ.get(variable)
        if value is None:
            raise CapabilityPolicyViolation(
                f"approval secret environment variable is missing: {variable}"
            )
        return cls(config, inventory, value.encode())

    def verify_and_consume(
        self, proposal: ActionProposal, token: str
    ) -> ApprovalClaims:
        claims, digest = self.verify(proposal, token)
        self._consume(digest, claims, datetime.now(UTC), manage_transaction=True)
        return claims

    def verify(
        self, proposal: ActionProposal, token: str
    ) -> tuple[ApprovalClaims, str]:
        claims, digest = self._verify_signature(token)
        now = datetime.now(UTC)
        if claims.engagement_name != self.config.name:
            raise CapabilityPolicyViolation("approval is for a different engagement")
        if claims.issued_at > now:
            raise CapabilityPolicyViolation("approval is not valid yet")
        if claims.expires_at <= now:
            raise CapabilityPolicyViolation("approval has expired")
        if proposal.capability not in claims.capabilities:
            raise CapabilityPolicyViolation("approval does not allow this capability")
        if proposal.method.upper() not in claims.methods:
            raise CapabilityPolicyViolation("approval does not allow this method")
        if not any(self._target_matches(root, proposal.target_url) for root in claims.targets):
            raise CapabilityPolicyViolation("approval does not allow this target")
        return claims, digest

    def verify_for_queue(
        self, proposal: ActionProposal, token: str
    ) -> tuple[ActionProposal, ApprovalClaims, str]:
        claims, digest = self.verify(proposal, token)
        approved = proposal.model_copy(update={"approval_id": claims.approval_id})
        CapabilityPolicy(self.config, ScopeGuard(self.config)).approve(
            approved, approval_verified=True
        )
        return approved, claims, digest

    def consume_in_current_transaction(
        self, digest: str, claims: ApprovalClaims, now: datetime
    ) -> None:
        self._consume(digest, claims, now, manage_transaction=False)

    def revoke(self, token_digest: str) -> None:
        cursor = self.inventory.connection.execute(
            "UPDATE approval_grants SET revoked = 1 WHERE token_digest = ?",
            (token_digest,),
        )
        self.inventory.connection.commit()
        if cursor.rowcount != 1:
            raise CapabilityPolicyViolation("approval grant was not found")

    def _verify_signature(self, token: str) -> tuple[ApprovalClaims, str]:
        if len(token) > 16_384 or token.count(".") != 1:
            raise CapabilityPolicyViolation("approval token format is invalid")
        encoded, supplied = token.split(".", 1)
        expected = hmac.new(self.secret, encoded.encode(), hashlib.sha256).digest()
        try:
            payload_bytes = _decode(encoded)
            supplied_bytes = _decode(supplied)
        except Exception as exc:
            raise CapabilityPolicyViolation("approval token encoding is invalid") from exc
        if _encode(payload_bytes) != encoded or _encode(supplied_bytes) != supplied:
            raise CapabilityPolicyViolation("approval token encoding is not canonical")
        if not hmac.compare_digest(expected, supplied_bytes):
            raise CapabilityPolicyViolation("approval signature is invalid")
        try:
            claims = ApprovalClaims.model_validate_json(payload_bytes)
        except (ValueError, ValidationError) as exc:
            raise CapabilityPolicyViolation("approval claims are invalid") from exc
        return claims, hashlib.sha256(token.encode()).hexdigest()

    def _consume(
        self,
        digest: str,
        claims: ApprovalClaims,
        now: datetime,
        *,
        manage_transaction: bool,
    ) -> None:
        connection = self.inventory.connection
        if manage_transaction:
            connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                INSERT OR IGNORE INTO approval_grants VALUES (
                    ?, ?, ?, ?, ?, ?, 0, 0, NULL
                )
                """,
                (
                    digest, claims.approval_id, claims.engagement_name,
                    claims.issuer, claims.expires_at.isoformat(), claims.max_uses,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE approval_grants SET use_count = use_count + 1,
                    last_used_at = ? WHERE token_digest = ? AND revoked = 0
                    AND use_count < max_uses AND expires_at > ?
                """,
                (now.isoformat(), digest, now.isoformat()),
            )
            if cursor.rowcount != 1:
                raise CapabilityPolicyViolation(
                    "approval is revoked, expired, or its uses are exhausted"
                )
            if manage_transaction:
                connection.commit()
        except Exception:
            if manage_transaction:
                connection.rollback()
            raise

    @staticmethod
    def _target_matches(approved_root: str, target: str) -> bool:
        try:
            root = urlsplit(approved_root)
            candidate = urlsplit(target)
            root_port = root.port or (443 if root.scheme == "https" else 80)
            candidate_port = candidate.port or (443 if candidate.scheme == "https" else 80)
        except ValueError:
            return False
        if root.scheme not in {"http", "https"} or not root.hostname:
            return False
        if root.query or root.fragment or root.username or root.password:
            return False
        if (
            root.scheme.lower(), root.hostname.lower(), root_port
        ) != (
            candidate.scheme.lower(), (candidate.hostname or "").lower(), candidate_port
        ):
            return False
        root_path = (root.path or "/").rstrip("/") + "/"
        candidate_path = (candidate.path or "/").rstrip("/") + "/"
        return candidate_path.startswith(root_path)
