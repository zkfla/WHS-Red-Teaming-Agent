"""Engagement Control Plane. Owned by Control Team.

Implements ``redteam.core.ports.control_plane.ControlPlane``. This is a
code-based gate, not a prompt-based safeguard: ``DefaultControlPlane``
runs Scope -> ROE -> Policy -> Rate Limit -> Budget -> Approval in
front of every ``ActionProposal`` (see instruction.md section 9).
"""

from redteam.control.service import DefaultControlPlane

__all__ = ["DefaultControlPlane"]
