#!/usr/bin/env python3
"""
NetSetBoy - a NetSetMan-style GUI for switching network profiles on Linux.
Wraps nmcli connection profiles. Requires NetworkManager + python3-tk.

    sudo apt install python3-tk
    python3 netsetboy.py
"""

import concurrent.futures
import ipaddress
import queue
import re
import socket
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox

# ---------- Dark theme palette ----------
BG = "#1e1e1e"
FG = "#e0e0e0"
FIELD_BG = "#2a2a2a"
BTN_BG = "#3a3a3a"
BTN_ACTIVE = "#505050"
ACCENT = "#4caf50"
INACTIVE = "#777777"


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def get_active_connection():
    r = run(["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"])
    return set(r.stdout.strip().split("\n")) if r.stdout.strip() else set()


def get_ethernet_interfaces():
    r = run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"])
    ifaces = []
    for line in r.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "ethernet":
            ifaces.append(parts[0])
    return ifaces


def get_profiles():
    """Return list of (name, type, ip4) for all saved ethernet/wifi connections."""
    r = run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    profiles = []
    for line in r.stdout.strip().split("\n"):
        if not line:
            continue
        name, ctype = line.split(":", 1)
        if ctype in ("802-3-ethernet", "ethernet", "802-11-wireless", "wifi"):
            ip_r = run(["nmcli", "-g", "ipv4.addresses", "connection", "show", name])
            ip = ip_r.stdout.strip() or "DHCP"
            profiles.append((name, ctype, ip))
    profiles.sort(key=lambda p: p[0].lower())
    return profiles


def get_runtime_ip(name):
    """Live IP currently bound to this connection (works for both static and DHCP)."""
    r = run(["nmcli", "-g", "IP4.ADDRESS", "connection", "show", name])
    lines = [l for l in r.stdout.strip().split("\n") if l]
    return lines[0] if lines else ""


def get_connection_details(name):
    fields = "connection.interface-name,ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns"
    r = run(["nmcli", "-g", fields, "connection", "show", name])
    lines = r.stdout.split("\n")
    while len(lines) < 5:
        lines.append("")
    return {
        "ifname": lines[0].strip(),
        "method": lines[1].strip() or "auto",
        "ip": lines[2].strip(),
        "gw": lines[3].strip(),
        "dns": lines[4].strip(),
    }


def get_active_ipv4_cidr():
    """Best-effort guess of the current subnet, for pre-filling the scan dialog."""
    r = run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", r.stdout)
    return m.group(1) if m else "192.168.1.0/24"


class ProfileDialog(tk.Toplevel):
    """Form for creating or editing a connection profile."""

    def __init__(self, parent, title, interfaces, existing=None):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.result = None
        self.is_edit = existing is not None

        pad = {"padx": 10, "pady": 6}

        row = 0
        ttk.Label(self, text="Profile name:").grid(row=row, column=0, sticky="e", **pad)
        self.name_var = tk.StringVar(value=(existing.get("name") if existing else ""))
        name_entry = ttk.Entry(self, textvariable=self.name_var, width=30)
        name_entry.grid(row=row, column=1, **pad)
        if self.is_edit:
            name_entry.configure(state="disabled")

        row += 1
        ttk.Label(self, text="Interface:").grid(row=row, column=0, sticky="e", **pad)
        self.iface_var = tk.StringVar(value=(existing.get("ifname") if existing else ""))
        iface_combo = ttk.Combobox(self, textvariable=self.iface_var, values=interfaces, width=27)
        iface_combo.grid(row=row, column=1, **pad)
        if interfaces and not self.iface_var.get():
            iface_combo.current(0)

        row += 1
        ttk.Label(self, text="IP/CIDR (blank = DHCP):").grid(row=row, column=0, sticky="e", **pad)
        ip_default = ""
        if existing and existing.get("method") == "manual":
            ip_default = existing.get("ip", "")
        self.ip_var = tk.StringVar(value=ip_default)
        ttk.Entry(self, textvariable=self.ip_var, width=30).grid(row=row, column=1, **pad)

        row += 1
        ttk.Label(self, text="Gateway:").grid(row=row, column=0, sticky="e", **pad)
        self.gw_var = tk.StringVar(value=(existing.get("gw") if existing else ""))
        ttk.Entry(self, textvariable=self.gw_var, width=30).grid(row=row, column=1, **pad)

        row += 1
        ttk.Label(self, text="DNS (comma-separated):").grid(row=row, column=0, sticky="e", **pad)
        dns_default = (existing.get("dns") if existing else "").replace(" ", ",")
        self.dns_var = tk.StringVar(value=dns_default)
        ttk.Entry(self, textvariable=self.dns_var, width=30).grid(row=row, column=1, **pad)

        row += 1
        btns = ttk.Frame(self)
        btns.grid(row=row, column=0, columnspan=2, pady=12)
        ttk.Button(btns, text="Save", command=self.on_save).pack(side="left", padx=5)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=5)

        self.grab_set()

    def on_save(self):
        name = self.name_var.get().strip()
        iface = self.iface_var.get().strip()
        if not name or not iface:
            messagebox.showwarning("Missing info", "Profile name and interface are required.")
            return
        self.result = {
            "name": name,
            "iface": iface,
            "ip": self.ip_var.get().strip(),
            "gw": self.gw_var.get().strip(),
            "dns": self.dns_var.get().strip(),
        }
        self.destroy()


class ScanDialog(tk.Toplevel):
    """Ping-sweeps a subnet, resolving MAC via ARP/neighbor table and hostname via reverse DNS."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Scan Network")
        self.configure(bg=BG)
        self.geometry("540x440")

        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=10)
        ttk.Label(top, text="Subnet (CIDR):").pack(side="left")
        self.cidr_var = tk.StringVar(value=get_active_ipv4_cidr())
        ttk.Entry(top, textvariable=self.cidr_var, width=20).pack(side="left", padx=8)
        self.scan_btn = ttk.Button(top, text="Start Scan", command=self.start_scan)
        self.scan_btn.pack(side="left", padx=4)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=10)

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=10, pady=(0, 8))

        columns = ("ip", "hostname", "mac")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=14)
        self.tree.heading("ip", text="IP Address")
        self.tree.heading("hostname", text="Hostname")
        self.tree.heading("mac", text="MAC Address")
        self.tree.column("ip", width=130)
        self.tree.column("hostname", width=220)
        self.tree.column("mac", width=150)
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.result_queue = queue.Queue()
        self.total = 0
        self.done = 0

    def start_scan(self):
        cidr = self.cidr_var.get().strip()
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            messagebox.showerror("Invalid subnet", "Enter a valid CIDR, e.g. 192.168.1.0/24")
            return

        hosts = list(network.hosts())
        if not hosts:
            messagebox.showinfo("Nothing to scan", "That subnet has no usable host addresses.")
            return
        if len(hosts) > 1024:
            if not messagebox.askyesno(
                "Large subnet",
                f"This subnet has {len(hosts)} addresses and may take a while. Continue?",
            ):
                return

        for row in self.tree.get_children():
            self.tree.delete(row)

        self.total = len(hosts)
        self.done = 0
        self.progress.configure(maximum=self.total, value=0)
        self.status_var.set(f"Scanning {self.total} addresses...")
        self.scan_btn.configure(state="disabled")

        threading.Thread(target=self._scan_worker, args=(hosts,), daemon=True).start()
        self.after(200, self._poll_queue)

    def _scan_worker(self, hosts):
        socket.setdefaulttimeout(1.0)
        with concurrent.futures.ThreadPoolExecutor(max_workers=40) as ex:
            futures = [ex.submit(self._probe, str(ip)) for ip in hosts]
            for fut in concurrent.futures.as_completed(futures):
                self.result_queue.put(fut.result())

    @staticmethod
    def _probe(ip):
        alive = run(["ping", "-c", "1", "-W", "1", ip]).returncode == 0
        if not alive:
            return None
        mac = ""
        r = run(["ip", "neigh", "show", ip])
        m = re.search(r"lladdr ([0-9A-Fa-f:]{17})", r.stdout)
        if m:
            mac = m.group(1)
        hostname = ""
        try:
            hostname = socket.gethostbyaddr(ip)[0]
        except (socket.herror, socket.gaierror, OSError):
            hostname = ""
        return {"ip": ip, "hostname": hostname, "mac": mac}

    def _poll_queue(self):
        try:
            while True:
                result = self.result_queue.get_nowait()
                self.done += 1
                self.progress.configure(value=self.done)
                if result:
                    self.tree.insert(
                        "", "end",
                        values=(result["ip"], result["hostname"] or "-", result["mac"] or "-"),
                    )
        except queue.Empty:
            pass

        if self.done >= self.total:
            self.status_var.set(f"Scan complete. {len(self.tree.get_children())} device(s) found.")
            self.scan_btn.configure(state="normal")
        else:
            self.status_var.set(f"Scanning... {self.done}/{self.total}")
            self.after(200, self._poll_queue)


class NetSwitch(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("netsetboy")
        self.geometry("640x600")
        self.minsize(520, 400)
        self.resizable(True, True)
        self.configure(bg=BG)

        self._setup_style()

        ttk.Label(self, text="Network Profiles", font=("Sans", 14, "bold")).pack(pady=10)

        list_container = ttk.Frame(self)
        list_container.pack(fill="both", expand=True, padx=10)

        self.canvas = tk.Canvas(list_container, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.frame = ttk.Frame(self.canvas)
        self.frame_id = self.canvas.create_window((0, 0), window=self.frame, anchor="nw")

        self.frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfig(self.frame_id, width=e.width),
        )
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))

        btns = ttk.Frame(self)
        btns.pack(pady=10)
        ttk.Button(btns, text="Refresh", command=self.refresh).pack(side="left", padx=4)
        ttk.Button(btns, text="New Profile", command=self.new_profile).pack(side="left", padx=4)
        ttk.Button(btns, text="Edit Selected", command=self.edit_profile).pack(side="left", padx=4)
        ttk.Button(btns, text="Delete Selected", command=self.delete_profile).pack(side="left", padx=4)
        ttk.Button(btns, text="Scan Network", command=self.scan_network).pack(side="left", padx=4)

        self.selected = tk.StringVar()
        self.refresh()

    def _setup_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG, fieldbackground=FIELD_BG)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("TButton", background=BTN_BG, foreground=FG, borderwidth=0, focusthickness=0)
        style.map("TButton", background=[("active", BTN_ACTIVE)])
        style.configure("TRadiobutton", background=BG, foreground=FG)
        style.map("TRadiobutton", background=[("active", BG)])
        style.configure("TCombobox", fieldbackground=FIELD_BG, background=FIELD_BG, foreground=FG)
        style.configure("TEntry", fieldbackground=FIELD_BG, foreground=FG)
        style.configure("TProgressbar", background=ACCENT, troughcolor=FIELD_BG)
        style.configure("Treeview", background=FIELD_BG, foreground=FG,
                         fieldbackground=FIELD_BG, bordercolor=BG)
        style.map("Treeview", background=[("selected", BTN_ACTIVE)])
        style.configure("Treeview.Heading", background=BTN_BG, foreground=FG)

    def refresh(self):
        for w in self.frame.winfo_children():
            w.destroy()

        active = get_active_connection()
        profiles = get_profiles()

        if not profiles:
            ttk.Label(self.frame, text="No ethernet/wifi profiles found.\nCreate one with 'New Profile'.").pack(pady=20)
            return

        for name, ctype, ip in profiles:
            row = ttk.Frame(self.frame)
            row.pack(fill="x", pady=3)

            is_active = name in active
            mark = "●" if is_active else "○"
            color = ACCENT if is_active else INACTIVE

            display_ip = ip
            if is_active:
                runtime = get_runtime_ip(name)
                if runtime:
                    display_ip = runtime

            tk.Label(row, text=mark, fg=color, bg=BG, font=("Sans", 12)).pack(side="left", padx=(0, 5))
            ttk.Radiobutton(row, variable=self.selected, value=name).pack(side="left")

            label = f"{name}  [{ctype}]  {display_ip}"
            ttk.Label(row, text=label, width=40, anchor="w").pack(side="left", padx=5)

            ttk.Button(row, text="Activate", command=lambda n=name: self.activate(n)).pack(side="right")

    def activate(self, name):
        r = run(["nmcli", "connection", "up", name])
        if r.returncode != 0:
            messagebox.showerror("Failed", r.stderr.strip() or "Could not activate profile.")
        self.refresh()

    def delete_profile(self):
        name = self.selected.get()
        if not name:
            messagebox.showinfo("Select a profile", "Click the radio button next to a profile first.")
            return
        if messagebox.askyesno("Confirm", f"Delete profile '{name}'?"):
            run(["nmcli", "connection", "delete", name])
            self.refresh()

    def new_profile(self):
        interfaces = get_ethernet_interfaces()
        dialog = ProfileDialog(self, "New Profile", interfaces)
        self.wait_window(dialog)
        if not dialog.result:
            return
        self._apply_profile(dialog.result, is_new=True)

    def edit_profile(self):
        name = self.selected.get()
        if not name:
            messagebox.showinfo("Select a profile", "Click the radio button next to a profile first.")
            return
        details = get_connection_details(name)
        details["name"] = name
        interfaces = get_ethernet_interfaces()
        dialog = ProfileDialog(self, f"Edit '{name}'", interfaces, existing=details)
        self.wait_window(dialog)
        if not dialog.result:
            return
        self._apply_profile(dialog.result, is_new=False)

    def scan_network(self):
        ScanDialog(self)

    def _apply_profile(self, data, is_new):
        name, iface, ip, gw, dns = data["name"], data["iface"], data["ip"], data["gw"], data["dns"]

        if is_new:
            cmd = ["nmcli", "connection", "add", "type", "ethernet", "con-name", name, "ifname", iface]
            if ip:
                cmd += ["ip4", ip]
                if gw:
                    cmd += ["gw4", gw]
            else:
                cmd += ["ipv4.method", "auto"]
            r = run(cmd)
            if r.returncode != 0:
                messagebox.showerror("Failed", r.stderr.strip() or "Could not create profile.")
                return
            if dns:
                run(["nmcli", "connection", "modify", name, "ipv4.dns", dns])
        else:
            cmd = ["nmcli", "connection", "modify", name, "connection.interface-name", iface]
            if ip:
                cmd += ["ipv4.method", "manual", "ipv4.addresses", ip,
                        "ipv4.gateway", gw if gw else "",
                        "ipv4.dns", dns if dns else ""]
            else:
                cmd += ["ipv4.method", "auto", "ipv4.addresses", "",
                        "ipv4.gateway", "", "ipv4.dns", ""]
            r = run(cmd)
            if r.returncode != 0:
                messagebox.showerror("Failed", r.stderr.strip() or "Could not update profile.")
                return
            if name in get_active_connection():
                run(["nmcli", "connection", "up", name])

        self.refresh()


if __name__ == "__main__":
    if run(["which", "nmcli"]).returncode != 0:
        print("nmcli not found. Install NetworkManager first: sudo apt install network-manager")
        exit(1)
    NetSwitch().mainloop()
