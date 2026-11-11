# Optional ArchiveBox reader

The core CLI works without ArchiveBox. The optional bridge was tested with [ArchiveBox 0.9.77](https://github.com/ArchiveBox/ArchiveBox/releases/tag/v0.9.77) in a separate Python 3.13 environment. It uses native snapshots, SQLite full-text search and a loopback-only web service. It does not enable browser capture or a ZIP download route.

## Isolated setup and validation

From the repository root, create a dedicated environment:

```sh
python3.13 -m venv .venv-archivebox
.venv-archivebox/bin/python -m pip install archivebox==0.9.77
```

Two compatibility fixes were needed in the original Mac environment: missing MAC-address metadata is recorded as `Unknown`, and unreadable swap contributes zero to memory capacity while the real RAM check remains. These change only the isolated dependency installation. If these specific failures occur, the optional script preserves original files and refuses unknown versions/source shapes:

```sh
.venv-archivebox/bin/python tools/prepare_archivebox.py
```

Set absolute paths for the separate interpreter, temporary integration data and readiness receipt. These example paths are outside the repository:

```sh
export PCL_ARCHIVEBOX_PYTHON="$PWD/.venv-archivebox/bin/python"
export PCL_VALIDATION_ROOT="/tmp/pcl-native-validation"
export PCL_BRIDGE_RUNTIME="/tmp/pcl-native-runtime"
export PCL_BRIDGE_READY="/tmp/pcl-native-ready.json"
.venv/bin/python -m bridge.validate
```

Validation uses synthetic records and a separate collection. It checks duplicate publication, retained versions, full-text search, annotation preservation, backup restoration while the original is unavailable, and detection of damaged renditions. The bridge refuses normal publication unless the readiness receipt matches the current publisher and renderer hashes. Revalidate after changing those files; use persistent private paths for the receipt and runtime in a real installation.

## Private settings

Add `archivebox_python`, `archivebox_data`, `bridge_runtime`, `bridge_ready` and `web_port` to private settings. Place `archivebox_data` and `bridge_runtime` under the external library root when using a whole-library external installation. Keep `bridge_ready` with private program control files. The default web port is `8864` and the address is always `127.0.0.1`.

Configure workflow steps using this shape; replace the example data path with your absolute data directory:

```json
{
  "pre_steps": [
    {"name": "pull_notes_and_tags", "command": ["{python}", "-m", "bridge.archivebox", "pull", "--data", "/absolute/library/archivebox", "--state", "{state}"]}
  ],
  "post_steps": [
    {"name": "publish_library", "command": ["{python}", "-m", "bridge.archivebox", "publish", "--data", "/absolute/library/archivebox", "--bundle", "{bundle}"]},
    {"name": "backup_library", "command": ["{python}", "-m", "bridge.archivebox", "backup", "--data", "/absolute/library/archivebox", "--output", "{run_dir}/archivebox-backup"]}
  ]
}
```

Run a manual update and check both backup results, then use `pcl reader install --config /absolute/path/to/settings.json`. Open `http://127.0.0.1:8864/public/`. In ArchiveBox use the `deep:sqlite` search type for body text and `meta` for tag filters such as `library:latest`.

An installation's `id_namespace` participates in native snapshot identity. Leave the default for a new library; when migrating an existing library preserve its established namespace. Changing it can duplicate snapshots. Preserve private credentials and user annotations; do not include them in the code checkout.

Cross-device sharing is not configured by this project. The reader remains local to the Mac. VPN, Tailscale, system DNS, firewall and sharing settings are outside its service-management scope.
