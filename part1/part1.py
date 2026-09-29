#!/usr/bin/env python3
"""
CSCI 5253 - Lab 5, Part 1

Creates a Compute Engine VM that installs and runs the Flask tutorial
application (flaskr), opens a firewall rule for port 5000, and prints
the URL where the app can be reached.

Attribution:
Instance/disk/firewall creation patterns are adapted from Google Cloud's
official Python Compute Engine samples:
https://github.com/GoogleCloudPlatform/python-docs-samples/tree/main/compute/client_library/snippets
"""

import google.auth
from google.cloud import compute_v1
from google.api_core.exceptions import NotFound

# Application Default Credentials — set up earlier via
# `gcloud auth application-default login`. `project` is pulled from the
# active gcloud config rather than hardcoded.
credentials, project = google.auth.default()

ZONE = "us-west1-b"
FIREWALL_NAME = "allow-5000"
NETWORK_TAG = "allow-5000"

# Startup script that GCE runs automatically once the VM boots.
# Installs Python/git, clones the flaskr tutorial app, initializes its
# database, and starts the Flask dev server in the background (nohup so
# it survives after the startup script itself exits).
STARTUP_SCRIPT = """#!/bin/bash
set -e

apt-get update
apt-get install -y python3 python3-pip git

git clone https://github.com/cu-csci-4253-datacenter/flask-tutorial /flask-tutorial
cd /flask-tutorial

python3 setup.py install
pip3 install -e .

export FLASK_APP=flaskr
flask init-db

nohup flask run -h 0.0.0.0 > /var/log/flask.log 2>&1 &
"""


def list_instances(project, zone):
    """Return all VM instances currently running in the given zone."""
    instance_client = compute_v1.InstancesClient(credentials=credentials)
    return instance_client.list(project=project, zone=zone)


def firewall_rule_exists(project, firewall_name):
    """
    Check whether a firewall rule with this name already exists.

    The Compute API raises NotFound (rather than returning None) when a
    resource doesn't exist, so we use that to detect absence.
    """
    firewall_client = compute_v1.FirewallsClient(credentials=credentials)
    try:
        firewall_client.get(project=project, firewall=firewall_name)
        return True
    except NotFound:
        return False


def create_firewall_rule(project, firewall_name, tag, port="5000"):
    """
    Create a firewall rule allowing TCP traffic on `port` from anywhere,
    scoped to instances carrying `tag`. Skips creation if the rule
    already exists, since firewall rules are project-wide (not
    per-instance) and only need to be created once.
    """
    if firewall_rule_exists(project, firewall_name):
        print(f"Firewall rule '{firewall_name}' already exists, skipping creation.")
        return

    firewall_client = compute_v1.FirewallsClient(credentials=credentials)

    allowed = compute_v1.Allowed()
    allowed.I_p_protocol = "tcp"
    allowed.ports = [port]

    firewall_rule = compute_v1.Firewall()
    firewall_rule.name = firewall_name
    firewall_rule.direction = "INGRESS"
    firewall_rule.network = "global/networks/default"
    firewall_rule.allowed = [allowed]
    firewall_rule.source_ranges = ["0.0.0.0/0"]
    # target_tags scopes this rule to only instances with this network
    # tag, so it doesn't open port 5000 on every VM in the project.
    firewall_rule.target_tags = [tag]

    print(f"Creating firewall rule '{firewall_name}'...")
    operation = firewall_client.insert(project=project, firewall_resource=firewall_rule)

    # Firewall rules are global (not zonal) resources, so we poll with
    # GlobalOperationsClient rather than ZoneOperationsClient.
    op_client = compute_v1.GlobalOperationsClient(credentials=credentials)
    while operation.status != compute_v1.Operation.Status.DONE:
        operation = op_client.wait(project=project, operation=operation.name)
    if operation.error:
        raise Exception(operation.error)

    print(f"Firewall rule '{firewall_name}' created.")


def get_ubuntu_image():
    """
    Look up the latest image in the ubuntu-2204-lts family rather than
    hardcoding a specific image name/version, per the assignment spec.
    """
    image_client = compute_v1.ImagesClient(credentials=credentials)
    return image_client.get_from_family(project="ubuntu-os-cloud", family="ubuntu-2204-lts")


def disk_from_image(disk_type, disk_size_gb, source_image):
    """Build a boot disk (AttachedDisk) sourced from the given image."""
    disk = compute_v1.AttachedDisk()
    initialize_params = compute_v1.AttachedDiskInitializeParams()
    initialize_params.source_image = source_image
    initialize_params.disk_size_gb = disk_size_gb
    initialize_params.disk_type = disk_type
    disk.initialize_params = initialize_params
    disk.auto_delete = True  # disk is deleted automatically when the VM is deleted
    disk.boot = True
    return disk


def create_instance(project, zone, instance_name, tag):
    """
    Create the VM instance: boot disk from the Ubuntu image family, a
    network interface with an external (ONE_TO_ONE_NAT) IP so the app
    is reachable from the internet, the allow-5000 network tag, and the
    startup script attached via instance metadata.
    """
    instance_client = compute_v1.InstancesClient(credentials=credentials)

    ubuntu_image = get_ubuntu_image()
    disk_type = f"zones/{zone}/diskTypes/pd-balanced"
    disk = disk_from_image(disk_type, 10, ubuntu_image.self_link)

    network_interface = compute_v1.NetworkInterface()
    network_interface.network = "global/networks/default"

    # ONE_TO_ONE_NAT gives the VM an external IP address so it's
    # reachable from outside GCP's internal network.
    access_config = compute_v1.AccessConfig()
    access_config.name = "External NAT"
    access_config.type_ = compute_v1.AccessConfig.Type.ONE_TO_ONE_NAT.name
    network_interface.access_configs = [access_config]

    instance = compute_v1.Instance()
    instance.name = instance_name
    instance.machine_type = f"zones/{zone}/machineTypes/e2-micro"
    instance.disks = [disk]
    instance.network_interfaces = [network_interface]
    # Tag lets the allow-5000 firewall rule target this VM specifically.
    instance.tags = compute_v1.Tags(items=[tag])
    instance.metadata = compute_v1.Metadata(
        items=[compute_v1.Items(key="startup-script", value=STARTUP_SCRIPT)]
    )

    request = compute_v1.InsertInstanceRequest(
        project=project, zone=zone, instance_resource=instance
    )

    print(f"Creating instance '{instance_name}'...")
    operation = instance_client.insert(request=request)

    # Instance creation is a zonal operation, so we poll with
    # ZoneOperationsClient here (vs. GlobalOperationsClient above).
    op_client = compute_v1.ZoneOperationsClient(credentials=credentials)
    while operation.status != compute_v1.Operation.Status.DONE:
        operation = op_client.wait(project=project, zone=zone, operation=operation.name)
    if operation.error:
        raise Exception(operation.error)

    print(f"Instance '{instance_name}' created.")
    # Re-fetch the instance so the returned object includes the
    # assigned external IP address (not present on the request object).
    return instance_client.get(project=project, zone=zone, instance=instance_name)


if __name__ == "__main__":
    INSTANCE_NAME = "flask-vm"

    # Informational only — shows what's running before this script
    # creates anything new.
    print("Your running instances are:")
    for instance in list_instances(project, ZONE):
        print(instance.name)

    create_firewall_rule(project, FIREWALL_NAME, NETWORK_TAG)

    vm = create_instance(project, ZONE, INSTANCE_NAME, NETWORK_TAG)

    # Pull the external IP out of the instance's network interface so we
    # can print a clickable URL for the user, instead of hardcoding one.
    external_ip = vm.network_interfaces[0].access_configs[0].nat_i_p
    print(f"\nThe Flask application is available at:\nhttp://{external_ip}:5000")