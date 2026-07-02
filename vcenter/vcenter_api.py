from fastapi import FastAPI, Query
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import ssl
import os
import atexit
from datetime import datetime, timezone, timedelta

app = FastAPI(
    title="vCenter Local API",
    version="1.1.0",
    description="Read-only vCenter API for Copilot Studio via MAP gateway",
    openapi_version="3.0.3"
)

VCENTER = "vc01.vcf.local"
VC_USER = os.getenv("VCENTER_USER")
VC_PASS = os.getenv("VCENTER_PASSWORD")


def get_si():
    context = ssl._create_unverified_context()
    si = SmartConnect(
        host=VCENTER,
        user=VC_USER,
        pwd=VC_PASS,
        sslContext=context
    )
    atexit.register(Disconnect, si)
    return si


def get_view(content, vim_type):
    return content.viewManager.CreateContainerView(
        content.rootFolder,
        [vim_type],
        True
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "vcenter": VCENTER
    }


@app.get("/hosts")
def list_hosts():
    si = get_si()
    content = si.RetrieveContent()
    view = get_view(content, vim.HostSystem)

    result = []
    for host in view.view:
        summary = host.summary
        result.append({
            "name": host.name,
            "version": host.config.product.version,
            "build": host.config.product.build,
            "connection_state": str(host.runtime.connectionState),
            "power_state": str(host.runtime.powerState),
            "maintenance_mode": host.runtime.inMaintenanceMode,
            "cpu_model": summary.hardware.cpuModel,
            "cpu_cores": summary.hardware.numCpuCores,
            "cpu_threads": summary.hardware.numCpuThreads,
            "memory_gb": round(summary.hardware.memorySize / 1024 / 1024 / 1024, 2)
        })

    view.Destroy()
    return result


@app.get("/vms")
def list_vms():
    si = get_si()
    content = si.RetrieveContent()
    view = get_view(content, vim.VirtualMachine)

    result = []
    for vm in view.view:
        guest_ip = None
        guest_os = None
        tools_status = None

        if vm.guest:
            guest_ip = vm.guest.ipAddress
            guest_os = vm.guest.guestFullName
            tools_status = str(vm.guest.toolsStatus)

        host_name = vm.runtime.host.name if vm.runtime and vm.runtime.host else None

        result.append({
            "name": vm.name,
            "power_state": str(vm.runtime.powerState),
            "cpu": vm.config.hardware.numCPU if vm.config else None,
            "memory_mb": vm.config.hardware.memoryMB if vm.config else None,
            "guest_os": guest_os,
            "guest_ip": guest_ip,
            "vmware_tools_status": tools_status,
            "host": host_name
        })

    view.Destroy()
    return result


@app.get("/vms/search")
def search_vms(name: str = Query(..., description="Search string for VM name")):
    si = get_si()
    content = si.RetrieveContent()
    view = get_view(content, vim.VirtualMachine)

    result = []
    for vm in view.view:
        if name.lower() in vm.name.lower():
            result.append({
                "name": vm.name,
                "power_state": str(vm.runtime.powerState),
                "cpu": vm.config.hardware.numCPU if vm.config else None,
                "memory_mb": vm.config.hardware.memoryMB if vm.config else None,
                "guest_ip": vm.guest.ipAddress if vm.guest else None,
                "host": vm.runtime.host.name if vm.runtime and vm.runtime.host else None
            })

    view.Destroy()
    return result


@app.get("/vms/poweredoff")
def powered_off_vms():
    si = get_si()
    content = si.RetrieveContent()
    view = get_view(content, vim.VirtualMachine)

    result = []
    for vm in view.view:
        if str(vm.runtime.powerState) == "poweredOff":
            result.append({
                "name": vm.name,
                "power_state": str(vm.runtime.powerState),
                "host": vm.runtime.host.name if vm.runtime and vm.runtime.host else None
            })

    view.Destroy()
    return result


@app.get("/datastores")
def list_datastores():
    si = get_si()
    content = si.RetrieveContent()
    view = get_view(content, vim.Datastore)

    result = []
    for ds in view.view:
        summary = ds.summary
        capacity_gb = summary.capacity / 1024 / 1024 / 1024
        free_gb = summary.freeSpace / 1024 / 1024 / 1024
        used_gb = capacity_gb - free_gb
        used_percent = (used_gb / capacity_gb * 100) if capacity_gb else 0

        result.append({
            "name": summary.name,
            "type": summary.type,
            "accessible": summary.accessible,
            "capacity_gb": round(capacity_gb, 2),
            "free_gb": round(free_gb, 2),
            "used_gb": round(used_gb, 2),
            "used_percent": round(used_percent, 2)
        })

    view.Destroy()
    return result


@app.get("/datastores/lowfree")
def datastores_low_free(threshold_percent: int = 20):
    si = get_si()
    content = si.RetrieveContent()
    view = get_view(content, vim.Datastore)

    result = []
    for ds in view.view:
        summary = ds.summary
        capacity_gb = summary.capacity / 1024 / 1024 / 1024
        free_gb = summary.freeSpace / 1024 / 1024 / 1024
        free_percent = (free_gb / capacity_gb * 100) if capacity_gb else 0

        if free_percent < threshold_percent:
            result.append({
                "name": summary.name,
                "type": summary.type,
                "capacity_gb": round(capacity_gb, 2),
                "free_gb": round(free_gb, 2),
                "free_percent": round(free_percent, 2)
            })

    view.Destroy()
    return result


@app.get("/clusters")
def list_clusters():
    si = get_si()
    content = si.RetrieveContent()
    view = get_view(content, vim.ClusterComputeResource)

    result = []
    for cluster in view.view:
        hosts = cluster.host or []
        result.append({
            "name": cluster.name,
            "host_count": len(hosts),
            "drs_enabled": cluster.configuration.drsConfig.enabled if cluster.configuration else None,
            "ha_enabled": cluster.configuration.dasConfig.enabled if cluster.configuration else None
        })

    view.Destroy()
    return result


@app.get("/alarms")
def active_alarms():
    si = get_si()
    content = si.RetrieveContent()

    result = []

    def walk_entity(entity):
        try:
            if hasattr(entity, "triggeredAlarmState") and entity.triggeredAlarmState:
                for alarm_state in entity.triggeredAlarmState:
                    result.append({
                        "entity": entity.name,
                        "alarm": alarm_state.alarm.info.name,
                        "overall_status": str(alarm_state.overallStatus),
                        "time": alarm_state.time.isoformat() if alarm_state.time else None
                    })
        except Exception:
            pass

        if hasattr(entity, "childEntity"):
            for child in entity.childEntity:
                walk_entity(child)

    walk_entity(content.rootFolder)
    return result


@app.get("/snapshots/old")
def old_snapshots(days: int = 14):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    si = get_si()
    content = si.RetrieveContent()
    view = get_view(content, vim.VirtualMachine)

    result = []

    def walk(vm, snaps):
        for snap in snaps:
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
                walk(vm, snap.childSnapshotList)

    for vm in view.view:
        if vm.snapshot:
            walk(vm, vm.snapshot.rootSnapshotList)

    view.Destroy()
    return result


@app.get("/vmtools/outdated")
def vmtools_outdated():
    si = get_si()
    content = si.RetrieveContent()
    view = get_view(content, vim.VirtualMachine)

    result = []
    for vm in view.view:
        if vm.guest and str(vm.guest.toolsStatus) not in ["toolsOk", "toolsNotRunning"]:
            result.append({
                "name": vm.name,
                "power_state": str(vm.runtime.powerState),
                "tools_status": str(vm.guest.toolsStatus),
                "guest_os": vm.guest.guestFullName
            })

    view.Destroy()
    return result
@app.get("/vm/details")

def vm_details(name: str):

    si = get_si()

    content = si.RetrieveContent()

    view = get_view(content, vim.VirtualMachine)

    for vm in view.view:

        if vm.name.lower() == name.lower():

            datastores = [ds.name for ds in vm.datastore] if vm.datastore else []

            result = {

                "name": vm.name,

                "power_state": str(vm.runtime.powerState),

                "cpu": vm.config.hardware.numCPU if vm.config else None,

                "memory_mb": vm.config.hardware.memoryMB if vm.config else None,

                "guest_os": vm.guest.guestFullName if vm.guest else None,

                "guest_ip": vm.guest.ipAddress if vm.guest else None,

                "vmware_tools_status": str(vm.guest.toolsStatus) if vm.guest else None,

                "host": vm.runtime.host.name if vm.runtime and vm.runtime.host else None,

                "datastores": datastores

            }

            view.Destroy()

            return result

    view.Destroy()

    return {"error": f"VM '{name}' not found"}

@app.get("/hosts/usage")

def host_usage():

    si = get_si()

    content = si.RetrieveContent()

    view = get_view(content, vim.HostSystem)

    result = []

    for host in view.view:

        summary = host.summary

        cpu_total_mhz = summary.hardware.cpuMhz * summary.hardware.numCpuCores

        cpu_usage_mhz = summary.quickStats.overallCpuUsage or 0

        cpu_usage_percent = (cpu_usage_mhz / cpu_total_mhz * 100) if cpu_total_mhz else 0

        mem_total_mb = summary.hardware.memorySize / 1024 / 1024

        mem_usage_mb = summary.quickStats.overallMemoryUsage or 0

        mem_usage_percent = (mem_usage_mb / mem_total_mb * 100) if mem_total_mb else 0

        result.append({

            "name": host.name,

            "connection_state": str(host.runtime.connectionState),

            "maintenance_mode": host.runtime.inMaintenanceMode,

            "cpu_usage_mhz": cpu_usage_mhz,

            "cpu_total_mhz": cpu_total_mhz,

            "cpu_usage_percent": round(cpu_usage_percent, 2),

            "memory_usage_mb": mem_usage_mb,

            "memory_total_mb": round(mem_total_mb, 2),

            "memory_usage_percent": round(mem_usage_percent, 2)

        })

    view.Destroy()

    return result

@app.get("/clusters/summary")

def cluster_summary():

    si = get_si()

    content = si.RetrieveContent()

    cluster_view = get_view(content, vim.ClusterComputeResource)

    vm_view = get_view(content, vim.VirtualMachine)

    result = []

    for cluster in cluster_view.view:

        hosts = cluster.host or []

        vm_count = 0

        for vm in vm_view.view:

            if vm.runtime.host in hosts:

                vm_count += 1

        result.append({

            "name": cluster.name,

            "host_count": len(hosts),

            "vm_count": vm_count,

            "ha_enabled": cluster.configuration.dasConfig.enabled if cluster.configuration else None,

            "drs_enabled": cluster.configuration.drsConfig.enabled if cluster.configuration else None

        })

    cluster_view.Destroy()

    vm_view.Destroy()

    return result

@app.get("/tasks/recent")

def recent_tasks(limit: int = 20):

    si = get_si()

    content = si.RetrieveContent()

    task_manager = content.taskManager

    recent_tasks = task_manager.recentTask[:limit]

    result = []

    for task in recent_tasks:

        info = task.info

        result.append({

            "name": info.name,

            "state": str(info.state),

            "entity": info.entityName,

            "start_time": info.startTime.isoformat() if info.startTime else None,

            "complete_time": info.completeTime.isoformat() if info.completeTime else None,

            "error": str(info.error.localizedMessage) if info.error else None

        })

    return result

@app.get("/events/recent")

def recent_events(limit: int = 20):

    si = get_si()

    content = si.RetrieveContent()

    event_manager = content.eventManager

    collector = event_manager.CreateCollectorForEvents(vim.event.EventFilterSpec())

    events = collector.ReadNextEvents(limit)

    collector.DestroyCollector()

    result = []

    for event in events:

        result.append({

            "created_time": event.createdTime.isoformat() if event.createdTime else None,

            "user_name": event.userName,

            "full_formatted_message": event.fullFormattedMessage,

            "event_type": event.__class__.__name__

        })

    return result

@app.get("/vm/storage")

def vm_storage(name: str):

    si = get_si()

    content = si.RetrieveContent()

    view = get_view(content, vim.VirtualMachine)

    for vm in view.view:

        if vm.name.lower() == name.lower():

            result = []

            for ds in vm.datastore:

                summary = ds.summary

                capacity_gb = summary.capacity / 1024 / 1024 / 1024

                free_gb = summary.freeSpace / 1024 / 1024 / 1024

                result.append({

                    "vm": vm.name,

                    "datastore": summary.name,

                    "type": summary.type,

                    "capacity_gb": round(capacity_gb, 2),

                    "free_gb": round(free_gb, 2)

                })

            view.Destroy()

            return result

    view.Destroy()

    return {"error": f"VM '{name}' not found"}

@app.get("/vm/snapshots")

def vm_snapshots(name: str):

    si = get_si()

    content = si.RetrieveContent()

    view = get_view(content, vim.VirtualMachine)

    result = []

    def walk(vm, snaps):

        for snap in snaps:

            result.append({

                "vm": vm.name,

                "snapshot": snap.name,

                "created": snap.createTime.isoformat(),

                "age_days": (datetime.now(timezone.utc) - snap.createTime).days,

                "description": snap.description

            })

            if snap.childSnapshotList:

                walk(vm, snap.childSnapshotList)

    for vm in view.view:

        if vm.name.lower() == name.lower():

            if vm.snapshot:

                walk(vm, vm.snapshot.rootSnapshotList)

            view.Destroy()

            return result

    view.Destroy()

    return {"error": f"VM '{name}' not found"}

@app.get("/vmtools/notrunning")

def vmtools_not_running():

    si = get_si()

    content = si.RetrieveContent()

    view = get_view(content, vim.VirtualMachine)

    result = []

    for vm in view.view:

        if vm.guest and str(vm.guest.toolsRunningStatus) != "guestToolsRunning":

            result.append({

                "name": vm.name,

                "power_state": str(vm.runtime.powerState),

                "tools_status": str(vm.guest.toolsStatus),

                "tools_running_status": str(vm.guest.toolsRunningStatus),

                "guest_os": vm.guest.guestFullName

            })

    view.Destroy()

    return result
def require_confirm(confirm: bool):
    if not confirm:
        return {
            "status": "confirmation_required",
            "message": "Add confirm=true to execute this action."
        }
    return None


def wait_task(task):
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        pass

    if task.info.state == vim.TaskInfo.State.error:
        return {
            "status": "error",
            "message": task.info.error.localizedMessage if task.info.error else "Unknown error"
        }

    return {"status": "success"}


def find_vm(content, name):
    view = get_view(content, vim.VirtualMachine)
    for vm in view.view:
        if vm.name.lower() == name.lower():
            view.Destroy()
            return vm
    view.Destroy()
    return None


def find_host(content, name):
    view = get_view(content, vim.HostSystem)
    for host in view.view:
        if host.name.lower() == name.lower():
            view.Destroy()
            return host
    view.Destroy()
    return None


def find_datastore(content, name):
    view = get_view(content, vim.Datastore)
    for ds in view.view:
        if ds.name.lower() == name.lower():
            view.Destroy()
            return ds
    view.Destroy()
    return None


@app.post("/vm/poweron")
def vm_poweron(name: str):
    si = get_si()
    content = si.RetrieveContent()
    vm_obj = find_vm(content, name)

    if not vm_obj:
        return {"status": "error", "message": f"VM '{name}' not found"}

    return wait_task(vm_obj.PowerOnVM_Task())


@app.post("/vm/poweroff")
def vm_poweroff(name: str):
    si = get_si()
    content = si.RetrieveContent()
    vm_obj = find_vm(content, name)

    if not vm_obj:
        return {"status": "error", "message": f"VM '{name}' not found"}

    return wait_task(vm_obj.PowerOffVM_Task())


@app.post("/vm/shutdown_guest")
def vm_shutdown_guest(name: str):
    si = get_si()
    content = si.RetrieveContent()
    vm_obj = find_vm(content, name)

    if not vm_obj:
        return {"status": "error", "message": f"VM '{name}' not found"}

    vm_obj.ShutdownGuest()
    return {"status": "success", "message": f"Guest shutdown requested for {name}"}


@app.post("/vm/reboot_guest")
def vm_reboot_guest(name: str):
    si = get_si()
    content = si.RetrieveContent()
    vm_obj = find_vm(content, name)

    if not vm_obj:
        return {"status": "error", "message": f"VM '{name}' not found"}

    vm_obj.RebootGuest()
    return {"status": "success", "message": f"Guest reboot requested for {name}"}


@app.post("/vm/reset")
def vm_reset(name: str):
    si = get_si()
    content = si.RetrieveContent()
    vm_obj = find_vm(content, name)

    if not vm_obj:
        return {"status": "error", "message": f"VM '{name}' not found"}

    return wait_task(vm_obj.ResetVM_Task())


@app.post("/vm/suspend")
def vm_suspend(name: str):
    si = get_si()
    content = si.RetrieveContent()
    vm_obj = find_vm(content, name)

    if not vm_obj:
        return {"status": "error", "message": f"VM '{name}' not found"}

    return wait_task(vm_obj.SuspendVM_Task())


@app.post("/vm/snapshot/create")
def vm_create_snapshot(
    name: str,
    snapshot_name: str,
    description: str = "",
    memory: bool = False,
    quiesce: bool = False
):
    si = get_si()
    content = si.RetrieveContent()
    vm_obj = find_vm(content, name)

    if not vm_obj:
        return {"status": "error", "message": f"VM '{name}' not found"}

    task = vm_obj.CreateSnapshot_Task(
        name=snapshot_name,
        description=description,
        memory=memory,
        quiesce=quiesce
    )

    return wait_task(task)


@app.post("/vm/snapshot/remove_all")
def vm_remove_all_snapshots(name: str):
    si = get_si()
    content = si.RetrieveContent()
    vm_obj = find_vm(content, name)

    if not vm_obj:
        return {"status": "error", "message": f"VM '{name}' not found"}

    if not vm_obj.snapshot:
        return {"status": "success", "message": f"VM '{name}' has no snapshots"}

    return wait_task(vm_obj.RemoveAllSnapshots_Task())


@app.post("/vm/vmotion")
def vmotion_vm(name: str, target_host: str):
    si = get_si()
    content = si.RetrieveContent()

    vm_obj = find_vm(content, name)
    host_obj = find_host(content, target_host)

    if not vm_obj:
        return {"status": "error", "message": f"VM '{name}' not found"}

    if not host_obj:
        return {"status": "error", "message": f"Host '{target_host}' not found"}

    task = vm_obj.MigrateVM_Task(
        pool=None,
        host=host_obj,
        priority=vim.VirtualMachine.MovePriority.defaultPriority
    )

    return wait_task(task)


@app.post("/vm/storage_vmotion")
def storage_vmotion_vm(name: str, target_datastore: str):
    si = get_si()
    content = si.RetrieveContent()

    vm_obj = find_vm(content, name)
    ds_obj = find_datastore(content, target_datastore)

    if not vm_obj:
        return {"status": "error", "message": f"VM '{name}' not found"}

    if not ds_obj:
        return {"status": "error", "message": f"Datastore '{target_datastore}' not found"}

    spec = vim.vm.RelocateSpec()
    spec.datastore = ds_obj

    task = vm_obj.RelocateVM_Task(spec=spec)
    return wait_task(task)


@app.post("/host/maintenance/enter")
def host_enter_maintenance(name: str):
    si = get_si()
    content = si.RetrieveContent()
    host_obj = find_host(content, name)

    if not host_obj:
        return {"status": "error", "message": f"Host '{name}' not found"}

    task = host_obj.EnterMaintenanceMode_Task(timeout=0, evacuatePoweredOffVms=True)
    return wait_task(task)


@app.post("/host/maintenance/exit")
def host_exit_maintenance(name: str):
    si = get_si()
    content = si.RetrieveContent()
    host_obj = find_host(content, name)

    if not host_obj:
        return {"status": "error", "message": f"Host '{name}' not found"}

    task = host_obj.ExitMaintenanceMode_Task(timeout=0)
    return wait_task(task)


@app.post("/host/reboot")
def host_reboot(name: str, force: bool = False):
    si = get_si()
    content = si.RetrieveContent()
    host_obj = find_host(content, name)

    if not host_obj:
        return {"status": "error", "message": f"Host '{name}' not found"}

    task = host_obj.RebootHost_Task(force=force)
    return wait_task(task)


@app.post("/host/shutdown")
def host_shutdown(name: str, force: bool = False):
    si = get_si()
    content = si.RetrieveContent()
    host_obj = find_host(content, name)

    if not host_obj:
        return {"status": "error", "message": f"Host '{name}' not found"}

    task = host_obj.ShutdownHost_Task(force=force)
    return wait_task(task)


@app.post("/host/disconnect")
def host_disconnect(name: str):
    si = get_si()
    content = si.RetrieveContent()
    host_obj = find_host(content, name)

    if not host_obj:
        return {"status": "error", "message": f"Host '{name}' not found"}

    task = host_obj.DisconnectHost_Task()
    return wait_task(task)


@app.post("/host/reconnect")
def host_reconnect(name: str):
    si = get_si()
    content = si.RetrieveContent()
    host_obj = find_host(content, name)

    if not host_obj:
        return {"status": "error", "message": f"Host '{name}' not found"}

    task = host_obj.ReconnectHost_Task()
    return wait_task(task)