# Evaluation build — hardware record

> **EXPERIMENTAL SOFTWARE — FOR EVALUATION ONLY.** This is the hardware the evaluation build was tested on. It is not a recommendation to buy or deploy anything. See [DISCLAIMER.md](../DISCLAIMER.md).

| Item | Value |
|---|---|
| Machine | Lenovo ThinkCentre M700 Tiny, product `10HY002AUS` |
| CPU | Intel Core i5-6500T @ 2.50 GHz (4 CPUs) |
| RAM | 15 GiB |
| Storage | Patriot P210 256 GB SSD (`sda`), one ext4 partition + swap, no encryption, no LVM |
| BIOS | `FWKTBFA`, dated 06/23/2022 |
| Display | HDMI, tested at 1360x768 and the GUI's 800 px layout; the calibration studio and wizard clamp to the screen height. |
| OS | Debian 13.7 "Trixie", Xfce 4.20 |
| Kernel | `6.12.113+deb13-amd64` |
| Python | 3.13.5 (venv at `/opt/ledger/venv`) |
| Install media | `debian-13.7.0-amd64-netinst.iso` (installer log: "Debian GNU/Linux 13.7.0 "Trixie" - Official amd64 NETINST with firmware 20260912-09:35", installer build 20250803+deb13u7) |
| ISO sha256 | not available (the ISO file is not on this machine) |
| Hostname | `LEDGER-DISTRO` |
| Build date | 2026-10-24 |
| Engine commit | `/opt/ledger` master `dc1ad54fdc3c0759e2ff5d6c980e37247492b2c6` |

A separate, hardened build exists for controlled pilots. It is not part of this release.
