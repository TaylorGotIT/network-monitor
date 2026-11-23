# 打包说明

## Windows 单文件 exe

```powershell
.\packaging\build_windows.ps1
```

输出：

- `dist\NetworkMonitor.exe`
- `dist\NetworkMonitor-<version>-win64.exe`

exe 为 PyInstaller onefile，静态资源打包在内；`config.json` 和 `data/` 会生成在 exe 所在目录，
所以请放在有写权限的文件夹（不要放 `C:\Program Files`）。

## Ubuntu / Debian .deb

```bash
python packaging/build_deb.py
```

输出：`dist/network-monitor_<version>_all.deb`

包内容：

- 程序：`/opt/network-monitor/`
- 启动命令：`/usr/bin/network-monitor`
- systemd 服务：`network-monitor.service`
- 配置/数据库：`/var/lib/network-monitor/`
- 默认配置模板：`/usr/share/network-monitor/config.json`

安装：

```bash
sudo apt install ./network-monitor_<version>_all.deb
sudo systemctl status network-monitor
```

卸载：`sudo apt remove network-monitor`；连数据一起删：`sudo apt purge network-monitor`。
