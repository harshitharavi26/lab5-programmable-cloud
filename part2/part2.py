#!/usr/bin/env python3
"""
CSCI 5253 - Lab 5, Part 2: Clone a machine

Snapshots the boot disk of the Part 1 VM (flask-vm), then creates three
new VM instances from that snapshot while measuring how long each
instance takes to create. Results are written to TIMING.md.

Attribution:
Instance/disk creation patterns are adapted from Google Cloud's
official Python Compute Engine samples:
https://github.com/GoogleCloudPlatform/python-docs-samples/tree/main/compute/client_library/snippets
"""

import time
import google.auth
from google.cloud import compute_v1

# Application Default Credentials, same as Part 1.
credentials, project = google.auth.default()

ZONE = "us-west1-b"
SOURCE_INSTANCE_NAME = "flask-vm"   # the VM created in Part 1
NETWORK_TAG = "allow-5000"          # reuse the same firewall tag from Part 1

# This startup script is intentionally minimal: the snapshot already
# contains a fully-installed Flask app (apt packages, cloned repo,
# initialized database), since it was taken from flask-vm *after* Part 1's
# startup script finished. All we need to do on boot is start Flask again
# — re-running apt-get/git/pip here would defeat the point of using a
# snapshot (fast, pre-configured VM creation) and would skew our timings.
SNAPSHOT_STARTUP_SCRIPT = """#!/bin/bash
cd /flask-tutorial
export FLASK_APP=flaskr
nohup flask run -h 0.0.0.0 > /var/log/flask.log 2>&1 &
"""


def get_instance(project, zone, instance_name):
    """Fetch full details of an existing VM instance."""
    instance_client = compute_v1.InstancesClient(credentials=credentials)
    return instance_client.get(project=project, zone=zone, instance=instance_name)


def get_boot_disk_name(project, zone, instance_name):
    """
    Find the name of the boot disk attached to the given instance.

    A VM can have multiple attached disks, so we check the `boot` flag
    to find the one containing the OS and the Flask app installed in
    Part 1. `disk.source` is a full resource URL; the disk name is the
    last path segment.
    """
    instance = get_instance(project, zone, instance_name)
    for disk in instance.disks:
        if disk.boot:
            return disk.source.split("/")[-1]
    raise RuntimeError(f"No boot disk found for instance '{instance_name}'")


def create_snapshot(project, zone, disk_name, snapshot_name):
    """
    Create a snapshot of the given zonal persistent disk.

    Per the assignment, the snapshot name must follow the format
    'base-snapshot-<instance-name>'.
    """
    disk_client = compute_v1.DisksClient(credentials=credentials)

    snapshot = compute_v1.Snapshot()
    snapshot.name = snapshot_name

    print(f"Creating snapshot '{snapshot_name}' from disk '{disk_name}'...")
    operation = disk_client.create_snapshot(
        project=project, zone=zone, disk=disk_name, snapshot_resource=snapshot
    )

    # Snapshot creation is a zonal operation (tied to the disk's zone).
    op_client = compute_v1.ZoneOperationsClient(credentials=credentials)
    while operation.status != compute_v1.Operation.Status.DONE:
        operation = op_client.wait(project=project, zone=zone, operation=operation.name)
    if operation.error:
        raise Exception(operation.error)

    print(f"Snapshot '{snapshot_name}' created.")


def disk_from_snapshot(disk_type, disk_size_gb, source_snapshot):
    """Build a boot disk (AttachedDisk) sourced from an existing snapshot."""
    disk = compute_v1.AttachedDisk()
    initialize_params = compute_v1.AttachedDiskInitializeParams()
    initialize_params.source_snapshot = source_snapshot
    initialize_params.disk_type = disk_type
    initialize_params.disk_size_gb = disk_size_gb
    disk.initialize_params = initialize_params
    disk.auto_delete = True
    disk.boot = True
    return disk


def create_instance_from_snapshot(project, zone, instance_name, snapshot_link, tag):
    """
    Create a new VM instance whose boot disk is built from `snapshot_link`
    instead of a public OS image. Because the snapshot already contains
    Flask fully installed, this VM comes up ready to serve traffic much
    faster than the original Part 1 VM did.
    """
    instance_client = compute_v1.InstancesClient(credentials=credentials)

    disk_type = f"zones/{zone}/diskTypes/pd-balanced"
    disk = disk_from_snapshot(disk_type, 10, snapshot_link)

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
    # Reuse the allow-5000 tag so these VMs are also reachable on port 5000
    # under the same firewall rule created in Part 1.
    instance.tags = compute_v1.Tags(items=[tag])
    instance.metadata = compute_v1.Metadata(
        items=[compute_v1.Items(key="startup-script", value=SNAPSHOT_STARTUP_SCRIPT)]
    )

    request = compute_v1.InsertInstanceRequest(
        project=project, zone=zone, instance_resource=instance
    )

    operation = instance_client.insert(request=request)

    op_client = compute_v1.ZoneOperationsClient(credentials=credentials)
    while operation.status != compute_v1.Operation.Status.DONE:
        operation = op_client.wait(project=project, zone=zone, operation=operation.name)
    if operation.error:
        raise Exception(operation.error)

    return instance_client.get(project=project, zone=zone, instance=instance_name)


if __name__ == "__main__":
    # --- Step 1: snapshot flask-vm's boot disk ---
    boot_disk_name = get_boot_disk_name(project, ZONE, SOURCE_INSTANCE_NAME)
    snapshot_name = f"base-snapshot-{SOURCE_INSTANCE_NAME}"
    create_snapshot(project, ZONE, boot_disk_name, snapshot_name)

    # Full resource path required when referencing the snapshot as a disk source.
    snapshot_link = f"projects/{project}/global/snapshots/{snapshot_name}"

    # --- Step 2: create 3 instances from the snapshot, timing each one ---
    timings = []
    for i in range(3):
        instance_name = f"snapshot-vm-{i + 1}"
        print(f"Creating instance '{instance_name}'...")

        start = time.perf_counter()
        create_instance_from_snapshot(project, ZONE, instance_name, snapshot_link, NETWORK_TAG)
        elapsed = time.perf_counter() - start

        timings.append((instance_name, elapsed))
        print(f"'{instance_name}' created in {elapsed:.2f} seconds.")

    # --- Step 3: record the timing results ---
    with open("TIMING.md", "w") as f:
        f.write("# VM Creation Timing\n\n")
        f.write("| Instance | Creation Time (seconds) |\n")
        f.write("|---|---:|\n")
        for name, t in timings:
            f.write(f"| {name} | {t:.2f} |\n")

    print("\nTiming results written to TIMING.md")