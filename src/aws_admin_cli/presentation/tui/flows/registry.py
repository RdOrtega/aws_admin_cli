"""The flow registry: every top-level TUI screen, in the order the main menu shows them.

Adding a new flow to this project means: write one module under
``flows/`` implementing ``Flow``, and add it here -- ``presentation/tui/app.py``
never grows an ``if kind == ...`` branch, same Open/Closed spirit as
``application/stacks/registry.py``'s own step registry.
"""

from aws_admin_cli.presentation.tui.flows.base import Flow
from aws_admin_cli.presentation.tui.flows.ec2_flow import Ec2Flow
from aws_admin_cli.presentation.tui.flows.iam_flow import IamFlow
from aws_admin_cli.presentation.tui.flows.s3_flow import S3Flow
from aws_admin_cli.presentation.tui.flows.stack_flow import StackFlow
from aws_admin_cli.presentation.tui.flows.vpc_flow import VpcFlow

__all__ = ["FLOWS"]

FLOWS: tuple[type[Flow], ...] = (S3Flow, IamFlow, VpcFlow, Ec2Flow, StackFlow)
