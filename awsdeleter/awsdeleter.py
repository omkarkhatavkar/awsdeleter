import functools
import time

import boto3
import botocore.exceptions
import click

# Priority ranking for ordered deletion (Lowest rank deleted first)
DELETION_PRIORITY = {
    "DNS Record": 1,
    "Load Balancer": 2,
    "Target Group": 3,
    "EC2 Instance": 4,
    "EBS Volume": 5,
    "Security Group": 6,
    "Elastic IP": 7,
    "S3 Bucket": 8,
    "VPC": 9,
}


def retry_on_dependency_violation(timeout_seconds=300, delay_seconds=5, backoff=2, max_delay=30):
    """Decorator to retry functions when AWS returns a dependency or in-use exception."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            attempt = 1
            delay = delay_seconds

            while True:
                try:
                    return func(*args, **kwargs)
                except botocore.exceptions.ClientError as e:
                    if "DependencyViolation" in str(e) or "ResourceInUse" in str(e):
                        elapsed = time.time() - start_time
                        if elapsed >= timeout_seconds:
                            raise TimeoutError(f"Timed out retrying {func.__name__} after {timeout_seconds} seconds.")
                        print(
                            f"[Retry {attempt}] {func.__name__} failed due to resource dependency. Retrying in {delay}s..."
                        )
                        time.sleep(delay)
                        delay = min(delay * backoff, max_delay)
                        attempt += 1
                    else:
                        raise

        return wrapper

    return decorator


def log(message, verbose):
    """Helper to output logs if verbose mode is enabled."""
    if verbose:
        click.echo(message)


def search_resources_with_prefix(prefix, resource_filter):
    """Search all supported AWS services for resources matching the given name prefix."""
    resources = []
    ec2 = boto3.client("ec2")
    s3 = boto3.client("s3")
    elbv2 = boto3.client("elbv2")
    route53 = boto3.client("route53")

    target_types = (
        [resource_filter.lower()] if resource_filter else ["dns", "alb", "tg", "ec2", "ebs", "sg", "eip", "s3", "vpc"]
    )

    # 1. Route 53 DNS Records (A records only; ambiguous multi-match is handled after collection)
    if "dns" in target_types:
        zones = route53.list_hosted_zones().get("HostedZones", [])
        for zone in zones:
            records = route53.list_resource_record_sets(HostedZoneId=zone["Id"]).get("ResourceRecordSets", [])
            for record in records:
                if record["Type"] == "A" and record["Name"].startswith(prefix):
                    resources.append(
                        {
                            "Type": "DNS Record",
                            "ID": record["Name"],
                            "Name": record["Name"],
                            "ZoneId": zone["Id"],
                            "Record": record,
                        }
                    )

        # Safety: only delete the DNS record when exactly one A record matches the prefix.
        dns_matches = [r for r in resources if r["Type"] == "DNS Record"]
        if len(dns_matches) > 1:
            click.echo(
                f"Warning: Found {len(dns_matches)} DNS A records matching prefix '{prefix}'. "
                "Skipping DNS deletion since the match is ambiguous."
            )
            resources = [r for r in resources if r["Type"] != "DNS Record"]

    # 2. Load Balancers (ALB/NLB)
    if "alb" in target_types:
        albs = elbv2.describe_load_balancers().get("LoadBalancers", [])
        for alb in albs:
            if alb["LoadBalancerName"].startswith(prefix):
                resources.append(
                    {"Type": "Load Balancer", "ID": alb["LoadBalancerArn"], "Name": alb["LoadBalancerName"]}
                )

    # 3. Target Groups
    if "tg" in target_types:
        tgs = elbv2.describe_target_groups().get("TargetGroups", [])
        for tg in tgs:
            if tg["TargetGroupName"].startswith(prefix):
                resources.append({"Type": "Target Group", "ID": tg["TargetGroupArn"], "Name": tg["TargetGroupName"]})

    # 4. EC2 Instances
    if "ec2" in target_types:
        instances = ec2.describe_instances().get("Reservations", [])
        for reservation in instances:
            for instance in reservation["Instances"]:
                if instance["State"]["Name"] in ["terminated", "shutting-down"]:
                    continue
                for tag in instance.get("Tags", []):
                    if tag["Key"] == "Name" and tag["Value"].startswith(prefix):
                        resources.append({"Type": "EC2 Instance", "ID": instance["InstanceId"], "Name": tag["Value"]})

    # 5. EBS Volumes
    if "ebs" in target_types:
        volumes = ec2.describe_volumes().get("Volumes", [])
        for vol in volumes:
            for tag in vol.get("Tags", []):
                if tag["Key"] == "Name" and tag["Value"].startswith(prefix):
                    resources.append({"Type": "EBS Volume", "ID": vol["VolumeId"], "Name": tag["Value"]})

    # 6. Security Groups
    if "sg" in target_types:
        sgs = ec2.describe_security_groups().get("SecurityGroups", [])
        for sg in sgs:
            if sg["GroupName"] != "default":
                for tag in sg.get("Tags", []):
                    if tag["Key"] == "Name" and tag["Value"].startswith(prefix):
                        resources.append({"Type": "Security Group", "ID": sg["GroupId"], "Name": tag["Value"]})

    # 7. Elastic IPs
    if "eip" in target_types:
        addresses = ec2.describe_addresses().get("Addresses", [])
        for address in addresses:
            for tag in address.get("Tags", []):
                if tag["Key"] == "Name" and tag["Value"].startswith(prefix):
                    resources.append(
                        {
                            "Type": "Elastic IP",
                            "ID": address.get("AllocationId"),
                            "PublicIp": address.get("PublicIp"),
                            "Name": tag["Value"],
                        }
                    )

    # 8. S3 Buckets
    if "s3" in target_types:
        buckets = s3.list_buckets().get("Buckets", [])
        for bucket in buckets:
            if bucket["Name"].startswith(prefix):
                resources.append({"Type": "S3 Bucket", "ID": bucket["Name"], "Name": bucket["Name"]})

    # 9. VPCs
    if "vpc" in target_types:
        vpcs = ec2.describe_vpcs().get("Vpcs", [])
        for vpc in vpcs:
            for tag in vpc.get("Tags", []):
                if tag["Key"] == "Name" and tag["Value"].startswith(prefix):
                    resources.append({"Type": "VPC", "ID": vpc["VpcId"], "Name": tag["Value"]})

    # Sort resources by safe deletion hierarchy order
    resources.sort(key=lambda r: DELETION_PRIORITY.get(r["Type"], 99))
    return resources


@retry_on_dependency_violation(timeout_seconds=300)
def delete_vpc(vpc_id, verbose=False):
    """Delete a VPC with everything inside it (Instances, Endpoints, NAT, EIPs, IGW, ENIs, Subnets, Route Tables, NACLs, SGs)."""
    ec2 = boto3.client("ec2")

    # 1. Terminate EC2 Instances in the VPC
    instances = ec2.describe_instances(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    instance_ids = [
        i["InstanceId"]
        for r in instances.get("Reservations", [])
        for i in r.get("Instances", [])
        if i["State"]["Name"] not in ("terminated", "shutting-down")
    ]
    if instance_ids:
        ec2.terminate_instances(InstanceIds=instance_ids)
        waiter = ec2.get_waiter("instance_terminated")
        waiter.wait(InstanceIds=instance_ids)

    # 2. Delete VPC Endpoints
    endpoints = ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["VpcEndpoints"]
    endpoint_ids = [e["VpcEndpointId"] for e in endpoints if e["State"] != "deleted"]
    if endpoint_ids:
        ec2.delete_vpc_endpoints(VpcEndpointIds=endpoint_ids)

    # 3. Delete NAT Gateways
    nats = ec2.describe_nat_gateways(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["NatGateways"]
    nat_ids = [n["NatGatewayId"] for n in nats if n["State"] != "deleted"]
    for nat_id in nat_ids:
        ec2.delete_nat_gateway(NatGatewayId=nat_id)
    if nat_ids:
        waiter = ec2.get_waiter("nat_gateway_deleted")
        waiter.wait(NatGatewayIds=nat_ids)

    # 4. Disassociate and Release Elastic IPs attached to VPC ENIs
    enis = ec2.describe_network_interfaces(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["NetworkInterfaces"]
    for eni in enis:
        assoc = eni.get("Association", {})
        if assoc:
            assoc_id = assoc.get("AssociationId")
            alloc_id = assoc.get("AllocationId")
            if assoc_id:
                ec2.disassociate_address(AssociationId=assoc_id)
            if alloc_id:
                ec2.release_address(AllocationId=alloc_id)

    # 5. Detach & Delete Internet Gateways
    igws = ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])[
        "InternetGateways"
    ]
    for igw in igws:
        ec2.detach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
        ec2.delete_internet_gateway(InternetGatewayId=igw["InternetGatewayId"])

    # 6. Delete leftover unattached Elastic Network Interfaces
    enis = ec2.describe_network_interfaces(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["NetworkInterfaces"]
    for eni in enis:
        if eni["Status"] == "available":
            ec2.delete_network_interface(NetworkInterfaceId=eni["NetworkInterfaceId"])

    # 7. Delete Subnets
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    for subnet in subnets:
        ec2.delete_subnet(SubnetId=subnet["SubnetId"])

    # 8. Delete Route Tables (excluding main)
    rtbs = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
    for rtb in rtbs:
        if not any(a.get("Main", False) for a in rtb.get("Associations", [])):
            ec2.delete_route_table(RouteTableId=rtb["RouteTableId"])

    # 9. Delete Network ACLs (excluding default)
    nacls = ec2.describe_network_acls(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["NetworkAcls"]
    for nacl in nacls:
        if not nacl.get("IsDefault", False):
            ec2.delete_network_acl(NetworkAclId=nacl["NetworkAclId"])

    # 10. Delete Security Groups (excluding default)
    sgs = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]
    for sg in sgs:
        if sg["GroupName"] != "default":
            ec2.delete_security_group(GroupId=sg["GroupId"])

    # 11. Delete VPC
    ec2.delete_vpc(VpcId=vpc_id)
    log(f"Deleted VPC {vpc_id}.", verbose)


def delete_resource(resource, verbose=False):
    """Route deletion calls based on specific AWS resource type."""
    ec2 = boto3.client("ec2")
    elbv2 = boto3.client("elbv2")
    route53 = boto3.client("route53")
    s3_resource = boto3.resource("s3")

    res_type = resource["Type"]
    res_id = resource["ID"]

    if res_type == "DNS Record":
        route53.change_resource_record_sets(
            HostedZoneId=resource["ZoneId"],
            ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": resource["Record"]}]},
        )
        log(f"Deleted DNS Record {resource['Name']}.", verbose)

    elif res_type == "Load Balancer":
        elbv2.delete_load_balancer(LoadBalancerArn=res_id)
        log(f"Deleted Load Balancer {res_id}.", verbose)

    elif res_type == "Target Group":
        elbv2.delete_target_group(TargetGroupArn=res_id)
        log(f"Deleted Target Group {res_id}.", verbose)

    elif res_type == "EC2 Instance":
        ec2.terminate_instances(InstanceIds=[res_id])
        log(f"Initiated termination for EC2 Instance {res_id}.", verbose)
        # Wait for termination so associated EIPs and EBS volumes release cleanly
        waiter = ec2.get_waiter("instance_terminated")
        waiter.wait(InstanceIds=[res_id])

    elif res_type == "EBS Volume":
        ec2.delete_volume(VolumeId=res_id)
        log(f"Deleted EBS Volume {res_id}.", verbose)

    elif res_type == "Security Group":
        ec2.delete_security_group(GroupId=res_id)
        log(f"Deleted Security Group {res_id}.", verbose)

    elif res_type == "Elastic IP":
        if resource.get("ID"):
            ec2.release_address(AllocationId=resource["ID"])
            log(f"Released Elastic IP {resource['ID']}.", verbose)

    elif res_type == "S3 Bucket":
        bucket = s3_resource.Bucket(resource["Name"])
        bucket.object_versions.delete()
        bucket.delete()
        log(f"Deleted S3 Bucket {resource['Name']} and all object versions.", verbose)

    elif res_type == "VPC":
        delete_vpc(res_id, verbose)


@click.command()
@click.argument("prefix")
@click.option(
    "--resource",
    default=None,
    help="Filter by resource (e.g., dns, alb, tg, ec2, ebs, sg, eip, s3, vpc)",
)
@click.option(
    "--confirm",
    type=str,
    default=None,
    help="Delete resources without individual prompts (e.g. '--confirm yes')",
)
@click.option(
    "--force",
    is_flag=True,
    help="Force deletion even if prefix is shorter than 8 characters",
)
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="Show detailed execution logs",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Simulate execution and list resources targeted for deletion without removing them",
)
def main(prefix, resource, confirm, force, verbose, dry_run):
    if len(prefix) < 8 and not force:
        click.echo("Error: Prefix must be at least 8 characters long. Use --force to override.")
        raise click.Abort()

    results = search_resources_with_prefix(prefix, resource)

    if not results:
        click.echo(f"No resources found matching prefix '{prefix}'.")
        return

    # Dry run execution path
    if dry_run:
        click.echo(f"\n[DRY RUN] Found {len(results)} resource(s) matching prefix '{prefix}':")
        click.echo("=" * 60)
        for idx, res in enumerate(results, start=1):
            click.echo(f"{idx}. [{res['Type']}] ID/Name: {res.get('ID', res['Name'])}")
        click.echo("=" * 60)
        click.echo("[DRY RUN] No resources were deleted. Remove --dry-run to execute.")
        return

    # Actual deletion execution path
    log(f"Found {len(results)} resource(s) with prefix '{prefix}' sorted by deletion priority:", verbose)
    for res in results:
        log(f" -> [{res['Type']}] ID/Name: {res.get('ID', res['Name'])}", verbose)

    # Parse confirm parameter (accepts strings like 'yes', 'y', 'true', '1')
    is_confirmed = False
    if confirm:
        if isinstance(confirm, str):
            is_confirmed = confirm.lower() in ["yes", "y", "true", "1"]
        else:
            is_confirmed = bool(confirm)

    for res in results:
        if is_confirmed:
            delete_confirm = "yes"
        else:
            delete_confirm = click.prompt(f"Delete {res['Type']} ({res.get('ID', res['Name'])})? (yes/y to confirm)")
        if delete_confirm.lower() in ["yes", "y"]:
            delete_resource(res, verbose)
        else:
            log(f"Skipped deletion for {res['Type']} ({res.get('ID', res['Name'])}).", verbose)


if __name__ == "__main__":
    main()
