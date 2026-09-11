"""Capture the stable deployment core class before compatibility layers replace its public alias."""

from ._deployment_state_machine_core import DeploymentStateMachine as CoreDeploymentStateMachine

__all__ = ["CoreDeploymentStateMachine"]
