#!/usr/bin/env python3
"""
CSCI 5253 - Lab 5, Part 3

Creates VM-1 with a service account attached. VM-1 retrieves a Python
program and VM-2 startup script from instance metadata. The Python
program running on VM-1 uses the attached service account to create
VM-2, which runs the Flask tutorial application.

No service-account credential file is copied to VM-1 because VM-1
authenticates using its attached service account.
"""

import google.auth
from google.cloud import compute_v1


credentials, project = google.auth.default()

ZONE = "us-west1-b"
PARENT_INSTANCE_NAME = "vm1-launcher"
CHILD_INSTANCE_NAME = "flask-vm-child"

NETWORK_TAG = "allow-5000"

SERVICE_ACCOUNT_EMAIL = (
    "lab5-vm-launcher@hara-lab5-2026.iam.gserviceaccount.com"
)


# -------------------------------------------------------------------
# Startup script for VM-2
# -------------------------------------------------------------------
# This is the same basic Flask installation/startup logic from Part 1.
# VM-1 will retrieve this from its metadata and provide it to VM-2.

VM2_STARTUP_SCRIPT = """#!/bin/bash
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


# -------------------------------------------------------------------
# Python program that runs INSIDE VM-1 and creates VM-2
# -------------------------------------------------------------------

VM1_LAUNCH_VM2_CODE = f'''#!/usr/bin/env python3

import google.auth
from google.cloud import compute_v1


credentials, project = google.auth.default()

ZONE = "{ZONE}"
CHILD_INSTANCE_NAME = "{CHILD_INSTANCE_NAME}"
NETWORK_TAG = "{NETWORK_TAG}"


def get_ubuntu_image():
    image_client = compute_v1.ImagesClient(
        credentials=credentials
    )

    return image_client.get_from_family(
        project="ubuntu-os-cloud",
        family="ubuntu-2204-lts"
    )


def create_vm2(project, zone, instance_name, tag):

    instance_client = compute_v1.InstancesClient(
        credentials=credentials
    )

    # Get Ubuntu image.
    ubuntu_image = get_ubuntu_image()

    # Configure boot disk.
    disk = compute_v1.AttachedDisk()

    initialize_params = (
        compute_v1.AttachedDiskInitializeParams()
    )

    initialize_params.source_image = ubuntu_image.self_link
    initialize_params.disk_size_gb = 10
    initialize_params.disk_type = (
        f"zones/{{zone}}/diskTypes/pd-balanced"
    )

    disk.initialize_params = initialize_params
    disk.auto_delete = True
    disk.boot = True

    # Configure networking.
    network_interface = compute_v1.NetworkInterface()
    network_interface.network = "global/networks/default"

    access_config = compute_v1.AccessConfig()
    access_config.name = "External NAT"

    access_config.type_ = (
        compute_v1.AccessConfig.Type.ONE_TO_ONE_NAT.name
    )

    network_interface.access_configs = [access_config]

    # Read VM-2's startup script from VM-1 metadata.
    import urllib.request

    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/"
        "instance/attributes/vm2-startup-script"
    )

    request.add_header(
        "Metadata-Flavor",
        "Google"
    )

    with urllib.request.urlopen(request) as response:
        vm2_startup_script = response.read().decode("utf-8")

    # Build VM-2.
    instance = compute_v1.Instance()

    instance.name = instance_name

    instance.machine_type = (
        f"zones/{{zone}}/machineTypes/e2-micro"
    )

    instance.disks = [disk]

    instance.network_interfaces = [
        network_interface
    ]

    instance.tags = compute_v1.Tags(
        items=[tag]
    )

    instance.metadata = compute_v1.Metadata(
        items=[
            compute_v1.Items(
                key="startup-script",
                value=vm2_startup_script
            )
        ]
    )

    print(
        f"Creating VM-2 '{{instance_name}}'..."
    )

    request = compute_v1.InsertInstanceRequest(
        project=project,
        zone=zone,
        instance_resource=instance
    )

    operation = instance_client.insert(
        request=request
    )

    op_client = compute_v1.ZoneOperationsClient(
        credentials=credentials
    )

    while operation.status != compute_v1.Operation.Status.DONE:

        operation = op_client.wait(
            project=project,
            zone=zone,
            operation=operation.name
        )

    if operation.error:
        raise Exception(operation.error)

    return instance_client.get(
        project=project,
        zone=zone,
        instance=instance_name
    )


if __name__ == "__main__":

    vm2 = create_vm2(
        project,
        ZONE,
        CHILD_INSTANCE_NAME,
        NETWORK_TAG
    )

    external_ip = (
        vm2.network_interfaces[0]
        .access_configs[0]
        .nat_i_p
    )

    print(
        f"VM-2 created successfully."
    )

    print(
        f"Flask application: "
        f"http://{{external_ip}}:5000"
    )
'''


# -------------------------------------------------------------------
# Startup script for VM-1
# -------------------------------------------------------------------
#
# VM-1 retrieves the VM-2 creation program from its own metadata,
# installs the necessary Google Cloud Python libraries, and executes it.

VM1_STARTUP_SCRIPT = """#!/bin/bash
set -e

mkdir -p /srv
cd /srv

apt-get update
apt-get install -y python3 python3-pip curl

pip3 install google-cloud-compute google-auth

curl \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/vm1-launch-vm2-code \
  -H "Metadata-Flavor: Google" \
  > /srv/vm1-launch-vm2-code.py

python3 /srv/vm1-launch-vm2-code.py \
  > /var/log/create-vm2.log 2>&1
"""


# -------------------------------------------------------------------
# Helper functions for creating VM-1
# -------------------------------------------------------------------

def get_ubuntu_image():

    image_client = compute_v1.ImagesClient(
        credentials=credentials
    )

    return image_client.get_from_family(
        project="ubuntu-os-cloud",
        family="ubuntu-2204-lts"
    )


def disk_from_image(
    disk_type,
    disk_size_gb,
    source_image
):

    disk = compute_v1.AttachedDisk()

    initialize_params = (
        compute_v1.AttachedDiskInitializeParams()
    )

    initialize_params.source_image = source_image
    initialize_params.disk_size_gb = disk_size_gb
    initialize_params.disk_type = disk_type

    disk.initialize_params = initialize_params

    disk.auto_delete = True
    disk.boot = True

    return disk


# -------------------------------------------------------------------
# Create VM-1
# -------------------------------------------------------------------

def create_vm1(
    project,
    zone,
    instance_name,
    service_account_email
):

    instance_client = compute_v1.InstancesClient(
        credentials=credentials
    )

    ubuntu_image = get_ubuntu_image()

    disk_type = (
        f"zones/{zone}/diskTypes/pd-balanced"
    )

    disk = disk_from_image(
        disk_type,
        10,
        ubuntu_image.self_link
    )

    # Networking
    network_interface = compute_v1.NetworkInterface()

    network_interface.network = (
        "global/networks/default"
    )

    access_config = compute_v1.AccessConfig()

    access_config.name = "External NAT"

    access_config.type_ = (
        compute_v1.AccessConfig.Type.ONE_TO_ONE_NAT.name
    )

    network_interface.access_configs = [
        access_config
    ]

    # Create VM-1 configuration.
    instance = compute_v1.Instance()

    instance.name = instance_name

    instance.machine_type = (
        f"zones/{zone}/machineTypes/e2-micro"
    )

    instance.disks = [disk]

    instance.network_interfaces = [
        network_interface
    ]

    # ---------------------------------------------------------------
    # Pass everything VM-1 needs through metadata.
    # ---------------------------------------------------------------

    instance.metadata = compute_v1.Metadata(
        items=[

            compute_v1.Items(
                key="startup-script",
                value=VM1_STARTUP_SCRIPT
            ),

            compute_v1.Items(
                key="vm1-launch-vm2-code",
                value=VM1_LAUNCH_VM2_CODE
            ),

            compute_v1.Items(
                key="vm2-startup-script",
                value=VM2_STARTUP_SCRIPT
            )
        ]
    )

    # ---------------------------------------------------------------
    # Attach the service account to VM-1.
    #
    # VM-1 can now use google.auth.default() and obtain credentials
    # automatically from Google's metadata server.
    # ---------------------------------------------------------------

    service_account = compute_v1.ServiceAccount()

    service_account.email = (
        service_account_email
    )

    service_account.scopes = [
        "https://www.googleapis.com/auth/cloud-platform"
    ]

    instance.service_accounts = [
        service_account
    ]

    request = compute_v1.InsertInstanceRequest(
        project=project,
        zone=zone,
        instance_resource=instance
    )

    print(
        f"Creating VM-1 '{instance_name}'..."
    )

    operation = instance_client.insert(
        request=request
    )

    op_client = (
        compute_v1.ZoneOperationsClient(
            credentials=credentials
        )
    )

    while (
        operation.status
        != compute_v1.Operation.Status.DONE
    ):

        operation = op_client.wait(
            project=project,
            zone=zone,
            operation=operation.name
        )

    if operation.error:
        raise Exception(operation.error)

    print(
        f"VM-1 '{instance_name}' created."
    )

    return instance_client.get(
        project=project,
        zone=zone,
        instance=instance_name
    )


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------

if __name__ == "__main__":

    print(
        "Creating VM-1 with service account attached..."
    )

    vm1 = create_vm1(
        project,
        ZONE,
        PARENT_INSTANCE_NAME,
        SERVICE_ACCOUNT_EMAIL
    )

    external_ip = (
        vm1.network_interfaces[0]
        .access_configs[0]
        .nat_i_p
    )

    print(
        f"\nVM-1 created successfully."
    )

    print(
        f"VM-1 external IP: {external_ip}"
    )

    print(
        "\nVM-1 will now automatically create VM-2."
    )

    print(
        "Give the startup script a few minutes to finish."
    )

    print(
        f"VM-2 will be named: {CHILD_INSTANCE_NAME}"
    )