#!/usr/bin/env python3

import google.auth
from google.cloud import compute_v1
from google.api_core.exceptions import NotFound

credentials, project = google.auth.default()

ZONE = "us-west1-b"
FIREWALL_NAME = "allow-5000"
NETWORK_TAG = "allow-5000"

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
    instance_client = compute_v1.InstancesClient(credentials=credentials)
    return instance_client.list(project=project, zone=zone)

def firewall_rule_exists(project, firewall_name):
    firewall_client = compute_v1.FirewallsClient(credentials=credentials)
    try:
        firewall_client.get(project=project, firewall=firewall_name)
        return True
    except NotFound:
        return False


def create_firewall_rule(project, firewall_name, tag, port="5000"):
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
    firewall_rule.target_tags = [tag]

    print(f"Creating firewall rule '{firewall_name}'...")
    operation = firewall_client.insert(project=project, firewall_resource=firewall_rule)

    op_client = compute_v1.GlobalOperationsClient(credentials=credentials)
    while operation.status != compute_v1.Operation.Status.DONE:
        operation = op_client.wait(project=project, operation=operation.name)
    if operation.error:
        raise Exception(operation.error)

    print(f"Firewall rule '{firewall_name}' created.")

def get_ubuntu_image():
    image_client = compute_v1.ImagesClient(credentials=credentials)
    return image_client.get_from_family(project="ubuntu-os-cloud", family="ubuntu-2204-lts")

def disk_from_image(disk_type, disk_size_gb, source_image):
    disk = compute_v1.AttachedDisk()
    initialize_params = compute_v1.AttachedDiskInitializeParams()
    initialize_params.source_image = source_image
    initialize_params.disk_size_gb = disk_size_gb
    initialize_params.disk_type = disk_type
    disk.initialize_params = initialize_params
    disk.auto_delete = True
    disk.boot = True
    return disk

def create_instance(project, zone, instance_name, tag):
    instance_client = compute_v1.InstancesClient(credentials=credentials)

    ubuntu_image = get_ubuntu_image()
    disk_type = f"zones/{zone}/diskTypes/pd-balanced"
    disk = disk_from_image(disk_type, 10, ubuntu_image.self_link)

    network_interface = compute_v1.NetworkInterface()
    network_interface.network = "global/networks/default"

    access_config = compute_v1.AccessConfig()
    access_config.name = "External NAT"
    access_config.type_ = compute_v1.AccessConfig.Type.ONE_TO_ONE_NAT.name
    network_interface.access_configs = [access_config]

    instance = compute_v1.Instance()
    instance.name = instance_name
    instance.machine_type = f"zones/{zone}/machineTypes/e2-micro"
    instance.disks = [disk]
    instance.network_interfaces = [network_interface]
    instance.tags = compute_v1.Tags(items=[tag])
    instance.metadata = compute_v1.Metadata(
        items=[compute_v1.Items(key="startup-script", value=STARTUP_SCRIPT)]
    )

    request = compute_v1.InsertInstanceRequest(
        project=project, zone=zone, instance_resource=instance
    )

    print(f"Creating instance '{instance_name}'...")
    operation = instance_client.insert(request=request)

    op_client = compute_v1.ZoneOperationsClient(credentials=credentials)
    while operation.status != compute_v1.Operation.Status.DONE:
        operation = op_client.wait(project=project, zone=zone, operation=operation.name)
    if operation.error:
        raise Exception(operation.error)

    print(f"Instance '{instance_name}' created.")
    return instance_client.get(project=project, zone=zone, instance=instance_name)

if __name__ == "__main__":
    INSTANCE_NAME = "flask-vm"

    print("Your running instances are:")
    for instance in list_instances(project, ZONE):
        print(instance.name)

    create_firewall_rule(project, FIREWALL_NAME, NETWORK_TAG)

    vm = create_instance(project, ZONE, INSTANCE_NAME, NETWORK_TAG)

    external_ip = vm.network_interfaces[0].access_configs[0].nat_i_p
    print(f"\nThe Flask application is available at:\nhttp://{external_ip}:5000")