Cisco APIC vPC Automation Script
Overview

This script is designed to automate provisioning tasks on Cisco APIC (Application Policy Infrastructure Controller), specifically for configuring vPC (Virtual Port Channel) in a Cisco ACI environment.

The main objective is to accelerate repetitive configuration tasks, reduce human error, and ensure consistency across fabric deployments.

Key Features
    Automated creation of Interface Policy Group (IPG) for vPC
    Automated creation of Port Selector and assignment to leaf nodes and interfaces
    Automated static port binding from EPG to vPC
    Integration with Cisco APIC REST API
    Parameterized execution for flexible deployment
    Use Cases
    vPC provisioning in Cisco ACI fabric
    Service deployment into Endpoint Groups (EPG)
    Standardized interface policy configuration
    Reducing manual configuration effort in large-scale environments
    Automation Workflow


    Input parameters (node, interface, tenant, EPG, etc.)
    Create Interface Policy Group (IPG)
    Create and assign Port Selector to nodes and interfaces
    Bind static port to EPG using vPC
    Verify configuration on APIC
    Requirements
    Python 3.x
    Required libraries:
    requests
    json
    urllib3

Install dependencies:

    pip install -r requirements.txt
    Configuration

The script requires the following parameters:

    APIC Host / URL
    Username & Password
    Leaf Node IDs (vPC pair)
    Interface (e.g., eth1/1)
    Tenant / Application Profile / EPG

Security Note:
    Do not store credentials in the repository. Use environment variables or local configuration files that are excluded via .gitignore.

License

    Internal Use / Project Delivery
