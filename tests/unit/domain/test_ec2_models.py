"""Tests for EC2 domain models: hydration, the state machine, and the security guard rails."""

import pytest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.ec2 import (
    Instance,
    InstanceState,
    KeyMaterial,
    get_instance_spec,
    validate_user_data,
)

# -- Instance hydration from a real, nested DescribeInstances entry ---------------


def _raw_instance() -> dict[str, object]:
    return {
        "InstanceId": "i-0a1b2c3d4e5f6a7b8",
        "InstanceType": "t3.micro",
        "State": {"Code": 16, "Name": "running"},
        "ImageId": "ami-0a1b2c3d",
        "PrivateIpAddress": "10.0.11.5",
        "PublicIpAddress": None,
        "SubnetId": "subnet-0a1b2c01",
        "VpcId": "vpc-0a1b2c3d",
        "Placement": {"AvailabilityZone": "us-east-1a", "Tenancy": "default"},
        "SecurityGroups": [
            {"GroupId": "sg-0a1b2c01", "GroupName": "corp-web-sg"},
            {"GroupId": "sg-0a1b2c02", "GroupName": "corp-internal-sg"},
        ],
        "KeyName": "demo-key",
        "IamInstanceProfile": {
            "Arn": "arn:aws:iam::123456789012:instance-profile/demo-role",
            "Id": "AIPAEXAMPLE",
        },
        "LaunchTime": "2026-01-01T00:00:00+00:00",
        "Architecture": "x86_64",
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "Status": "attached",
                    "DeleteOnTermination": True,
                    "VolumeId": "vol-0a1b2c3d",
                },
            }
        ],
        "Tags": [
            {"Key": "Name", "Value": "demo-web"},
            {"Key": "ManagedBy", "Value": "aws-admin-cli"},
        ],
    }


def test_instance_hydrates_from_a_real_nested_describe_instances_entry() -> None:
    instance = Instance.model_validate(_raw_instance())

    assert instance.instance_id == "i-0a1b2c3d4e5f6a7b8"
    assert instance.state is InstanceState.RUNNING
    assert instance.availability_zone == "us-east-1a"
    assert instance.security_group_ids == ["sg-0a1b2c01", "sg-0a1b2c02"]
    assert instance.security_group_names == ["corp-web-sg", "corp-internal-sg"]
    assert instance.iam_instance_profile_arn == (
        "arn:aws:iam::123456789012:instance-profile/demo-role"
    )
    assert len(instance.block_devices) == 1
    assert instance.block_devices[0].device_name == "/dev/xvda"
    assert instance.block_devices[0].delete_on_termination is True
    assert instance.name == "demo-web"
    assert instance.display_name == "demo-web"
    assert instance.managed_by_cli is True


def test_instance_display_name_falls_back_to_id_without_a_name_tag() -> None:
    raw = _raw_instance()
    raw["Tags"] = []
    instance = Instance.model_validate(raw)
    assert instance.name is None
    assert instance.display_name == instance.instance_id


def test_instance_managed_by_cli_is_false_without_the_tag() -> None:
    raw = _raw_instance()
    raw["Tags"] = [{"Key": "Name", "Value": "someone-elses-box"}]
    instance = Instance.model_validate(raw)
    assert instance.managed_by_cli is False


# -- InstanceState machine: full 6-state x 4-action matrix -------------------------

_ALL_STATES = list(InstanceState)

_EXPECTED_CAN_START = {InstanceState.STOPPED}
_EXPECTED_CAN_STOP = {InstanceState.RUNNING}
_EXPECTED_CAN_REBOOT = {InstanceState.RUNNING}
_EXPECTED_CAN_TERMINATE = set(InstanceState) - {
    InstanceState.TERMINATED,
    InstanceState.SHUTTING_DOWN,
}


@pytest.mark.parametrize("state", _ALL_STATES)
def test_can_start_matrix(state: InstanceState) -> None:
    assert state.can_start == (state in _EXPECTED_CAN_START)


@pytest.mark.parametrize("state", _ALL_STATES)
def test_can_stop_matrix(state: InstanceState) -> None:
    assert state.can_stop == (state in _EXPECTED_CAN_STOP)


@pytest.mark.parametrize("state", _ALL_STATES)
def test_can_reboot_matrix(state: InstanceState) -> None:
    assert state.can_reboot == (state in _EXPECTED_CAN_REBOOT)


@pytest.mark.parametrize("state", _ALL_STATES)
def test_can_terminate_matrix(state: InstanceState) -> None:
    assert state.can_terminate == (state in _EXPECTED_CAN_TERMINATE)


def test_is_transitional_and_is_billable() -> None:
    assert InstanceState.PENDING.is_transitional is True
    assert InstanceState.STOPPING.is_transitional is True
    assert InstanceState.SHUTTING_DOWN.is_transitional is True
    assert InstanceState.RUNNING.is_transitional is False
    assert InstanceState.STOPPED.is_transitional is False
    assert InstanceState.TERMINATED.is_transitional is False

    assert InstanceState.RUNNING.is_billable is True
    assert InstanceState.STOPPED.is_billable is False


def test_transition_error_mentions_state_and_action() -> None:
    message = InstanceState.TERMINATED.transition_error("iniciar")
    assert "iniciar" in message
    assert "terminated" in message


# -- KeyMaterial: never leaks the private key -------------------------------------


def test_key_material_repr_and_str_never_contain_the_private_key() -> None:
    material = KeyMaterial(key_name="demo-key", private_key="SUPERSECRETO")

    assert "SUPERSECRETO" not in repr(material)
    assert "SUPERSECRETO" not in str(material)
    assert "SUPERSECRETO" not in f"{material}"
    assert "REDACTED" in repr(material)
    assert "demo-key" in repr(material)


# -- validate_user_data -------------------------------------------------------------


def test_validate_user_data_accepts_empty_and_typical_scripts() -> None:
    assert validate_user_data("") == ""
    assert validate_user_data("#!/bin/bash\necho hello") == "#!/bin/bash\necho hello"


def test_validate_user_data_accepts_exactly_16384_bytes() -> None:
    script = "a" * 16384
    assert validate_user_data(script) == script


def test_validate_user_data_rejects_16385_bytes_with_the_real_size_in_the_message() -> None:
    script = "a" * 16385
    with pytest.raises(ValidationError) as exc_info:
        validate_user_data(script)
    assert "16385" in str(exc_info.value)


@pytest.mark.parametrize(
    "user_data",
    [
        "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----",
        "DB_PASSWORD=hunter2\npassword=hunter2",
        "API_TOKEN\ntoken=abc123",
    ],
)
def test_validate_user_data_rejects_each_secret_pattern(user_data: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_user_data(user_data)
    assert exc_info.value.hint is not None
    assert "IMDS" in exc_info.value.hint


# -- get_instance_spec ---------------------------------------------------------------


def test_get_instance_spec_known_type() -> None:
    spec = get_instance_spec("t3.micro")
    assert spec is not None
    assert spec.family == "t3"
    assert spec.is_burstable is True
    assert spec.vcpus == 2


def test_get_instance_spec_unknown_type_returns_none() -> None:
    assert get_instance_spec("z9.impossible") is None
