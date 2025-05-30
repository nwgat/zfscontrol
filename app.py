from flask import Flask, render_template, request, jsonify
import subprocess
import re
import os
import json
# import sys # Uncomment for debugging print statements if needed

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
# Templates are in the APP_ROOT.
# Static files (like style.css) will now also be served from APP_ROOT.
app = Flask(__name__, template_folder=APP_ROOT, static_folder=APP_ROOT)


# --- Define command paths ---
LSBLK_PATH = '/usr/bin/lsblk'
ZPOOL_PATH = '/usr/sbin/zpool'
# ---

def run_zfs_command(command_args, timeout_seconds=30):
    """
    Helper function to run ZFS commands and return output.
    Expects command_args as a list of arguments.
    """
    try:
        process = subprocess.Popen(command_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False)
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        if process.returncode != 0:
            error_message = stderr.strip() if stderr.strip() else stdout.strip()
            return {"status": "error", "message": error_message or f"Command failed with exit code {process.returncode}"}
        return {"status": "success", "message": stdout.strip()}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": f"Command timed out after {timeout_seconds} seconds."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def get_zfs_member_devices(timeout_seconds=60):
    """
    Returns a set of canonical paths for devices currently used in any ZFS pool.
    """
    devices = set()
    status_result = run_zfs_command(['sudo', ZPOOL_PATH, 'status', '-P'], timeout_seconds=timeout_seconds)

    if status_result['status'] == 'success':
        in_config_section = False
        current_pool_name = None
        header_skipped = False
        for line in status_result['message'].splitlines():
            stripped_line = line.strip()
            if stripped_line.startswith("pool:"):
                current_pool_name = stripped_line.split(":", 1)[1].strip()
                in_config_section = False
                header_skipped = False
            elif stripped_line == "config:" and current_pool_name:
                in_config_section = True
                header_skipped = False
            elif in_config_section:
                if not header_skipped:
                    if stripped_line.startswith("NAME"):
                        header_skipped = True
                    continue

                if not stripped_line:
                    continue
                if stripped_line.startswith("errors:"):
                    in_config_section = False
                    continue

                parts = stripped_line.split()
                if len(parts) > 0:
                    device_path_candidate = parts[0]
                    if device_path_candidate.startswith('/dev/') and device_path_candidate != current_pool_name:
                        try:
                            real_path = os.path.realpath(device_path_candidate)
                            devices.add(real_path)
                        except OSError:
                            pass
    return devices

def get_single_device_details_if_disk(device_realpath, timeout_sec=10):
    """
    Gets details for a specific device path if it's a whole disk using lsblk.
    Returns a dict with fstype, size (bytes), model, vendor, or None.
    """
    try:
        cmd = ['sudo', LSBLK_PATH, '--json', '-p', '-b', '-d',
               '-o', 'NAME,TYPE,FSTYPE,SIZE,MODEL,VENDOR', device_realpath]
        result = run_zfs_command(cmd, timeout_seconds=timeout_sec)
        if result["status"] == "success" and result["message"]:
            data = json.loads(result["message"])
            if data.get("blockdevices") and len(data["blockdevices"]) == 1:
                dev_info = data["blockdevices"][0]
                if dev_info.get("name") == device_realpath and dev_info.get("type") == "disk":
                    return {
                        "fstype": dev_info.get("fstype"),
                        "size": dev_info.get("size"),
                        "model": (dev_info.get("model") or "").strip(),
                        "vendor": (dev_info.get("vendor") or "").strip()
                    }
    except json.JSONDecodeError:
        pass
    except Exception:
        pass
    return None

@app.route('/available_disks')
def available_disks_route():
    discovery_timeout = 60
    zfs_members = get_zfs_member_devices(timeout_seconds=discovery_timeout)
    available_disks_list = []
    processed_real_paths = set()

    try:
        proc = subprocess.Popen(['ls', '-1', '/dev/disk/by-id/'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = proc.communicate(timeout=10)

        if proc.returncode != 0:
            return jsonify({"status": "error", "message": f"Failed to list /dev/disk/by-id/: {stderr.strip()}"})

        partition_regex = re.compile(r'-part\d+$')

        for id_name in stdout.splitlines():
            id_name = id_name.strip()
            if not id_name or partition_regex.search(id_name):
                continue

            full_id_path = os.path.join('/dev/disk/by-id/', id_name)

            try:
                real_path = os.path.realpath(full_id_path)
                if not real_path.startswith('/dev/') or real_path in processed_real_paths:
                    continue
            except OSError:
                continue

            if real_path not in zfs_members:
                dev_details = get_single_device_details_if_disk(real_path)

                if dev_details and (dev_details["fstype"] is None or dev_details["fstype"] == ""):
                    size_bytes_str = dev_details.get("size")
                    size_readable = "N/A"
                    if size_bytes_str:
                        try:
                            size_bytes_int = int(size_bytes_str)
                            gb_threshold = 1024**3; mb_threshold = 1024**2; kb_threshold = 1024
                            if size_bytes_int >= gb_threshold: size_readable = f"{size_bytes_int / gb_threshold:.1f} GB"
                            elif size_bytes_int >= mb_threshold: size_readable = f"{size_bytes_int / mb_threshold:.0f} MB"
                            elif size_bytes_int >= kb_threshold: size_readable = f"{size_bytes_int / kb_threshold:.0f} KB"
                            else: size_readable = f"{size_bytes_int} B"
                        except ValueError: size_readable = str(size_bytes_str)

                    available_disks_list.append({
                        "name": full_id_path,
                        "size": size_readable,
                        "model": dev_details.get("model", "N/A"),
                        "vendor": dev_details.get("vendor", "N/A")
                    })
                    processed_real_paths.add(real_path)

    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "Timeout while listing /dev/disk/by-id/"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"An error occurred while processing available disks: {str(e)}"})

    return jsonify({"status": "success", "disks": available_disks_list})


def parse_zpool_status(status_output):
    pools = []; current_pool_info = None; current_pool_raw_lines = []; parsing_config_for_current_pool = False
    if not status_output or "no pools available" in status_output.lower(): return []
    lines = status_output.splitlines()
    for line_content in lines:
        stripped_line = line_content.strip()
        if stripped_line.startswith("pool:"):
            if current_pool_info: current_pool_info["status_details"] = current_pool_raw_lines; pools.append(current_pool_info)
            pool_name = stripped_line.split(":", 1)[1].strip()
            current_pool_info = {"name": pool_name, "disks": [], "status_details": [] }; current_pool_raw_lines = [line_content]; parsing_config_for_current_pool = False
        elif current_pool_info:
            current_pool_raw_lines.append(line_content)
            if stripped_line == "config:":
                parsing_config_for_current_pool = True;
                continue

            if parsing_config_for_current_pool and stripped_line:
                if stripped_line.startswith("NAME ") and "STATE " in stripped_line and "READ " in stripped_line:
                    continue
                if stripped_line.startswith("errors:"):
                    continue

                if line_content.startswith((" ", "\t")):
                    parts = stripped_line.split() 
                    if not parts: continue
                    component_name = parts[0] 
                    if component_name == current_pool_info["name"]:
                        continue
                    if len(parts) > 1 and parts[1].isupper():
                        if component_name not in current_pool_info["disks"]:
                             current_pool_info["disks"].append(component_name) 
                    elif component_name.lower() in ["logs", "cache", "spares", "dedup", "special"] and len(parts) == 1:
                        pass 
    if current_pool_info:
        current_pool_info["status_details"] = current_pool_raw_lines;
        pools.append(current_pool_info)
    return pools

@app.route('/')
def index_route(): return render_template('index.html')

@app.route('/status')
def get_status():
    result = run_zfs_command(['sudo', ZPOOL_PATH, 'status', '-v']);
    if result["status"] == "success": return jsonify({"status": "success", "pools": parse_zpool_status(result["message"]), "raw_output": result["message"]})
    return jsonify(result)

@app.route('/list_all_disk_ids')
def list_all_disk_ids_route():
    all_entries_list = []
    try:
        proc = subprocess.Popen(['ls', '-1', '/dev/disk/by-id/'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = proc.communicate(timeout=10) 

        if proc.returncode != 0:
            return jsonify({"status": "error", "message": f"Failed to list /dev/disk/by-id/: {stderr.strip()}"})

        for id_name_raw in stdout.splitlines():
            id_name = id_name_raw.strip()
            if not id_name: 
                continue

            full_id_path = os.path.join('/dev/disk/by-id/', id_name)
            target_device = "N/A" 
            try:
                target_device = os.path.realpath(full_id_path)
            except OSError as e:
                target_device = f"Error resolving path: {str(e)}"
            except Exception as e: 
                target_device = f"Unexpected error resolving path: {str(e)}"

            all_entries_list.append({
                "id_name": id_name,          
                "full_path": full_id_path,   
                "target_device": target_device 
            })
        
        return jsonify({"status": "success", "entries": all_entries_list})

    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "Timeout while listing /dev/disk/by-id/"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"An error occurred while listing all disk IDs: {str(e)}"})

@app.route('/pool_history/<pool_name>')
def get_pool_history(pool_name):
    if not pool_name or not re.match(r'^[a-zA-Z0-9_.-]+$', pool_name):
        return jsonify({"status": "error", "message": "Invalid pool name provided."})
    result = run_zfs_command(['sudo', ZPOOL_PATH, 'history', pool_name], timeout_seconds=60)
    return jsonify(result)

@app.route('/create_pool', methods=['POST'])
def create_pool_route():
    data = request.json; pool_name = data.get('pool_name'); pool_type = data.get('pool_type', ''); disks_from_json = data.get('disks')
    if not pool_name or not disks_from_json: return jsonify({"status": "error", "message": "Pool name and at least one disk selection are required."})
    if not isinstance(disks_from_json, list) or not all(isinstance(d, str) for d in disks_from_json): return jsonify({"status": "error", "message": "Disks parameter must be a list of strings."})
    actual_disks = [d.strip() for d in disks_from_json if d.strip()]
    if not actual_disks: return jsonify({"status": "error", "message": "At least one valid disk must be selected."})
    command = ['sudo', ZPOOL_PATH, 'create']; command.append(pool_name)
    if pool_type and pool_type.lower() not in ['stripe', '']: command.append(pool_type)
    command.extend(actual_disks); result = run_zfs_command(command)
    return jsonify(result)

@app.route('/attach_disk', methods=['POST'])
def attach_disk_route():
    data = request.json
    pool_name = data.get('pool_name')
    new_disk = data.get('new_disk')
    existing_disk_or_vdev = data.get('existing_vdev', '').strip()

    if not pool_name or not new_disk:
        return jsonify({"status": "error", "message": "Pool name and new disk are required."})
    if not new_disk.startswith('/dev/'): 
        return jsonify({"status": "error", "message": "Invalid new disk path."})
    if existing_disk_or_vdev and not existing_disk_or_vdev.startswith('/dev/') and not re.match(r'^[a-zA-Z0-9_.:-]+$', existing_disk_or_vdev):
        return jsonify({"status": "error", "message": "Invalid existing disk/vdev path or name."})

    command = ['sudo', ZPOOL_PATH, 'attach', pool_name]
    if existing_disk_or_vdev:
        command.append(existing_disk_or_vdev)
    command.append(new_disk)
    result = run_zfs_command(command, timeout_seconds=120) 
    return jsonify(result)

@app.route('/replace_disk', methods=['POST'])
def replace_disk_route():
    data = request.json; pool_name = data.get('pool_name'); old_disk = data.get('old_disk'); new_disk = data.get('new_disk')
    if not pool_name or not old_disk or not new_disk: return jsonify({"status": "error", "message": "Pool name, old disk, and new disk are required."})
    command = ['sudo', ZPOOL_PATH, 'replace', pool_name, old_disk, new_disk]; result = run_zfs_command(command)
    return jsonify(result)

@app.route('/destroy_pool', methods=['POST'])
def destroy_pool_route():
    data = request.json; pool_name = data.get('pool_name')
    if not pool_name: return jsonify({"status": "error", "message": "Pool name is required."})
    command = ['sudo', ZPOOL_PATH, 'destroy', pool_name]; result = run_zfs_command(command)
    return jsonify(result)

@app.route('/scrub_pool', methods=['POST'])
def scrub_pool_route():
    data = request.json; pool_name = data.get('pool_name'); action = data.get('action', 'start') 
    if not pool_name: return jsonify({"status": "error", "message": "Pool name is required."})
    command_base = ['sudo', ZPOOL_PATH, 'scrub']
    if action == 'stop': command_base.append('-s') 
    command_base.append(pool_name); result = run_zfs_command(command_base)
    return jsonify(result)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)