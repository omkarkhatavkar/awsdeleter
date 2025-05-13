import functools
import time

import boto3
import botocore.exceptions
import click


def retry_on_dependency_violation(timeout_seconds=300, delay_seconds=5, backoff=2, max_delay=30):
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
                    if "DependencyViolation" in str(e):
                        elapsed = time.time() - start_time
                        if elapsed >= timeout_seconds:
                            raise TimeoutError(f"Timed out retrying {func.__name__} after {timeout_seconds} seconds.")
                        print(
                            f"[Retry {attempt}] {func.__name__} failed due to dependency violation. Retrying in {delay}s..."
                        )
                        time.sleep(delay)
                        delay = min(delay * backoff, max_delay)
                        attempt += 1
                    else:
                        raise

        return wrapper

    return decorator


def search_resources_with_prefix(prefix, resource):
    resources = []

    ec2 = boto3.client("ec2")
    s3 = boto3.client("s3")
    if resource is None or resource == "":
        resource = ["ec2", "s3", "vpc"]
    else:
        resource = [str(resource)]
    # EC2 Instances
    if "ec2" in resource:
        instances = ec2.describe_instances()
        for reservation in instances["Reservations"]:
            for instance in reservation["Instances"]:
                for tag in instance.get("Tags", []):
                    if tag["Key"] == "Name" and tag["Value"].startswith(prefix):
                        resources.append({"Type": "EC2 Instance", "ID": instance["InstanceId"], "Name": tag["Value"]})

    # S3 Buckets
    if "s3" in resource:
        buckets = s3.list_buckets()
        for bucket in buckets["Buckets"]:
            if bucket["Name"].startswith(prefix):
                resources.append({"Type": "S3 Bucket", "Name": bucket["Name"]})

    # VPCs
    if "vpc" in resource:
        vpcs = ec2.describe_vpcs()
        for vpc in vpcs["Vpcs"]:
            for tag in vpc.get("Tags", []):
                if tag["Key"] == "Name" and tag["Value"].startswith(prefix):
                    resources.append({"Type": "VPC", "ID": vpc["VpcId"], "Name": tag["Value"]})

    return resources


@retry_on_dependency_violation(timeout_seconds=300)
def delete_vpc(vpc_id):
    """Delete VPC and its dependencies."""
    ec2 = boto3.client("ec2")

    click.echo(f"Deleting dependencies of VPC {vpc_id}...")

    # Delete NAT Gateways
    nat_gateways = ec2.describe_nat_gateways(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["NatGateways"]
    for nat in nat_gateways:
        ec2.delete_nat_gateway(NatGatewayId=nat["NatGatewayId"])
        click.echo(f"Deleted NAT Gateway {nat['NatGatewayId']}.")
    # Wait for deletion
    waiter = ec2.get_waiter("nat_gateway_deleted")
    for nat in nat_gateways:
        waiter.wait(NatGatewayIds=[nat["NatGatewayId"]])

    # Detach and delete Internet Gateways
    igws = ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])[
        "InternetGateways"
    ]
    for igw in igws:
        ec2.detach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
        ec2.delete_internet_gateway(InternetGatewayId=igw["InternetGatewayId"])
        click.echo(f"Deleted Internet Gateway {igw['InternetGatewayId']}.")

    # Delete Subnets
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    for subnet in subnets:
        ec2.delete_subnet(SubnetId=subnet["SubnetId"])
        click.echo(f"Deleted Subnet {subnet['SubnetId']}.")

    # Delete Route Tables (excluding main)
    route_tables = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
    for rtb in route_tables:
        if not any(assoc.get("Main", False) for assoc in rtb.get("Associations", [])):
            ec2.delete_route_table(RouteTableId=rtb["RouteTableId"])
            click.echo(f"Deleted Route Table {rtb['RouteTableId']}.")

    # Delete Security Groups (excluding default)
    security_groups = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]
    for sg in security_groups:
        if sg["GroupName"] != "default":
            ec2.delete_security_group(GroupId=sg["GroupId"])
            click.echo(f"Deleted Security Group {sg['GroupId']}.")

    # Finally, delete the VPC
    ec2.delete_vpc(VpcId=vpc_id)
    click.echo(f"Deleted VPC {vpc_id}.")


def delete_resource(resource):
    """Delete EC2 instance, S3 bucket, or VPC."""
    ec2 = boto3.client("ec2")
    s3 = boto3.client("s3")

    if resource["Type"] == "EC2 Instance":
        # First, release and delete any associated Elastic IPs
        addresses = ec2.describe_addresses(Filters=[{"Name": "instance-id", "Values": [resource["ID"]]}])
        for address in addresses.get("Addresses", []):
            association_id = address.get("AssociationId")
            allocation_id = address.get("AllocationId")

            if association_id:
                ec2.disassociate_address(AssociationId=association_id)
                click.echo(f"Disassociated Elastic IP {address['PublicIp']}.")

            if allocation_id:
                ec2.release_address(AllocationId=allocation_id)
                click.echo(f"Released Elastic IP {address['PublicIp']}.")

        # Then terminate the instance
        ec2.terminate_instances(InstanceIds=[resource["ID"]])
        click.echo(f"EC2 Instance {resource['ID']} has been terminated.")

    elif resource["Type"] == "S3 Bucket":
        objects = s3.list_objects_v2(Bucket=resource["Name"])
        if "Contents" in objects:
            for obj in objects["Contents"]:
                s3.delete_object(Bucket=resource["Name"], Key=obj["Key"])
        s3.delete_bucket(Bucket=resource["Name"])
        click.echo(f"S3 Bucket {resource['Name']} has been deleted.")

    elif resource["Type"] == "VPC":
        delete_vpc(resource["ID"])


@click.command()
@click.argument("prefix")
@click.option(
    "--resource", default=None, help="Enter the resoruce type wanted to delete e.g. --resource=vpc or ec2 or s3"
)
@click.option("--confirm", default=False, help="Enter boolean to delete without getting the confirm popup")
@click.option("--force", is_flag=True, help="Force deletion even if prefix is shorter than 8 characters")
def main(prefix, resource, confirm, force):
    if len(prefix) < 8 and not force:
        click.echo("Error: Prefix must be at least 8 characters long. Use --force to override.")
        raise click.Abort()
    results = search_resources_with_prefix(prefix, resource)
    delete_confirm = None
    if results:
        click.echo(f"Resources found with prefix '{prefix}':")
        for resource in results:
            click.echo(f"Type: {resource['Type']}, ID/Name: {resource.get('ID', resource['Name'])}")
            if confirm:
                delete_confirm = "yes"
            else:
                delete_confirm = click.prompt(
                    f"Do you want to delete this resource (ID/Name: {resource.get('ID', resource['Name'])})? (yes/y to confirm)"
                )
            if delete_confirm.lower() in ["yes", "y"]:
                delete_resource(resource)
            else:
                click.echo(f"Resource {resource.get('ID', resource['Name'])} has not been deleted.")
    else:
        click.echo(f"No resources found with prefix '{prefix}'.")


if __name__ == "__main__":
    main()
