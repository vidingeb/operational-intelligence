from mcp.server.fastmcp import FastMCP
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import ssl
import os
import atexit
from datetime import datetime, timezone, timedelta

mcp = FastMCP("vcenter-local")

VCENTER = "vc01.vcf.local"
VC_USER = os.getenv("VCENTER_USER", "administrator@vsphere.local")
VC_PASS = os.getenv("VCENTER_PASSWORD", "")

def get_si():
    context = ssl._create_unverified_context()
    si = SmartConnect(host=VCENTER, user=VC_USER, pwd=VC_PASS, sslContext=context)
    atexit.register(Disconnect, si)
    return si

@mcp.tool()
def list_vms() -> list:
    """List virtual machines from vCenter."""
    si = get_si()
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)

    result = []
    for vm in view.view:
        result.append({
            "name": vm.name,
            "power_state": str(vm.runtime.powerState),
            "cpu": vm.config.hardware.numCPU if vm.config else None,
            "memory_mb": vm.config.hardware.memoryMB if vm.config else None
        })

    view.Destroy()
    return result

@mcp.tool()
def list_hosts() -> list:
    """List ESXi hosts from vCenter."""
    si = get_si()
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)

    result = []
    for host in view.view:
        result.append({
            "name": host.name,
            "connection_state": str(host.runtime.connectionState),
            "power_state": str(host.runtime.powerState),
            "version": host.config.product.version,
            "build": host.config.product.build
        })

    view.Destroy()
    return result

@mcp.tool()
def list_old_snapshots(days: int = 14) -> list:
    """List VM snapshots older than a given number of days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    si = get_si()
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)

    result = []

    def walk_snapshot_tree(vm, snapshots):
        for snap in snapshots:
            created = snap.createTime
            if created < cutoff:
                result.append({
                    "vm": vm.name,
                    "snapshot": snap.name,
                    "created": created.isoformat(),
                    "age_days": (datetime.now(timezone.utc) - created).days,
                    "description": snap.description
                })
            if snap.childSnapshotList:
                walk_snapshot_tree(vm, snap.childSnapshotList)

    for vm in view.view:
        if vm.snapshot:
            walk_snapshot_tree(vm, vm.snapshot.rootSnapshotList)

    view.Destroy()
    return result
print("Testing vCenter connectivity...")
print(list_hosts())

if __name__ == "__main__":
    mcp.run()