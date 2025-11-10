#!/usr/bin/env python3
import proxmoxer
import requests
import curses
import argparse
import sys
import os
import json
import subprocess
from configparser import ConfigParser
from io import StringIO
from time import sleep
import random


class VDIClient:
    def __init__(self):
        self.proxmox = None
        self.spiceproxy_conv = {}
        self.addl_params = None
        self.vvcmd = None
        self.title = 'VDI Login'
        self.kiosk = False
        self.fullscreen = True
        self.guest_type = 'both'
        self.current_hostset = 'DEFAULT'
        self.hosts = {}
        
    def load_config(self, config_path=None):
        """Load configuration from file matching the original format"""
        config = ConfigParser(delimiters='=')
        
        if not config_path:
            # Default config locations
            if os.name == 'nt':  # Windows
                config_locations = [
                    f'{os.getenv("APPDATA")}\\VDIClient\\vdiclient.ini',
                    f'{os.getenv("PROGRAMFILES")}\\VDIClient\\vdiclient.ini',
                    f'{os.getenv("PROGRAMFILES(x86)")}\\VDIClient\\vdiclient.ini',
                    'C:\\Program Files\\VDIClient\\vdiclient.ini'
                ]
            else:  # Linux/Unix
                config_locations = [
                    os.path.expanduser('~/.config/vdiclient/vdiclient.ini'),
                    '/etc/vdiclient/vdiclient.ini',
                    '/usr/local/etc/vdiclient/vdiclient.ini',
                    './vdiclient.ini'
                ]
            
            for location in config_locations:
                print(f"Checking config {location}")
                if os.path.exists(location):
                    config_path = location
                    break
        
        if not config_path or not os.path.exists(config_path):
            raise FileNotFoundError("Configuration file not found")
        
        config.read(config_path)
        
        if 'General' not in config:
            raise ValueError('No `General` section found in configuration!')
        
        # Load general settings
        if 'title' in config['General']:
            self.title = config['General']['title']
        if 'kiosk' in config['General']:
            self.kiosk = config['General'].getboolean('kiosk')
        if 'fullscreen' in config['General']:
            self.fullscreen = config['General'].getboolean('fullscreen')
        if 'guest_type' in config['General']:
            self.guest_type = config['General']['guest_type']
        
        # Check for legacy Authentication section
        if 'Authentication' in config:
            self.hosts['DEFAULT'] = {
                'hostpool': [],
                'backend': 'pve',
                'user': "",
                'token_name': None,
                'token_value': None,
                'totp': False,
                'verify_ssl': True,
                'pwresetcmd': None,
                'auto_vmid': None,
                'knock_seq': []
            }
            
            if 'Hosts' not in config:
                raise ValueError('No `Hosts` section found in legacy configuration!')
            
            for key in config['Hosts']:
                self.hosts['DEFAULT']['hostpool'].append({
                    'host': key,
                    'port': int(config['Hosts'][key])
                })
            
            # Load authentication settings
            if 'auth_backend' in config['Authentication']:
                self.hosts['DEFAULT']['backend'] = config['Authentication']['auth_backend']
            if 'user' in config['Authentication']:
                self.hosts['DEFAULT']['user'] = config['Authentication']['user']
            if 'token_name' in config['Authentication']:
                self.hosts['DEFAULT']['token_name'] = config['Authentication']['token_name']
            if 'token_value' in config['Authentication']:
                self.hosts['DEFAULT']['token_value'] = config['Authentication']['token_value']
            if 'auth_totp' in config['Authentication']:
                self.hosts['DEFAULT']['totp'] = config['Authentication'].getboolean('auth_totp')
            if 'tls_verify' in config['Authentication']:
                self.hosts['DEFAULT']['verify_ssl'] = config['Authentication'].getboolean('tls_verify')
        else:
            # New style config with Hosts.* sections
            i = 0
            for section in config.sections():
                if section.startswith('Hosts.'):
                    _, group = section.split('.', 1)
                    if i == 0:
                        self.current_hostset = group
                    
                    self.hosts[group] = {
                        'hostpool': [],
                        'backend': 'pve',
                        'user': "",
                        'token_name': None,
                        'token_value': None,
                        'totp': False,
                        'verify_ssl': True,
                        'pwresetcmd': None,
                        'auto_vmid': None,
                        'knock_seq': []
                    }
                    
                    try:
                        hostjson = json.loads(config[section]['hostpool'])
                    except Exception as e:
                        raise ValueError(f"Error parsing hostpool in section {section}: {e}")
                    
                    for key, value in hostjson.items():
                        self.hosts[group]['hostpool'].append({
                            'host': key,
                            'port': int(value)
                        })
                    
                    # Load host-specific settings
                    if 'auth_backend' in config[section]:
                        self.hosts[group]['backend'] = config[section]['auth_backend']
                    if 'user' in config[section]:
                        self.hosts[group]['user'] = config[section]['user']
                    if 'token_name' in config[section]:
                        self.hosts[group]['token_name'] = config[section]['token_name']
                    if 'token_value' in config[section]:
                        self.hosts[group]['token_value'] = config[section]['token_value']
                    if 'auth_totp' in config[section]:
                        self.hosts[group]['totp'] = config[section].getboolean('auth_totp')
                    if 'tls_verify' in config[section]:
                        self.hosts[group]['verify_ssl'] = config[section].getboolean('tls_verify')
                    if 'pwresetcmd' in config[section]:
                        self.hosts[group]['pwresetcmd'] = config[section]['pwresetcmd']
                    if 'auto_vmid' in config[section]:
                        self.hosts[group]['auto_vmid'] = config[section].getint('auto_vmid')
                    if 'knock_seq' in config[section]:
                        try:
                            self.hosts[group]['knock_seq'] = json.loads(config[section]['knock_seq'])
                        except Exception:
                            pass  # Skip invalid JSON
                    
                    i += 1
        
        # Load SPICE proxy redirects
        if 'SpiceProxyRedirect' in config:
            for key in config['SpiceProxyRedirect']:
                self.spiceproxy_conv[key] = config['SpiceProxyRedirect'][key]
        
        # Load additional parameters
        if 'AdditionalParameters' in config:
            self.addl_params = {}
            for key in config['AdditionalParameters']:
                self.addl_params[key] = config['AdditionalParameters'][key]
        
        if not self.hosts:
            raise ValueError("No host configurations found!")
    
    def find_viewer_command(self):
        """Find the virt-viewer command"""
        try:
            if os.name == 'nt':  # Windows
                import csv
                cmd = 'ftype VirtViewer.vvfile'
                result = subprocess.check_output(cmd, shell=True)
                cmdresult = result.decode('utf-8')
                cmdparts = cmdresult.split('=')
                for row in csv.reader([cmdparts[1]], delimiter=' ', quotechar='"'):
                    self.vvcmd = row[0]
                    break
            else:  # Linux/Unix
                subprocess.check_output(['which', 'remote-viewer'])
                self.vvcmd = 'remote-viewer'
        except subprocess.CalledProcessError:
            error_msg = ('virt-viewer not found. Please install:\n'
                        'Windows: https://virt-manager.org/download/\n'
                        'Linux: apt install virt-viewer')
            raise RuntimeError(error_msg)
    
    def authenticate(self):
        """Authenticate with Proxmox using API token"""
        # Shuffle hostpool for load balancing
        random.shuffle(self.hosts[self.current_hostset]['hostpool'])
        
        for hostinfo in self.hosts[self.current_hostset]['hostpool']:
            host = hostinfo['host']
            port = hostinfo.get('port', 8006)
            
            try:
                self.proxmox = proxmoxer.ProxmoxAPI(
                    host,
                    user=f"{self.hosts[self.current_hostset]['user']}@{self.hosts[self.current_hostset]['backend']}",
                    token_name=self.hosts[self.current_hostset]['token_name'],
                    token_value=self.hosts[self.current_hostset]['token_value'],
                    verify_ssl=self.hosts[self.current_hostset]['verify_ssl'],
                    port=port
                )
                # Test connection
                self.proxmox.cluster.resources.get(type='node')
                return True
            except proxmoxer.backends.https.AuthenticationError as e:
                raise ConnectionError(f"Authentication failed: {e}")
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout, 
                    requests.exceptions.ConnectionError) as e:
                continue  # Try next host
        
        raise ConnectionError("Unable to connect to any host in the pool")
    
    def get_vms(self):
        """Get list of available VMs"""
        try:
            # Get online nodes
            online_nodes = []
            for node in self.proxmox.cluster.resources.get(type='node'):
                if node['status'] == 'online':
                    online_nodes.append(node['node'])
            
            # Get VMs
            vms = []
            for vm in self.proxmox.cluster.resources.get(type='vm'):
                if vm['node'] not in online_nodes:
                    continue
                if 'template' in vm and vm['template']:
                    continue
                if self.guest_type == 'both' or self.guest_type == vm['type']:
                    vms.append({
                        'vmid': vm['vmid'],
                        'name': vm['name'],
                        'node': vm['node'],
                        'status': vm['status'],
                        'type': vm['type'],
                        'lock': vm.get('lock', None)
                    })
            
            return sorted(vms, key=lambda x: x['name'])
        except Exception as e:
            raise RuntimeError(f"Failed to get VMs: {e}")
    
    def connect_to_vm(self, vm):
        """Connect to a VM using SPICE"""
        try:
            # Start VM if not running
            if vm['status'] != 'running':
                print(f"Starting {vm['name']}...")
                if vm['type'] == 'qemu':
                    jobid = self.proxmox.nodes(vm['node']).qemu(str(vm['vmid'])).status.start.post(timeout=28)
                else:
                    jobid = self.proxmox.nodes(vm['node']).lxc(str(vm['vmid'])).status.start.post(timeout=28)
                
                # Wait for VM to start
                for i in range(30):
                    sleep(1)
                    try:
                        jobstatus = self.proxmox.nodes(vm['node']).tasks(jobid).status.get()
                    except Exception:
                        continue
                    
                    if 'exitstatus' in jobstatus:
                        if jobstatus['exitstatus'] != 'OK':
                            raise RuntimeError("Failed to start VM")
                        break
                else:
                    raise RuntimeError("VM failed to start within 30 seconds")
            
            # Get SPICE configuration
            if vm['type'] == 'qemu':
                spiceconfig = self.proxmox.nodes(vm['node']).qemu(str(vm['vmid'])).spiceproxy.post()
            else:
                spiceconfig = self.proxmox.nodes(vm['node']).lxc(str(vm['vmid'])).spiceproxy.post()
            
            # Create virt-viewer config
            config_parser = ConfigParser()
            config_parser['virt-viewer'] = {}
            
            for key, value in spiceconfig.items():
                if key == 'proxy':
                    val = value[7:].lower()
                    if val in self.spiceproxy_conv:
                        config_parser['virt-viewer'][key] = f'http://{self.spiceproxy_conv[val]}'
                    else:
                        config_parser['virt-viewer'][key] = value
                else:
                    config_parser['virt-viewer'][key] = str(value)
            
            # Add additional parameters
            if self.addl_params:
                for key, value in self.addl_params.items():
                    config_parser['virt-viewer'][key] = str(value)
            
            # Generate config string
            config_file = StringIO('')
            config_parser.write(config_file)
            config_file.seek(0)
            config_string = config_file.read()
            
            # Launch virt-viewer
            print(f"Connecting to {vm['name']}...")
            cmd = [self.vvcmd]
            
            if self.kiosk:
                cmd.extend(['--kiosk', '--kiosk-quit', 'on-disconnect'])
            elif self.fullscreen:
                cmd.append('--full-screen')
            
            cmd.append('-')  # Read from stdin
            
            print(f"CMD VM: {config_string}")
            process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            process.communicate(input=config_string.encode('utf-8'))
            
            return True
            
        except Exception as e:
            raise RuntimeError(f"Failed to connect to VM: {e}")


def draw_menu(stdscr, client, vms, selected_idx):
    """Draw the VM selection menu"""
    stdscr.clear()
    h, w = stdscr.getmaxyx()
    
    # Title with orange background and white text
    title = client.title
    title_padding = " " * ((w - len(title)) // 2)
    header_line = title_padding + title + title_padding
    if len(header_line) < w:
        header_line += " " * (w - len(header_line))
    stdscr.addstr(0, 0, header_line, curses.color_pair(5))
    
    # Separator line with same color
    separator = "=" * w
    stdscr.addstr(1, 0, separator, curses.color_pair(5))
    
    # Instructions with orange background and white text
    instructions = "Use ↑/↓ arrows to navigate, Enter to connect, 'q' to quit, 'r' to refresh"
    instr_padding = " " * ((w - len(instructions)) // 2)
    instr_line = instr_padding + instructions + instr_padding
    if len(instr_line) < w:
        instr_line += " " * (w - len(instr_line))
    stdscr.addstr(2, 0, instr_line, curses.color_pair(5))
    
    # Bottom separator line
    stdscr.addstr(3, 0, separator, curses.color_pair(5))
    
    # VM list
    start_y = 5
    max_visible = h - start_y - 2
    
    # Calculate scrolling
    if len(vms) > max_visible:
        if selected_idx < max_visible // 2:
            start_idx = 0
        elif selected_idx > len(vms) - max_visible // 2:
            start_idx = len(vms) - max_visible
        else:
            start_idx = selected_idx - max_visible // 2
        end_idx = min(start_idx + max_visible, len(vms))
    else:
        start_idx = 0
        end_idx = len(vms)
    
    # Draw VMs
    for i, vm in enumerate(vms[start_idx:end_idx], start_idx):
        y = start_y + (i - start_idx)
        
        # Determine status and color
        if vm['status'] == 'running':
            if vm['lock'] in ('suspending', 'suspended'):
                status_text = "⏸"
                status_color = curses.color_pair(3)  # Yellow/orange
                state = vm['lock']
            else:
                status_text = "●"
                status_color = curses.color_pair(2)  # Green
                state = 'running'
        else:
            status_text = "○"
            status_color = curses.color_pair(4)  # Red
            state = vm['status']
        
        # VM info
        vm_text = f"{status_text} {vm['name']} (ID: {vm['vmid']}) [{state}]"
        
        if i == selected_idx:
            stdscr.addstr(y, 2, vm_text, curses.A_REVERSE | curses.A_BOLD)
        else:
            stdscr.addstr(y, 2, status_text, status_color)
            stdscr.addstr(y, 4, vm_text[2:])
    
    # Scroll indicators
    if start_idx > 0:
        stdscr.addstr(start_y - 1, w - 10, "↑ More ↑")
    if end_idx < len(vms):
        stdscr.addstr(start_y + max_visible, w - 10, "↓ More ↓")
    
    stdscr.refresh()


def main_menu(stdscr, client):
    """Main menu loop"""
    curses.curs_set(0)  # Hide cursor
    
    # Initialize colors
    curses.start_color()
    curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_GREEN, curses.COLOR_BLACK)   # Running
    curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)  # Suspended
    curses.init_pair(4, curses.COLOR_RED, curses.COLOR_BLACK)     # Stopped
    curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_YELLOW) # Header (closest to orange)
    
    selected_idx = 0
    
    while True:
        try:
            vms = client.get_vms()
            if not vms:
                stdscr.clear()
                stdscr.addstr(0, 0, "No VMs available. Press any key to exit.")
                stdscr.refresh()
                stdscr.getch()
                break
            
            # Ensure selected index is valid
            selected_idx = min(selected_idx, len(vms) - 1)
            
            draw_menu(stdscr, client, vms, selected_idx)
            
            key = stdscr.getch()
            
            if key == ord('q') or key == ord('Q'):
                break
            elif key == ord('r') or key == ord('R'):
                continue  # Refresh
            elif key == curses.KEY_UP and selected_idx > 0:
                selected_idx -= 1
            elif key == curses.KEY_DOWN and selected_idx < len(vms) - 1:
                selected_idx += 1
            elif key == ord('\n') or key == ord(' '):
                # Connect to selected VM
                selected_vm = vms[selected_idx]
                stdscr.clear()
                stdscr.addstr(0, 0, f"Connecting to {selected_vm['name']}... Press any key after connection closes.")
                stdscr.refresh()
                
                try:
                    # Restore terminal for subprocess
                    curses.endwin()
                    client.connect_to_vm(selected_vm)
                except Exception as e:
                    print(f"Error connecting to VM: {e}")
                finally:
                    # Reinitialize curses
                    stdscr = curses.initscr()
                    curses.noecho()
                    curses.cbreak()
                    stdscr.keypad(True)
                    curses.start_color()
                    curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)
                    curses.init_pair(2, curses.COLOR_GREEN, curses.COLOR_BLACK)
                    curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)
                    curses.init_pair(4, curses.COLOR_RED, curses.COLOR_BLACK)
                    curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_YELLOW)  # Header
                    curses.curs_set(0)
                
                stdscr.clear()
                stdscr.addstr(0, 0, "Connection closed. Press any key to continue...")
                stdscr.refresh()
                stdscr.getch()
        
        except Exception as e:
            stdscr.clear()
            stdscr.addstr(0, 0, f"Error: {e}")
            stdscr.addstr(1, 0, "Press any key to continue...")
            stdscr.refresh()
            stdscr.getch()


def main():
    parser = argparse.ArgumentParser(description='Proxmox VDI Client (NCurses)')
    parser.add_argument('--config', help='Configuration file path', default=None)
    args = parser.parse_args()
    
    client = VDIClient()
    
    try:
        # Load configuration
        client.load_config(args.config)
        
        # Find virt-viewer
        client.find_viewer_command()
        
        # Authenticate
        print("Authenticating...")
        client.authenticate()
        print("Authentication successful!")
        
        # Check for auto_vmid
        if client.hosts[client.current_hostset].get('auto_vmid'):
            vms = client.get_vms()
            for vm in vms:
                if vm['vmid'] == client.hosts[client.current_hostset]['auto_vmid']:
                    print(f"Auto-connecting to VM {vm['name']} (ID: {vm['vmid']})")
                    client.connect_to_vm(vm)
                    return 0
            print(f"Auto VM ID {client.hosts[client.current_hostset]['auto_vmid']} not found!")
        
        # Start curses interface
        curses.wrapper(main_menu, client)
        
    except Exception as e:
        print(f"Error: {e}")
        return 1
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
