from setuptools import find_packages, setup

setup(
    name="awsdeleter",
    version="0.1.2",
    description="A CLI tool to search and delete AWS EC2, S3, and VPC resources by prefix",
    author="Omkar Khatavkar",
    author_email="okhatavkar007@gmail.com",
    license="MIT",
    packages=find_packages(exclude=["__pycache__"]),
    install_requires=[
        "boto3",
        "click",
    ],
    entry_points={
        "console_scripts": [
            "awsdeleter = awsdeleter.awsdeleter:main",
        ],
    },
    include_package_data=True,
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.6",
)
