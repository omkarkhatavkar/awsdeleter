
# AWS Deleter

A CLI tool to delete AWS resources (EC2, S3, VPC) by prefix.

## Installation

Install via pip:

```bash
pip install awsdeleter
```

## Usage

Delete resources with a specified prefix:

```bash
aws_deleter <prefix> --resource <resource_type> --confirm yes
```

- `<prefix>`: Resource name prefix to search for.
- `--resource`: Specify resource type (`ec2`, `s3`, `vpc`).
- `--confirm`: Delete without confirmation.


### Example

Delete EC2 instances starting with "test":

```bash
aws_deleter test --resource ec2 --confirm yes
```

## License

MIT License. See [LICENSE](LICENSE).
