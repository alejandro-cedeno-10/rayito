"""`rayito doctor --efs-vpc-id ... --efs-subnet-ids ...`: la comprobación
previa de `EfsVolumes` como una comprobación más (sólo lectura), y que sin
esas opciones no aparece ni llama a EC2."""

from __future__ import annotations

import boto3

from rayito.cli import _checks
from rayito.cli._checks import DoctorContext, run_check
from rayito.cli._session import Clients

from ..fake_ec2 import SUBNET_A, SUBNET_B, VPC_ID, FakeEc2Api, subnet

REGION = "us-east-1"


def clients_with(ec2: FakeEc2Api) -> Clients:
    return Clients(
        session=boto3.session.Session(region_name=REGION), region=REGION, clients={"ec2": ec2}
    )


def run(ec2: FakeEc2Api, subnets: str):  # type: ignore[no-untyped-def]
    context = DoctorContext(efs_vpc_id=VPC_ID, efs_subnet_ids=subnets)
    return run_check(_checks.EFS_NETWORK_CHECK, clients_with(ec2), context)


def test_a_suitable_vpc_is_ok_and_reports_what_deploy_would_create() -> None:
    ec2 = FakeEc2Api()
    result = run(ec2, f"{SUBNET_A},{SUBNET_B}")
    assert result.name == "efs-network"
    assert result.status == "OK"
    assert "crearía" in result.summary
    assert "AWS::EFS::FileSystem" in result.details["creates"]
    assert result.details["idle_monthly"]
    assert all(call.startswith("describe_") for call in ec2.calls)


def test_two_subnets_in_one_az_fail_and_nothing_would_be_created() -> None:
    ec2 = FakeEc2Api(subnets=[subnet(SUBNET_A, "us-east-1a"), subnet(SUBNET_B, "us-east-1a")])
    result = run(ec2, f"{SUBNET_A},{SUBNET_B}")
    assert result.status == "FAIL"
    assert "misma AZ" in result.summary
    assert "no crearía nada" in result.summary


def test_a_single_az_is_a_warning() -> None:
    result = run(FakeEc2Api(), SUBNET_A)
    assert result.status == "WARN"
    assert "una sola AZ" in result.summary


def test_a_malformed_subnet_list_fails_without_calling_ec2() -> None:
    ec2 = FakeEc2Api()
    result = run(ec2, "subnet-nope")
    assert result.status == "FAIL"
    assert ec2.calls == []


def test_the_check_is_off_without_its_options() -> None:
    assert "efs-network" not in _checks.CHECK_NAMES
    assert DoctorContext().efs_vpc_id is None


def test_run_doctor_appends_it_only_when_asked(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from rayito.cli import doctor

    monkeypatch.setattr(doctor, "PRE_LAUNCH_CHECKS", ())
    monkeypatch.setattr(doctor, "LAUNCH_CHECKS", ())
    ec2 = FakeEc2Api()
    assert doctor.run_doctor(clients_with(ec2), DoctorContext()) == []
    assert ec2.calls == []
    asked = DoctorContext(efs_vpc_id=VPC_ID, efs_subnet_ids=f"{SUBNET_A},{SUBNET_B}")
    (result,) = doctor.run_doctor(clients_with(ec2), asked)
    assert result.name == "efs-network"
